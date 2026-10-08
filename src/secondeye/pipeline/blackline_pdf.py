"""The blackline as a PDF a client can open anywhere, and its change summary.

Litera Compare's output that people actually forward is not the Word file: it
is the PDF, a change summary on the first page and then the document with
insertions in blue, deletions struck through in red, moved text in green and a
bar in the margin beside every changed line. A client reads it on a phone; a
partner prints it. This module makes that from the Word comparison that
`compare.compare()` has already written and proved, so the PDF can never show a
change the Word file does not carry, or miss one it does.

Three parts, all deterministic. The model's part is one line of significance
per material change, which arrives as data and is only typeset here.

- `summarise()`: the counts, by kind and by clause, and the change list with
  short before and after, as JSON for the agent and for the email.
- `render()`: the PDF. Through LibreOffice when it is installed and its output
  demonstrably shows the changes (the document's own layout, with our colours
  set in a throwaway profile); otherwise typeset here from the comparison's
  own markup. Either way page 1 is the change summary, every page is headed
  "Blackline: <new> against <old>" and footed "Page x of y".
- `check()`: what the host runs on the file that comes back: it opens, it has
  the summary, and the words of both versions that differ are all in it.

No reportlab. The PDF is written directly, in Helvetica with its published
widths, which is a few hundred lines rather than a dependency the server image
does not otherwise need. pypdf (already a dependency) is used only to read a
PDF back and to stamp LibreOffice's pages.
"""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
import unicodedata
import zlib
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn

from secondeye.pipeline import compare
from secondeye.pipeline.ooxml import _normalize, _paragraphs_under, _run_text, _story_parts

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# The summary: deterministic counts
# --------------------------------------------------------------------------

_SHORT = 90


def clause_of(change: compare.Change) -> str:
    """The clause a change belongs to, for counting: "4.2", "under 4" -> "4",
    a move counted where it landed."""
    where = change.where
    story = ""
    if ", " in where and not where.startswith(("from ", "under ")):
        story, _, where = where.partition(", ")
    if change.kind == "moved" and " to " in where:
        where = where.rsplit(" to ", 1)[1]
    where = where.removeprefix("under ").strip()
    if where in ("near the start",) or where.startswith("paragraph "):
        where = "(unnumbered)"
    return f"{story}, {where}" if story else where


def counts_of(change: compare.Change) -> tuple[int, int, int]:
    """(insertions, deletions, moves) in one change, counted the way a
    blackline shows them: each run of inserted words is one insertion, each
    run of struck words one deletion."""
    if change.kind == "inserted":
        return 1, 0, 0
    if change.kind == "deleted":
        return 0, 1, 0
    if change.kind == "moved":
        return 0, 0, 1
    ins = dels = 0
    for hunk in compare._hunks(change.before, change.after):
        if hunk.text.strip():
            ins += 1
        if change.before[hunk.start:hunk.end].strip():
            dels += 1
    return max(ins, 0), max(dels, 0), 0


def short_diff(before: str, after: str, limit: int = _SHORT) -> tuple[str, str]:
    """The part of each side that differs, trimmed to whole words, with a
    word of context either side so "thirty" -> "sixty" reads as "within
    thirty (30) days" -> "within sixty (60) days"."""
    a, b = before.split(), after.split()
    start = 0
    while start < min(len(a), len(b)) and a[start] == b[start]:
        start += 1
    end = 0
    while (end < min(len(a), len(b)) - start
           and a[len(a) - 1 - end] == b[len(b) - 1 - end]):
        end += 1
    lo = max(0, start - 1)
    a_part = " ".join(a[lo:len(a) - max(0, end - 1)])
    b_part = " ".join(b[lo:len(b) - max(0, end - 1)])
    lead = "… " if lo > 0 else ""
    return _clip(lead + a_part, limit), _clip(lead + b_part, limit)


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def summarise(comparison: compare.Comparison) -> dict:
    """The comparison as JSON: counts, counts by clause, and every change."""
    ins = dels = moves = 0
    by_clause: dict[str, list[int]] = {}
    listed: list[dict] = []
    for index, change in enumerate(comparison.changes):
        i, d, m = counts_of(change)
        ins, dels, moves = ins + i, dels + d, moves + m
        clause = clause_of(change)
        row = by_clause.setdefault(clause, [0, 0, 0])
        row[0] += i
        row[1] += d
        row[2] += m
        if change.kind == "inserted":
            before, after = "", _clip(change.after, _SHORT)
        elif change.kind == "deleted":
            before, after = _clip(change.before, _SHORT), ""
        elif change.kind == "moved":
            # Its number changes with its place; what matters is whether the
            # words changed on the way.
            old, new = (compare._CLAUSE.sub("", t, count=1).strip()
                        for t in (change.before, change.after))
            before, after = (short_diff(old, new) if _normalize(old) != _normalize(new)
                             else ("", _clip(new, _SHORT)))
        else:
            before, after = short_diff(change.before, change.after)
        listed.append({
            "index": index, "kind": change.kind, "clause": clause, "where": change.where,
            "before": before, "after": after,
            "before_full": _clip(change.before, 600), "after_full": _clip(change.after, 600),
            "landed": change.landed,
        })
    counts = {"insertions": ins, "deletions": dels, "moves": moves,
              "changes": len(comparison.changes), "clauses": len(by_clause)}
    return {
        "counts": counts,
        "sentence": counts_sentence(counts),
        "by_clause": [{"clause": c, "insertions": r[0], "deletions": r[1], "moves": r[2]}
                      for c, r in by_clause.items()],
        "changes": listed,
        "proven": comparison.proven,
        "notes": list(comparison.notes),
    }


