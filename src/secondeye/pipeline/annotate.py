"""The findings written onto the PDF itself, beside the text they are about.

A PDF gets its tracked changes in a Word copy built from its text
(pipeline/reflow.py), and that copy has lost the layout. The lawyer reading the
other side's PDF on a phone wants to see the problem on the page they were sent,
so each finding also goes onto the PDF as a sticky note where its anchor sits,
and a blocker is highlighted as well. Neither changes a word of the document:
annotations sit in a layer over the page and every reader can hide or delete
them.

Two libraries, for licensing reasons that matter to a commercial product.
pypdfium2 (BSD) finds the anchor: it gives one character box per character, so
an anchor located in the page text can be turned into the rectangles the
annotation needs. pypdfium2 cannot write, so pypdf (BSD) adds the annotations.
PyMuPDF does both and is AGPL.

The same rule as the OOXML writer: an anchor that is not found exactly once in
the document gets no annotation. The finding is still in the email and in the
Word copy, and a note beside the wrong one of two identical phrases would be
worse than no note.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from io import BytesIO

from secondeye.models import Finding, Severity
from secondeye.pipeline.ooxml import _normalize, _normalize_with_map

log = logging.getLogger(__name__)

AUTHOR = "Reviewer"  # the default of settings().redline_author


def configured_author() -> str:
    """The configured revision author, read when writing so a setting takes.

    The writer also runs inside the sandbox as a skill, where the config module
    is not bundled; there it signs as the default.
    """
    try:
        from secondeye.config import settings

        return settings().redline_author or AUTHOR
    except ImportError:
        return AUTHOR


# Above either of these the PDF is left alone: reading every character box on
# three hundred pages is slow, and the outbound size limit would drop the
# result anyway.
MAX_BYTES = 20 * 1024 * 1024
MAX_PAGES = 300

# The note icon, in points. Placed in the left margin beside the anchor's first
# line, so it does not sit on top of the text it is about.
_ICON = 18.0
_HIGHLIGHT = "ffe066"

_ONLY_ELSEWHERE = "{why}, so the findings are in this email and the Word copy only."

_ALL = re.compile(r"\bhighlight (?:everything|all|every finding)\b", re.IGNORECASE)
# "Comments only" is deliberately absent: intake reads it as a request for
# notes with no edits at all, and that more cautious reading wins before this
# module is reached.
_NONE = re.compile(r"\b(?:no highlights?|don'?t highlight|do not highlight|notes? not highlights?)\b",
                   re.IGNORECASE)


@dataclass
class Annotated:
    content: bytes | None
    placed: int = 0
    highlighted: int = 0
    # Why nothing was produced, or what the lawyer should know about the copy.
    notes: list[str] = field(default_factory=list)


def highlight_policy(instructions: str) -> str:
    """"blockers" unless the lawyer said otherwise in their own words."""
    if _NONE.search(instructions or ""):
        return "none"
    if _ALL.search(instructions or ""):
        return "all"
    return "blockers"


def annotate(pdf: bytes, findings: list[Finding], instructions: str = "",
             author: str | None = None) -> Annotated:
    """The PDF with a note per finding whose anchor was found. Never raises."""
    if len(pdf) > MAX_BYTES:
        return Annotated(None, notes=[_ONLY_ELSEWHERE.format(
            why="The PDF is too large for me to annotate")])
    anchored = [f for f in findings if (f.anchor or "").strip()]
    if not anchored:
        return Annotated(None)

    try:
        located = _locate(pdf, anchored)
    except _TooManyPages:
        return Annotated(None, notes=[_ONLY_ELSEWHERE.format(
            why=f"The PDF runs to more than {MAX_PAGES} pages, so I have not annotated it")])
    except Exception:
        # The PDF is the other side's file, and it has already been read once
        # for the review; whatever this trips on must not cost the reply.
        log.exception("could not read the PDF to annotate it")
        return Annotated(None, notes=[_ONLY_ELSEWHERE.format(
            why="I could not read the PDF closely enough to annotate it")])
    if not located:
        return Annotated(None)

    policy = highlight_policy(instructions)
    try:
        author = author or configured_author()
        content, placed, highlighted = _write(pdf, located, policy, author)
    except Exception:
        log.exception("could not write annotations onto the PDF")
        return Annotated(None, notes=[_ONLY_ELSEWHERE.format(
            why="I could not write the notes onto the PDF")])

    expected = placed + highlighted
    if not _verify(content, expected):
        log.error("annotated PDF failed verification; not attaching it")
        return Annotated(None, notes=[_ONLY_ELSEWHERE.format(
            why="I could not produce a safe annotated PDF")])
    return Annotated(content, placed=placed, highlighted=highlighted)


# --------------------------------------------------------------------------
# Locating
# --------------------------------------------------------------------------


class _TooManyPages(Exception):
    pass


@dataclass
class _Hit:
    finding: Finding
    page: int
    # One (left, bottom, right, top) per printed line the anchor covers, in
    # PDF user space, which is what annotations are positioned in.
    lines: list[tuple[float, float, float, float]]


def _locate(pdf: bytes, findings: list[Finding]) -> list[_Hit]:
    import pypdfium2

    document = pypdfium2.PdfDocument(BytesIO(pdf))
    if len(document) > MAX_PAGES:
        raise _TooManyPages()

    needles = {id(f): _normalize(f.anchor) for f in findings}
    # Every occurrence of every anchor, across the whole document, before any
    # is placed: the once-only rule is about the document, not the page.
    occurrences: dict[int, list[tuple[int, int, int]]] = {id(f): [] for f in findings}
    pages: list = []
    for number in range(len(document)):
        page = document[number]
        textpage = page.get_textpage()
        raw = textpage.get_text_range()
        normalized, index_map = _normalize_with_map(raw)
        pages.append((page, textpage, index_map))
        for key, needle in needles.items():
            if not needle:
                continue
            start = normalized.find(needle)
            while start != -1:
                occurrences[key].append((number, start, start + len(needle)))
                start = normalized.find(needle, start + 1)

    hits: list[_Hit] = []
    for finding in findings:
        found = occurrences[id(finding)]
        if len(found) != 1:
            continue
        number, start, end = found[0]
        _, textpage, index_map = pages[number]
        boxes = []
        for position in range(start, end):
            if position >= len(index_map):
                break
            box = textpage.get_charbox(index_map[position])
            if box and box[2] > box[0] and box[3] > box[1]:
                boxes.append(box)
        lines = _lines(boxes)
        if lines:
            hits.append(_Hit(finding, number, lines))
    return hits


def _lines(boxes: list[tuple[float, float, float, float]]) -> list[tuple[float, float, float, float]]:
    """Character boxes grouped into the printed lines they sit on."""
    out: list[list[float]] = []
    for left, bottom, right, top in boxes:
        for line in out:
            # Same baseline, within a couple of points, is the same line.
            if abs(line[1] - bottom) <= 2.5:
                line[0] = min(line[0], left)
                line[1] = min(line[1], bottom)
                line[2] = max(line[2], right)
                line[3] = max(line[3], top)
                break
        else:
            out.append([left, bottom, right, top])
    return [tuple(line) for line in out]


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------


def _write(pdf: bytes, hits: list[_Hit], policy: str, author: str) -> tuple[bytes, int, int]:
    from pypdf import PdfWriter
    from pypdf.annotations import Highlight, Text
    from pypdf.generic import ArrayObject, FloatObject, NameObject, TextStringObject

    writer = PdfWriter(clone_from=BytesIO(pdf))
    placed = highlighted = 0
    for hit in hits:
        left = hit.lines[0][0]
        first_top = max(line[3] for line in hit.lines)
        # In the margin to the left of the first line, hanging from its top.
        icon = (max(0.0, left - _ICON - 4), first_top - _ICON, max(_ICON, left - 4), first_top)
        note = Text(rect=icon, text=_body(hit.finding), open=False)
        note.update({
            NameObject("/T"): TextStringObject(author),
            NameObject("/Subj"): TextStringObject(hit.finding.title[:120]),
            NameObject("/Name"): NameObject("/Comment"),
        })
        writer.add_annotation(page_number=hit.page, annotation=note)
        placed += 1

        wants = policy == "all" or (
            policy == "blockers" and hit.finding.severity is Severity.BLOCKER)
        if not wants:
            continue
        quads: list[float] = []
        for line_left, line_bottom, line_right, line_top in hit.lines:
            # Upper-left, upper-right, lower-left, lower-right: the order the
            # PDF specification gives, and the one Acrobat draws correctly.
            quads += [line_left, line_top, line_right, line_top,
                      line_left, line_bottom, line_right, line_bottom]
        rect = (min(line[0] for line in hit.lines), min(line[1] for line in hit.lines),
                max(line[2] for line in hit.lines), max(line[3] for line in hit.lines))
        mark = Highlight(rect=rect, quad_points=ArrayObject([FloatObject(q) for q in quads]),
                         highlight_color=_HIGHLIGHT)
        mark.update({
            NameObject("/T"): TextStringObject(author),
            NameObject("/Contents"): TextStringObject(hit.finding.title),
        })
        writer.add_annotation(page_number=hit.page, annotation=mark)
        highlighted += 1

    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue(), placed, highlighted


def _body(finding: Finding) -> str:
    parts = [finding.title.strip()]
    if finding.explanation and finding.explanation.strip():
        parts.append(finding.explanation.strip())
    if finding.question:
        question = finding.question.strip()
        if finding.options:
            question += "  (" + " / ".join(finding.options) + ")"
        parts.append(question)
    elif finding.suggested_text and finding.suggested_text.strip() != finding.anchor.strip():
        parts.append(f"Suggested: {finding.suggested_text.strip()}")
    return "\n\n".join(parts)


def _verify(content: bytes, expected: int) -> bool:
    """The output opens in an independent parser and carries every note."""
    from pypdf import PdfReader

    try:
        reader = PdfReader(BytesIO(content))
        count = 0
        for page in reader.pages:
            annots = page.get("/Annots")
            if annots is not None:
                count += len(annots.get_object())
    except Exception:
        log.exception("the annotated PDF would not re-open")
        return False
    return count >= expected


def count_annotations(content: bytes) -> int:
    """How many annotations a PDF carries, for tests and the word pack."""
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(content))
    total = 0
    for page in reader.pages:
        annots = page.get("/Annots")
        if annots is not None:
            total += len(annots.get_object())
    return total
