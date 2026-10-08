"""PDF review.

Previously this branch raised, told the lawyer to reply "review it anyway", and
raised again on that reply. There was no sequence of replies that produced a
review, on a document type litigators send constantly.

Uses pypdfium2 rather than PyMuPDF. PyMuPDF is the more obvious choice and is
AGPL, which is a real problem for a commercial product.
"""

from __future__ import annotations

from io import BytesIO

import pytest
from reportlab.pdfgen import canvas

from secondeye.models import Attachment, Mode
from secondeye.pipeline import checks, extract, intake


def pdf_of(*lines: str) -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf)
    y = 800
    for line in lines:
        c.drawString(60, y, line)
        y -= 20
    c.save()
    return buf.getvalue()


def att(content: bytes, name="brief.pdf") -> Attachment:
    return Attachment(filename=name, content_type="application/pdf",
                      size_bytes=len(content), content=content)


BRIEF = (
    "MEMORANDUM OF LAW",
    "1. Introduction. Defendant moves to dismiss the complaint under Rule 12(b)(6),",
    "as further set out in Section 9 of this memorandum.",
    "2. Standard of Review. A complaint must state a claim that is plausible",
    "on its face, and must be signed by [COUNSEL NAME] before filing.",
    "4. Conclusion. The complaint should be dismissed with prejudice.",
)


def test_a_pdf_is_reviewed_rather_than_refused():
    doc = extract.extract(att(pdf_of(*BRIEF)))
    assert any("Introduction" in b.text for b in doc.blocks)


def test_wrapped_lines_are_rejoined_into_clauses():
    """PDF text breaks at the visual line, so a clause arrives as fragments.
    Left alone, every cross-reference check reads across a break that is not
    in the document."""
    doc = extract.extract(att(pdf_of(*BRIEF)))
    intro = next(b.text for b in doc.blocks if b.text.startswith("1."))
    assert "Section 9" in intro, "the clause was left split across lines"


def test_defects_in_a_pdf_are_found():
    content = pdf_of(*BRIEF)
    doc = extract.extract(att(content))
    categories = {f.category for f in checks.run_all(doc, content)}
    assert {"placeholder", "cross-reference", "numbering"} <= categories


def test_a_pdf_is_no_longer_memo_only():
    """The changes go into a Word copy built from its text; see test_reflow.py."""
    assert intake.mode_for(att(pdf_of("Some text.")), Mode.REDLINE) is Mode.REDLINE


def test_a_scan_is_still_reviewable_because_the_model_can_read_it():
    """A PDF with no text layer used to be refused. The model reads pages
    directly, so the review degrades to a substantive one rather than nothing."""
    buf = BytesIO()
    c = canvas.Canvas(buf)
    c.rect(100, 100, 200, 200, fill=1)      # drawing only, no text
    c.save()
    doc = extract.extract(att(buf.getvalue()))
    assert doc.native is not None
    assert doc.native[1] == "application/pdf"
    assert not any(b.text.strip() for b in doc.blocks if b.kind != "page-break")


def test_a_pdf_is_handed_to_the_model_directly_as_well_as_extracted():
    """Text extraction flattens layout, tables and signature pages. The model
    sees the original too."""
    doc = extract.extract(att(pdf_of(*BRIEF)))
    assert doc.native is not None
    assert doc.native[0][:4] == b"%PDF"
    assert any("Introduction" in b.text for b in doc.blocks)


def test_a_docx_is_not_sent_natively():
    """Only formats the model reads better than our extractor. A .docx is
    extracted precisely, and its bytes would just cost tokens."""
    from docx import Document as Docx

    d = Docx()
    d.add_paragraph("A clause.")
    buf = BytesIO()
    d.save(buf)
    doc = extract.extract(Attachment(
        filename="a.docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=buf.tell(), content=buf.getvalue()))
    assert doc.native is None


def test_a_damaged_pdf_is_reported_not_crashed():
    with pytest.raises(extract.Unreadable) as e:
        extract.extract(att(b"%PDF-1.7\nthis is truncated nonsense"))
    assert "would not open" in str(e.value) or "no text layer" in str(e.value)


def test_page_boundaries_are_marked():
    buf = BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(60, 800, "Page one text here.")
    c.showPage()
    c.drawString(60, 800, "Page two text here.")
    c.save()
    doc = extract.extract(att(buf.getvalue()))
    assert sum(1 for b in doc.blocks if b.kind == "page-break") == 2


def test_the_rejection_no_longer_promises_a_command_that_does_not_exist():
    from secondeye.pipeline.filetype import Kind, message

    assert "review it anyway" not in message(Kind.PDF)
