"""A Word document to write tracked changes into, whatever arrived.

The redline is the product (PRODUCT.md: a suggestion that has to be re-keyed is
worth nothing), and until now only a native .docx got one. A PDF, a legacy .doc,
an RTF or a plain-text file got findings in an email and a sentence saying
tracked changes were not possible. They are possible, in a Word file built from
the document, and the lawyer who forwards the PDF the other side returned is
exactly the lawyer who most needs the changes as tracked changes rather than as
a list to retype.

Three routes, in order of how faithful the result is to the original.

**A format Word itself can open** (.doc, .rtf, .odt) is converted by
LibreOffice as a local subprocess, the same way `extract` already reads it. The
layout survives to the extent that Word and LibreOffice agree on it, which for
an ordinary contract is nearly all of it.

**A PDF with a text layer** is rebuilt from the extracted blocks with
python-docx: one paragraph per clause, in a neutral font. The layout is not the
original's and the reply says so. LibreOffice's own PDF import is deliberately
not used: it produces one text frame per printed line, which is a document
nothing can review or redline.

**A plain-text file** becomes paragraphs at the blank lines.

Whatever the route, the text of the result has to match the text the review
was run on closely enough for every finding's anchor to be found again by the
OOXML writer, which normalises whitespace and quotes and nothing else. The
tests hold the built document to that. A scan has no text to build from and is
refused here with a sentence the reply can carry.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from io import BytesIO

from docx import Document
from docx.shared import Pt

from secondeye.models import Attachment
from secondeye.pipeline import extract
from secondeye.pipeline.filetype import SANDBOX_CONVERTIBLE, Kind, identify

log = logging.getLogger(__name__)

# The neutral face for a document rebuilt from text. Not the original's, and
# not pretending to be: the note in the reply says the layout was rebuilt.
_FONT = "Calibri"
_SIZE = Pt(11)

_CONVERTED_NOTE = (
    "The marked-up copy was converted from your .{ext} to Word first. The layout "
    "is preserved where Word and LibreOffice agree on it, so glance at the "
    "formatting before you rely on the copy."
)
_REBUILT_FROM_PDF_NOTE = (
    "The marked-up copy is a Word document built from the PDF's text, so its "
    "layout is not the original's. Accept all the changes for clean text you "
    "can paste back, or reply \"clean copy\"."
)
_REBUILT_FROM_TEXT_NOTE = (
    "The marked-up copy is a Word document built from your text file, so the "
    "tracked changes are in it rather than in the .{ext}."
)
_REBUILT_FROM_SCAN_NOTE = (
    "The marked-up copy is a Word document built from my transcription of the "
    "scanned PDF, so check its wording against the scan before relying on it, "
    "and it has none of the original layout. The mechanical checks "
    "(placeholders, numbering, dates, amounts) did not run on a transcription, "
    "because they would report its slips as the document's; the findings are "
    "from reading the pages."
)
_SCAN = (
    "This PDF is a scan with no text layer, so there was nothing to build a "
    "marked-up copy from. These are notes only, from reading the pages."
)


class CannotConvert(Exception):
    """Carries a sentence we are willing to email back verbatim."""


@dataclass
class Rebuilt:
    content: bytes
    note: str
    # What it was made from: "pdf", "text", or the extension of a converted
    # word-processor file. The reply and the comparison name it.
    source: str


def docx_name(filename: str) -> str:
    """The name the Word copy carries: the original stem, .docx extension."""
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    return f"{stem or filename}.docx"


def convertible(att: Attachment) -> bool:
    """Whether `to_docx` can be expected to produce a Word file for this.

    Cheap, from the bytes: a PDF might still turn out to be a scan, which
    `to_docx` reports when it tries. A deck or a workbook is never convertible:
    a slide is not a paragraph and a tracked change in one is not a thing.
    """
    kind = identify(att.content, att.filename)
    if kind in (Kind.DOCX, Kind.PDF, Kind.TEXT):
        return True
    return kind in SANDBOX_CONVERTIBLE and conversion_available()


def conversion_available() -> bool:
    """Whether a word-processor format can be converted at all here."""
    from secondeye import convert, managed

    return convert.available() or managed.configured()


def to_docx(att: Attachment, doc: extract.ExtractedDoc | None = None) -> Rebuilt:
    """A Word document carrying this attachment's text, and how it was made.

    `doc` is the extraction the review ran on, when the caller has it. The
    rebuilt paragraphs are taken from the same blocks the findings quote, so an
    anchor found in the review can be found in the copy.
    """
    kind = identify(att.content, att.filename)

    if kind is Kind.DOCX:
        return Rebuilt(att.content, "", "docx")

    if kind is Kind.PDF:
        doc = doc or extract.extract(att)
        if not any(b.text.strip() for b in doc.blocks if b.kind != "page-break"):
            raise CannotConvert(_SCAN)
        note = _REBUILT_FROM_SCAN_NOTE if doc.transcribed else _REBUILT_FROM_PDF_NOTE
        return Rebuilt(_build(doc), note, "pdf")

    if kind is Kind.TEXT:
        doc = doc or extract.extract(att)
        if not any(b.text.strip() for b in doc.blocks):
            raise CannotConvert("That text file is empty, so there was nothing to mark up.")
        ext = _extension(att.filename) or "txt"
        return Rebuilt(_build(doc, join_lines=True),
                       _REBUILT_FROM_TEXT_NOTE.format(ext=ext), "text")

    if kind in SANDBOX_CONVERTIBLE:
        ext = _extension(att.filename) or {
            Kind.LEGACY_DOC: "doc", Kind.RTF: "rtf", Kind.ODT: "odt"}.get(kind, "doc")
        return Rebuilt(_via_word_processor(att, ext), _CONVERTED_NOTE.format(ext=ext), ext)

    raise CannotConvert(
        "I cannot put tracked changes into that kind of file, so these are notes only."
    )


def _via_word_processor(att: Attachment, ext: str) -> bytes:
    """LibreOffice locally, the associate's sandbox as the fallback, exactly
    as `extract` reads these formats. Raises CannotConvert with a sentence when
    neither is on offer, which is what the reply then says."""
    from secondeye import convert, managed

    converted = None
    if convert.available():
        try:
            converted = convert.to_docx(att.content, att.filename)
        except convert.ConversionUnavailable as e:
            log.info("local conversion failed for %s: %s", att.filename, e)
    if converted is None and managed.configured():
        try:
            converted = managed.convert_to_docx(att.content, att.filename)
        except managed.JobFailed as e:
            log.info("session conversion unavailable for %s: %s", att.filename, e)
    if converted is None or identify(converted, att.filename) is not Kind.DOCX:
        raise CannotConvert(
            f"I could not convert the .{ext} to Word here, so these are notes "
            "only. Send me the .docx and I will mark it up."
        )
    return converted


def _build(doc: extract.ExtractedDoc, join_lines: bool = False) -> bytes:
    """One paragraph per block, headings kept, pages kept as page breaks.

    `join_lines` is for plain text, where the extractor made one block per
    line: consecutive lines become one paragraph and a blank line ends it. The
    writer collapses whitespace before matching, so an anchor quoted from one
    line is still found inside the joined paragraph.
    """
    document = Document()
    normal = document.styles["Normal"]
    normal.font.name = _FONT
    normal.font.size = _SIZE

    pending: list[str] = []

    def flush() -> None:
        if pending:
            document.add_paragraph(" ".join(pending))
            pending.clear()

    blocks = [b for b in doc.blocks if b.kind != "page-break" or not join_lines]
    last_page_break = max(
        (i for i, b in enumerate(blocks) if b.kind == "page-break"), default=-1)
    for i, block in enumerate(blocks):
        if block.kind == "page-break":
            flush()
            # Not after the last page: a trailing break is an empty final page
            # that the comparison would then count as a paragraph.
            if i != last_page_break:
                document.add_page_break()
            continue
        text = block.text
        if join_lines:
            if text.strip():
                pending.append(text.strip())
            else:
                flush()
            continue
        if not text.strip():
            continue
        if block.kind == "heading":
            document.add_heading(text, level=1)
        else:
            document.add_paragraph(text)
    flush()

    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _extension(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