def _n(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def counts_sentence(counts: dict) -> str:
    """ "9 changes: 6 insertions, 5 deletions, 1 move, in 4 clauses." """
    if not counts["changes"]:
        return "No changes."
    return (f"{_n(counts['changes'], 'change')}: {_n(counts['insertions'], 'insertion')}, "
            f"{_n(counts['deletions'], 'deletion')}, {_n(counts['moves'], 'move')}, "
            f"in {_n(counts['clauses'], 'clause')}.")


# --------------------------------------------------------------------------
# What the blackline shows: the comparison's own markup, read back
# --------------------------------------------------------------------------

PLAIN, INS, DEL, MOVE_FROM, MOVE_TO = "plain", "ins", "del", "move_from", "move_to"


@dataclass
class Para:
    segments: list[tuple[str, str]]          # (style, text)
    bold: bool = False
    heading: str = ""                        # a section label drawn above it

    @property
    def changed(self) -> bool:
        return any(style != PLAIN and text.strip() for style, text in self.segments)

    @property
    def text(self) -> str:
        return "".join(t for _, t in self.segments)


def _segments(paragraph, author: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []

    def walk(node, style: str) -> None:
        for child in node:
            if not isinstance(child.tag, str):
                continue
            ours = child.get(qn("w:author")) == author
            if child.tag == qn("w:r"):
                text = _run_text(child)
                if text:
                    out.append((style, text))
            elif child.tag in (qn("w:ins"), qn("w:moveTo")):
                walk(child, INS if ours else style)
            elif child.tag in (qn("w:del"), qn("w:moveFrom")):
                if ours:
                    walk(child, DEL)
            elif child.tag == qn("w:sdt"):
                for grandchild in child:
                    if grandchild.tag == qn("w:sdtContent"):
                        walk(grandchild, style)
            elif child.tag in (qn("w:hyperlink"), qn("w:smartTag"), qn("w:fldSimple"),
                               qn("w:customXml"), qn("w:bdo"), qn("w:dir")):
                walk(child, style)

    walk(paragraph._p, PLAIN)
    merged: list[tuple[str, str]] = []
    for style, text in out:
        if merged and merged[-1][0] == style:
            merged[-1] = (style, merged[-1][1] + text)
        else:
            merged.append((style, text))
    # The writer puts a replacement's new words before the old ones; a
    # blackline reads the other way, struck text first.
    for i in range(len(merged) - 1):
        if merged[i][0] == INS and merged[i + 1][0] == DEL:
            merged[i], merged[i + 1] = merged[i + 1], merged[i]
    return merged


def _is_heading(paragraph) -> bool:
    try:
        name = (paragraph.style.name or "") if paragraph.style is not None else ""
    except Exception:  # noqa: BLE001 - a style python-docx cannot resolve
        name = ""
    return name.startswith(("Heading", "Title"))


def paragraphs(content: bytes, changes: list[compare.Change],
               author: str = compare.AUTHOR) -> list[Para]:
    """Every paragraph of the Word comparison, as styled segments. Moved text
    is found by the change list: a paragraph wholly struck whose words are a
    move's "before" is where it moved from; one wholly inserted whose words
    are its "after" is where it moved to."""
    doc = Document(BytesIO(content))
    froms = {_normalize(c.before) for c in changes if c.kind == "moved"}
    tos = {_normalize(c.after) for c in changes if c.kind == "moved"}

    def read(root, parent) -> list[Para]:
        out: list[Para] = []
        for p in _paragraphs_under(root, parent):
            segments = _segments(p, author)
            if not "".join(t for _, t in segments).strip():
                continue
            styles = {s for s, t in segments if t.strip()}
            joined = _normalize("".join(t for _, t in segments))
            if styles == {DEL} and joined in froms:
                segments = [(MOVE_FROM, t) for _, t in segments]
            elif styles == {INS} and joined in tos:
                segments = [(MOVE_TO, t) for _, t in segments]
            out.append(Para(segments, bold=_is_heading(p)))
        return out

    body = read(doc.element.body, doc)
    for part in _story_parts(doc):
        story = read(part.element, part)
        if story:
            label = str(part.partname).rsplit("/", 1)[-1].removesuffix(".xml")
            story[0].heading = f"[{label}]"
            body += story
    return body


# --------------------------------------------------------------------------
# Text as the PDF holds it
# --------------------------------------------------------------------------

_SUBSTITUTES = {"\u2192": "->", "\u2190": "<-", "\u2264": "<=", "\u2265": ">=",
                "\u2260": "!=", "\u2713": "v", "\u00a0": " ", "\u2009": " ", "\u202f": " ",
                "\u200b": "", "\t": " "}


def pdf_text(text: str) -> str:
    """The text as the standard PDF fonts can show it (Windows-1252), so what
    is drawn and what `check()` looks for are the same characters."""
    out: list[str] = []
    for ch in text:
        ch = _SUBSTITUTES.get(ch, ch)
        try:
            ch.encode("cp1252")
            out.append(ch)
        except UnicodeEncodeError:
            decomposed = unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode()
            out.append(decomposed or "?")
    return "".join(out)


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", pdf_text(text))


# --------------------------------------------------------------------------
# A minimal PDF writer: Helvetica, lines, rectangles
# --------------------------------------------------------------------------

# Helvetica and Helvetica-Bold advance widths, per 1000 units, for
# Windows-1252 codes 32-255, from the fonts' published AFM metrics.
_HELVETICA = [
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556,
    556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556, 1015, 667, 667, 722,
    722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778, 667, 778, 722, 667, 611, 722,
    667, 944, 667, 667, 611, 278, 278, 278, 469, 556, 333, 556, 556, 500, 556, 556, 278, 556,
    556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500, 278, 556, 500, 722, 500, 500,
    500, 334, 260, 334, 584, 761, 556, 0, 222, 556, 333, 1000, 556, 556, 333, 1000, 667, 333,
    1000, 0, 611, 0, 0, 222, 222, 333, 333, 350, 556, 1000, 333, 1000, 500, 333, 944, 0, 500,
    667, 278, 333, 556, 556, 556, 556, 260, 556, 333, 737, 370, 556, 584, 333, 737, 333, 400,
    584, 333, 333, 333, 556, 537, 278, 333, 333, 365, 556, 834, 834, 834, 611, 667, 667, 667,
    667, 667, 667, 1000, 722, 667, 667, 667, 667, 278, 278, 278, 278, 722, 722, 778, 778, 778,
    778, 778, 584, 778, 722, 722, 722, 722, 667, 667, 611, 556, 556, 556, 556, 556, 556, 889,
    500, 556, 556, 556, 556, 278, 278, 278, 278, 556, 556, 556, 556, 556, 556, 556, 584, 611,
    556, 556, 556, 556, 500, 556, 500,
]
_HELVETICA_BOLD = [
    278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556,
    556, 556, 556, 556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611, 975, 722, 722, 722,
    722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778, 667, 778, 722, 667, 611, 722,
    667, 944, 667, 667, 611, 333, 278, 333, 584, 556, 333, 556, 611, 556, 611, 556, 333, 611,
    611, 278, 278, 556, 278, 889, 611, 611, 611, 611, 389, 556, 333, 611, 556, 778, 556, 556,
    500, 389, 280, 389, 584, 761, 556, 0, 278, 556, 500, 1000, 556, 556, 333, 1000, 667, 333,
    1000, 0, 611, 0, 0, 278, 278, 500, 500, 350, 556, 1000, 333, 1000, 556, 333, 944, 0, 500,
    667, 278, 333, 556, 556, 556, 556, 280, 556, 333, 737, 370, 556, 584, 333, 737, 333, 400,
    584, 333, 333, 333, 611, 556, 278, 333, 333, 365, 556, 834, 834, 834, 611, 722, 722, 722,
    722, 722, 722, 1000, 722, 667, 667, 667, 667, 278, 278, 278, 278, 722, 722, 778, 778, 778,
    778, 778, 584, 778, 722, 722, 722, 722, 667, 667, 611, 611, 556, 556, 556, 556, 556, 889,
    556, 556, 556, 556, 556, 278, 278, 278, 278, 611, 611, 611, 611, 611, 611, 611, 584, 611,
    611, 611, 611, 611, 556, 611, 556,
]
_FONTS = {"F1": ("Helvetica", _HELVETICA), "F2": ("Helvetica-Bold", _HELVETICA_BOLD)}

PAGE_W, PAGE_H = 595.28, 841.89               # A4
LEFT, RIGHT, TOP, BOTTOM = 72.0, 56.0, 70.0, 62.0
BODY, LEADING = 10.0, 13.5

_COLOURS = {
    PLAIN: (0.07, 0.07, 0.07), INS: (0.08, 0.28, 0.85), DEL: (0.80, 0.10, 0.10),
    MOVE_FROM: (0.0, 0.50, 0.18), MOVE_TO: (0.0, 0.50, 0.18),
}
_GREY = (0.40, 0.40, 0.40)
_RULE = (0.75, 0.75, 0.75)


def width(text: str, font: str = "F1", size: float = BODY) -> float:
    table = _FONTS[font][1]
    total = 0
    for byte in pdf_text(text).encode("cp1252"):
        total += table[byte - 32] if byte >= 32 else 0
    return total * size / 1000.0


def _literal(text: str) -> str:
    raw = pdf_text(text).encode("cp1252")
    escaped = raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")
    return "(" + escaped.decode("latin-1") + ")"


def _f(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


class Canvas:
    """One page's content stream."""

    def __init__(self, w: float = PAGE_W, h: float = PAGE_H) -> None:
        self.w, self.h = w, h
        self.ops: list[str] = []

    def text(self, x: float, y: float, text: str, font: str = "F1", size: float = BODY,
             colour: tuple = _COLOURS[PLAIN]) -> None:
        if not text:
            return
        r, g, b = colour
        self.ops.append(f"BT {_f(r)} {_f(g)} {_f(b)} rg /{font} {_f(size)} Tf "
                        f"{_f(x)} {_f(y)} Td {_literal(text)} Tj ET")

    def line(self, x1: float, y1: float, x2: float, y2: float, colour: tuple = _GREY,
             w: float = 0.6) -> None:
        r, g, b = colour
        self.ops.append(f"{_f(r)} {_f(g)} {_f(b)} RG {_f(w)} w {_f(x1)} {_f(y1)} m "
                        f"{_f(x2)} {_f(y2)} l S")

    def rect(self, x: float, y: float, w: float, h: float, fill: tuple) -> None:
        r, g, b = fill
        self.ops.append(f"{_f(r)} {_f(g)} {_f(b)} rg {_f(x)} {_f(y)} {_f(w)} {_f(h)} re f")

    def run(self, x: float, y: float, text: str, style: str, font: str = "F1",
            size: float = BODY) -> float:
        """Draw one styled run and its decoration; returns its width."""
        colour = _COLOURS[style]
        self.text(x, y, text, font, size, colour)
        span = width(text, font, size)
        if style == INS:
            self.line(x, y - 1.8, x + span, y - 1.8, colour, 0.7)
        elif style == DEL:
            self.line(x, y + size * 0.3, x + span, y + size * 0.3, colour, 0.7)
        elif style == MOVE_TO:
            self.line(x, y - 1.6, x + span, y - 1.6, colour, 0.6)
            self.line(x, y - 3.2, x + span, y - 3.2, colour, 0.6)
        elif style == MOVE_FROM:
            self.line(x, y + size * 0.22, x + span, y + size * 0.22, colour, 0.6)
            self.line(x, y + size * 0.40, x + span, y + size * 0.40, colour, 0.6)
        return span

    def stream(self) -> bytes:
        return "\n".join(self.ops).encode("latin-1")


def write_pdf(pages: list[Canvas], title: str = "") -> bytes:
    """The pages as a PDF file, with the two Helvetica faces as WinAnsi fonts."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    fonts = {name: add(f"<< /Type /Font /Subtype /Type1 /BaseFont /{base} "
                       f"/Encoding /WinAnsiEncoding >>".encode())
             for name, (base, _) in _FONTS.items()}
    font_dict = " ".join(f"/{name} {num} 0 R" for name, num in fonts.items())
    pages_num = len(objects) + 2 * len(pages) + 1   # the object after the pages'
    kids: list[int] = []
    for page in pages:
        data = zlib.compress(page.stream())
        content = add(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(data)
                      + data + b"\nendstream")
        kids.append(add((f"<< /Type /Page /Parent {pages_num} 0 R /MediaBox [0 0 "
                         f"{_f(page.w)} {_f(page.h)}] /Resources << /Font << {font_dict} >> >> "
                         f"/Contents {content} 0 R >>").encode()))
    assert len(objects) + 1 == pages_num
    add((f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] "
         f"/Count {len(kids)} >>").encode())
    catalog = add(f"<< /Type /Catalog /Pages {pages_num} 0 R >>".encode())
    info = add(f"<< /Title {_literal(title)} /Producer (Second Eye blackline) >>".encode())

    out = BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write((f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R "
               f"/Info {info} 0 R >>\nstartxref\n{xref}\n%%EOF\n").encode())
    return out.getvalue()


# --------------------------------------------------------------------------
# Typesetting
# --------------------------------------------------------------------------

_PIECE = re.compile(r"\s+|\S+")


def _wrap(segments: list[tuple[str, str]], limit: float, font: str,
          size: float) -> list[list[tuple[str, str]]]:
    """Styled segments broken into lines no wider than `limit`."""
    lines: list[list[tuple[str, str]]] = [[]]
    used = 0.0
    for style, text in segments:
        for piece in _PIECE.findall(text.replace("\n", " ")):
            if piece.isspace():
                piece = " "
                if not lines[-1]:
                    continue
            span = width(piece, font, size)
            if used + span > limit and lines[-1] and not piece.isspace():
                lines.append([])
                used = 0.0
            while span > limit and len(piece) > 1:    # a word wider than the line
                cut = len(piece)
                while cut > 1 and width(piece[:cut], font, size) > limit - used:
                    cut -= 1
                lines[-1].append((style, piece[:cut]))
                lines.append([])
                used = 0.0
                piece = piece[cut:]
                span = width(piece, font, size)
            lines[-1].append((style, piece))
            used += span
    out = []
    for line in lines:
        while line and line[-1][1].isspace():
            line.pop()
        runs: list[tuple[str, str]] = []
        for style, piece in line:
            if runs and runs[-1][0] == style:
                runs[-1] = (style, runs[-1][1] + piece)
            else:
                runs.append((style, piece))
        out.append(runs)
    return out or [[]]


class Pages:
    """Flowing text onto A4 pages, leaving room for the header and footer."""

    def __init__(self) -> None:
        self.pages: list[Canvas] = []
        self.y = 0.0
        self.new()

    def new(self) -> None:
        self.pages.append(Canvas())
        self.y = PAGE_H - TOP

    @property
    def page(self) -> Canvas:
        return self.pages[-1]

    def room(self, height: float) -> None:
        if self.y - height < BOTTOM:
            self.new()

    def gap(self, height: float) -> None:
        self.y -= height

    def lines(self, segments, font="F1", size=BODY, leading=LEADING, indent=0.0,
              bar: bool = True) -> None:
        limit = PAGE_W - LEFT - RIGHT - indent
        for line in _wrap(segments, limit, font, size):
            self.room(leading)
            self.y -= leading
            x = LEFT + indent
            changed = False
            previous = PLAIN
            for style, text in line:
                if previous == DEL and style == INS:
                    x += 2.5                  # struck and inserted words apart
                x += self.page.run(x, self.y, text, style, font, size)
                previous = style
                changed = changed or (style != PLAIN and bool(text.strip()))
            if bar and changed:
                self.page.line(LEFT - 16, self.y - 3, LEFT - 16, self.y + size, (0, 0, 0), 1.6)


def _stamp(canvas: Canvas, header: str, number: int, total: int) -> None:
    canvas.text(LEFT, canvas.h - 38, header, "F1", 8, _GREY)
    canvas.line(LEFT, canvas.h - 44, canvas.w - RIGHT, canvas.h - 44, _RULE, 0.5)
    footer = f"Page {number} of {total}"
    canvas.text((canvas.w - width(footer, "F1", 8)) / 2, 30, footer, "F1", 8, _GREY)


# --------------------------------------------------------------------------
# Page 1: the change summary
# --------------------------------------------------------------------------


@dataclass
class Material:
    """One material change as the summary table shows it."""

    clause: str
    before: str
    after: str
    significance: str = ""
    kind: str = "replaced"


@dataclass
class Labels:
    earlier: str                     # "Falcon SPA v1.docx"
    later: str
    earlier_detail: str = ""         # "the original, received Tue 1 Sep 2026"
    later_detail: str = ""

    @property
    def header(self) -> str:
        return f"Blackline: {self.later} against {self.earlier}"


def _table(out: Pages, columns: list[tuple[str, float]], rows: list[list],
           size: float = 8.5) -> None:
    """A ruled table. A cell is a string or a list of styled segments."""
    leading = size * 1.3
    total = sum(w for _, w in columns)

    def draw_row(cells: list, header: bool) -> None:
        wrapped = []
        for (_, w), cell in zip(columns, cells, strict=True):
            segments = [(PLAIN, cell)] if isinstance(cell, str) else cell
            wrapped.append(_wrap(segments, w - 8, "F2" if header else "F1", size))
        height = max(len(w) for w in wrapped) * leading + 6
        out.room(height)
        top = out.y
        if header:
            out.page.rect(LEFT, top - height, total, height, (0.93, 0.93, 0.93))
        x = LEFT
        for (_, w), lines in zip(columns, wrapped, strict=True):
            y = top - 3
            for line in lines:
                y -= leading
                cx = x + 4
                for style, text in line:
                    cx += out.page.run(cx, y + 2, text, style, "F2" if header else "F1", size)
            x += w
        out.page.line(LEFT, top - height, LEFT + total, top - height, _RULE, 0.5)
        out.y = top - height

    out.page.line(LEFT, out.y, LEFT + total, out.y, _RULE, 0.5)
    draw_row([name for name, _ in columns], header=True)
    for row in rows:
        draw_row(row, header=False)


def _summary(out: Pages, labels: Labels, summary: dict, material: list[Material],
             engine_note: str) -> None:
    out.gap(8)
    out.lines([(PLAIN, "Change summary")], "F2", 16, 20, bar=False)
    out.gap(4)
    out.lines([(PLAIN, labels.header)], "F1", 10.5, 14, bar=False)
    out.gap(6)
    for side, name, detail in (("Earlier", labels.earlier, labels.earlier_detail),
                               ("Later", labels.later, labels.later_detail)):
        out.lines([(PLAIN, f"{side}: {name}" + (f" ({detail})" if detail else ""))],
                  "F1", 9.5, 13, bar=False)
    out.gap(8)
    out.lines([(PLAIN, summary["sentence"])], "F2", 10.5, 14, bar=False)
    out.gap(4)
    out.lines([(PLAIN, "Key: "), (INS, "inserted"), (PLAIN, "   "), (DEL, "deleted"),
               (PLAIN, "   "), (MOVE_TO, "moved (new place)"), (PLAIN, "   "),
               (MOVE_FROM, "moved (old place)"),
               (PLAIN, "   | a bar in the margin marks every changed line")],
              "F1", 8.5, 12, bar=False)
    out.gap(10)

    clauses = summary["by_clause"]
    if clauses:
        out.lines([(PLAIN, "By clause")], "F2", 10.5, 14, bar=False)
        out.gap(3)
        shown = clauses[:30]
        _table(out, [("Clause", 160), ("Insertions", 70), ("Deletions", 70), ("Moves", 70)],
               [[c["clause"], str(c["insertions"]), str(c["deletions"]), str(c["moves"])]
                for c in shown])
        if len(clauses) > len(shown):
            out.lines([(PLAIN, f"and {len(clauses) - len(shown)} more clauses.")],
                      "F1", 8.5, 12, bar=False)
        out.gap(12)

    out.lines([(PLAIN, "Material changes")], "F2", 10.5, 14, bar=False)
    out.gap(3)
    if material:
        rows = []
        for m in material:
            old, new = (MOVE_FROM, MOVE_TO) if m.kind == "moved" else (DEL, INS)
            change: list[tuple[str, str]] = []
            if m.kind == "moved":
                change.append((PLAIN, "moved: "))
            if m.before:
                change.append((old, m.before))
            if m.before and m.after:
                change.append((PLAIN, "  "))
            if m.after:
                change.append((new, m.after))
            rows.append([m.clause, change or "(no text)", m.significance])
        _table(out, [("Clause", 62), ("Change", 222), ("Why it matters", 183)], rows)
    else:
        out.lines([(PLAIN, "None of the changes was marked as material.")], "F1", 9.5, 13,
                  bar=False)
    if engine_note:
        out.gap(10)
        out.lines([(PLAIN, engine_note)], "F1", 8, 11, bar=False)


# --------------------------------------------------------------------------
# The document pages, typeset here
# --------------------------------------------------------------------------


def _body(out: Pages, paras: list[Para]) -> None:
    for para in paras:
        if para.heading:
            out.gap(8)
            out.lines([(PLAIN, para.heading)], "F2", 9, 12, bar=False)
        out.lines(para.segments, "F2" if para.bold else "F1")
        out.gap(5)


# --------------------------------------------------------------------------
# The document pages, through LibreOffice
# --------------------------------------------------------------------------

# Insertions blue and deletions red, instead of LibreOffice's colour per author.
# Written into a profile made for the one conversion, so nothing touches a
# LibreOffice a person uses. Attributes (underline, strikethrough) and the
# change bar are LibreOffice's defaults.
_PROFILE = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry"
 xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<item oor:path="/org.openoffice.Office.Writer/Revision/TextDisplay/Insert"><prop oor:name="Color" oor:op="fuse"><value>1459413</value></prop></item>
<item oor:path="/org.openoffice.Office.Writer/Revision/TextDisplay/Delete"><prop oor:name="Color" oor:op="fuse"><value>13374490</value></prop></item>
</oor:items>
"""


def libreoffice_pdf(content: bytes, timeout: int = 180) -> bytes | None:
    """The Word comparison printed by LibreOffice with its changes shown, or
    None when LibreOffice is not installed or produced nothing."""
    from secondeye import convert

    executable = convert.binary()
    if not executable:
        return None
    with tempfile.TemporaryDirectory() as work:
        profile = Path(work) / "profile" / "user"
        profile.mkdir(parents=True)
        (profile / "registrymodifications.xcu").write_text(_PROFILE)
        source = Path(work) / "blackline.docx"
        source.write_bytes(content)
        outdir = Path(work) / "out"
        outdir.mkdir()
        try:
            subprocess.run([executable, "--headless", "--norestore",
                            f"-env:UserInstallation=file://{work}/profile",
                            "--convert-to", "pdf", "--outdir", str(outdir), str(source)],
                           capture_output=True, timeout=timeout, check=False)
        except (subprocess.TimeoutExpired, OSError) as e:
            log.warning("LibreOffice did not print the blackline: %s", e)
            return None
        produced = list(outdir.glob("*.pdf"))
        return produced[0].read_bytes() if produced else None


def _add_stamped(writer, pdf: bytes, labels: Labels, first: int, total: int) -> None:
    """LibreOffice's pages, with our header and footer laid over them."""
    from pypdf import PdfReader

    for offset, source in enumerate(PdfReader(BytesIO(pdf)).pages):
        page = writer.add_page(source)
        overlay = Canvas(float(page.mediabox.width), float(page.mediabox.height))
        _stamp(overlay, labels.header, first + offset, total)
        page.merge_page(PdfReader(BytesIO(write_pdf([overlay]))).pages[0])


# --------------------------------------------------------------------------
# The whole thing
# --------------------------------------------------------------------------


@dataclass
class Rendered:
    content: bytes
    pages: int
    engine: str                      # "libreoffice" | "typeset"
    notes: list[str] = field(default_factory=list)


def render(comparison_docx: bytes, changes: list[compare.Change], labels: Labels,
           material: list[Material], engine: str = "auto",
           author: str = compare.AUTHOR) -> Rendered:
    """Page 1 the change summary, then the blackline.

    `engine` "auto" prints the Word comparison through LibreOffice when it is
    installed and the printed pages carry every change; "typeset" always sets
    it here; "libreoffice" insists (and raises when it cannot)."""
    summary = summarise(compare.Comparison(content=comparison_docx, changes=changes))
    notes: list[str] = []

    if engine in ("auto", "libreoffice"):
        printed = libreoffice_pdf(comparison_docx)
        problems = (["LibreOffice is not installed or printed nothing"] if printed is None
                    else check(printed, changes, labels=None, summary_sentence=None))
        if not problems:
            note = ("The document pages are the Word comparison as LibreOffice prints it: "
                    "moved text shows as a deletion and an insertion there.")
            return _assemble_libreoffice(printed, labels, summary, material, note)
        if engine == "libreoffice":
            raise RuntimeError("LibreOffice could not print the blackline: " + problems[0])
        log.info("typesetting the blackline ourselves: %s", problems[0])

    out = Pages()
    _summary(out, labels, summary, material,
             "The document pages are set from the Word comparison's text; the Word file "
             "has the original layout.")
    out.new()
    _body(out, paragraphs(comparison_docx, changes, author))
    total = len(out.pages)
    for number, page in enumerate(out.pages, start=1):
        _stamp(page, labels.header, number, total)
    return Rendered(write_pdf(out.pages, labels.header), total, "typeset", notes)


def _assemble_libreoffice(printed: bytes, labels: Labels, summary: dict,
                          material: list[Material], note: str) -> Rendered:
    from pypdf import PdfReader, PdfWriter

    front = Pages()
    _summary(front, labels, summary, material, note)
    body_count = len(PdfReader(BytesIO(printed)).pages)
    total = len(front.pages) + body_count
    for number, page in enumerate(front.pages, start=1):
        _stamp(page, labels.header, number, total)
    writer = PdfWriter()
    for page in PdfReader(BytesIO(write_pdf(front.pages))).pages:
        writer.add_page(page)
    _add_stamped(writer, printed, labels, len(front.pages) + 1, total)
    writer.add_metadata({"/Title": labels.header, "/Producer": "Second Eye blackline"})
    buffer = BytesIO()
    writer.write(buffer)
    return Rendered(buffer.getvalue(), total, "libreoffice")


# --------------------------------------------------------------------------
# The check the host runs on what comes back
# --------------------------------------------------------------------------


def fragments(change: compare.Change) -> list[str]:
    """The words of each version that a blackline of this change must show."""
    if change.kind == "inserted":
        return [change.after]
    if change.kind == "deleted":
        return [change.before]
    if change.kind == "moved":
        return [change.before, change.after]
    out: list[str] = []
    for hunk in compare._hunks(change.before, change.after):
        old = change.before[hunk.start:hunk.end]
        out += [t for t in (old, hunk.text) if t.strip()]
    return out


def page_texts(pdf: bytes) -> list[str]:
    from pypdf import PdfReader

    return [page.extract_text() or "" for page in PdfReader(BytesIO(pdf)).pages]


def check(pdf: bytes, changes: list[compare.Change], labels: Labels | None,
          summary_sentence: str | None) -> list[str]:
    """Problems with a blackline PDF; empty when it passes.

    It opens; with `labels`, every page carries the header and "Page x of y",
    and page 1 is the change summary with `summary_sentence`; and the words of
    both versions that differ are all in it. Whitespace is ignored, because a
    PDF's text layer puts spaces where the layout suggests them."""
    try:
        texts = page_texts(pdf)
    except Exception as e:  # noqa: BLE001 - any failure to read is the finding
        return [f"the PDF does not open: {e}"]
    if not texts:
        return ["the PDF has no pages"]
    total = len(texts)
    squashed = [_squash(t) for t in texts]
    problems: list[str] = []
    if labels is not None:
        header = _squash(labels.header)
        for number, text in enumerate(squashed, start=1):
            footer = _squash(f"Page {number} of {total}")
            if header not in text:
                problems.append(f'page {number} is not headed "{labels.header}"')
            if footer not in text:
                problems.append(f'page {number} is not footed "Page {number} of {total}"')
            squashed[number - 1] = text.replace(header, "").replace(footer, "")
        if _squash("Change summary") not in squashed[0]:
            problems.append("page 1 is not the change summary")
        if summary_sentence and _squash(summary_sentence) not in squashed[0]:
            problems.append(f'page 1 does not give the counts ("{summary_sentence}")')
        if total < 2:
            problems.append("the PDF has the summary but no blackline pages")
    body = "".join(squashed[1:] if labels is not None else squashed)
    missing = [f for change in changes for f in fragments(change)
               if _squash(f) and _squash(f) not in body]
    if missing:
        problems.append(f"{len(missing)} changed passage(s) are not in the blackline, "
                        f"for example “{_clip(missing[0], 80)}”")
    return problems


def which_engine() -> str:
    """What `render(engine="auto")` will try first here."""
    from secondeye import convert

    return "libreoffice" if convert.available() else "typeset"


__all__ = [
    "DEL", "INS", "MOVE_FROM", "MOVE_TO", "PLAIN", "Labels", "Material", "Para", "Rendered",
    "check", "clause_of", "counts_of", "counts_sentence", "fragments", "libreoffice_pdf",
    "page_texts", "paragraphs", "pdf_text", "render", "short_diff", "summarise", "which_engine",
    "write_pdf",
]
