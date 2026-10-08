"""Local conversion, and the independent round trip.

Every test here skips cleanly when LibreOffice is absent, because it is not a
hard dependency: without it the affected formats are refused with a helpful
sentence, which is the behaviour that shipped before.

CI installs libreoffice-writer, so the round-trip tests run there. They are the
closest thing to "does this open in a real word processor" that runs offline.
"""

from __future__ import annotations

from io import BytesIO

import pytest
from docx import Document

from secondeye import convert
from secondeye.models import Finding, Mode, ReviewResult, Severity
from secondeye.pipeline import redline
from secondeye.pipeline.ooxml import Revision, RevisionWriter

needs_soffice = pytest.mark.skipif(
    not convert.available(), reason="LibreOffice is not installed"
)


def save(d) -> bytes:
    buf = BytesIO()
    d.save(buf)
    return buf.getvalue()


def edited(text="The term is thirty days.", anchor="thirty", new="sixty") -> bytes:
    d = Document()
    d.add_paragraph(text)
    doc = Document(BytesIO(save(d)))
    RevisionWriter(doc, "Legal Review Agent").apply(
        Revision(anchor, new, "Legal Review Agent")
    )
    return save(doc)


# --- graceful absence -----------------------------------------------------

def test_the_check_passes_when_libreoffice_is_absent():
    """It is a CI gate, not a runtime dependency. Absent means skipped, never
    a failure that blocks a review."""
    ok, why = convert.opens_in_libreoffice(edited())
    assert ok, why


def test_converting_without_libreoffice_raises_recoverably():
    if convert.available():
        pytest.skip("LibreOffice is installed")
    with pytest.raises(convert.ConversionUnavailable):
        convert.to_docx(b"x", "old.doc")


# --- the round trip that closes the honest gap ---------------------------

@needs_soffice
def test_a_redline_opens_in_an_independent_word_processor():
    """Until this existed, our output had only ever been parsed by the library
    that wrote it."""
    ok, why = convert.opens_in_libreoffice(edited())
    assert ok, why


@needs_soffice
def test_a_redline_with_several_changes_opens():
    d = Document()
    d.add_paragraph("The Reciever pays thirty (13) days after closing.")
    doc = Document(BytesIO(save(d)))
    writer = RevisionWriter(doc, "Legal Review Agent")
    writer.apply(Revision("Reciever", "Recipient", "Legal Review Agent"))
    writer.apply(Revision("thirty (13) days", "thirty (30) days", "Legal Review Agent"))
    ok, why = convert.opens_in_libreoffice(save(doc))
    assert ok, why


@needs_soffice
def test_a_document_with_comments_opens():
    content = save(Document())
    d = Document()
    d.add_paragraph("1. Term. The term is thirty (13) months.")
    content = save(d)
    out = redline.apply(content, ReviewResult(mode=Mode.REDLINE, summary="", findings=[
        Finding(severity=Severity.BLOCKER, category="amount", title="t",
                explanation="", anchor="thirty (13)", question="30 or 13?"),
    ]))
    ok, why = convert.opens_in_libreoffice(out.content)
    assert ok, why


@needs_soffice
def test_a_redline_inside_a_table_opens():
    d = Document()
    t = d.add_table(rows=1, cols=2)
    t.rows[0].cells[0].text = "Term"
    t.rows[0].cells[1].text = "thirty days"
    doc = Document(BytesIO(save(d)))
    RevisionWriter(doc, "Legal Review Agent").apply(
        Revision("thirty days", "sixty days", "Legal Review Agent")
    )
    ok, why = convert.opens_in_libreoffice(save(doc))
    assert ok, why


@needs_soffice
def test_deliberately_broken_markup_is_caught_by_the_round_trip():
    """The gate is worthless if it accepts anything. A file with malformed XML
    must fail to parse."""
    import zipfile

    src = zipfile.ZipFile(BytesIO(edited()))
    items = {n: src.read(n) for n in src.namelist()}
    items["word/document.xml"] = items["word/document.xml"].replace(b"</w:body>", b"")
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
        for name, data in items.items():
            out.writestr(name, data)
    ok, _ = convert.opens_in_libreoffice(buf.getvalue())
    assert not ok


# --- local conversion replaces the off-box path --------------------------

@needs_soffice
def test_an_rtf_is_converted_locally():
    rtf = rb"{\rtf1\ansi This Agreement is made on 3 March 2026.\par}"
    converted = convert.to_docx(rtf, "old.rtf")
    from secondeye.pipeline.filetype import Kind, identify

    assert identify(converted, "old.docx") is Kind.DOCX
    assert "Agreement" in "\n".join(
        p.text for p in Document(BytesIO(converted)).paragraphs
    )
