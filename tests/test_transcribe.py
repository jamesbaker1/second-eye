"""Scanned PDFs: the model transcribes the pages, so a Word copy can be built.

The owner's decision is that reading goes through the model, not an OCR
engine. The model call is stubbed here; the tests pin what the transcription
buys, what it must never feed, and what happens without a model to call.
"""

from __future__ import annotations

import pytest

from secondeye.models import Attachment
from secondeye.pipeline import checks, extract, reflow
from tests.test_reflow import blank_pdf, pdf_of

SCAN_TEXT = "1. Term. The term is thirty (13) days.\n\n2. Law. English law applies."


def scan() -> Attachment:
    content = blank_pdf()
    return Attachment(filename="scan.pdf", content_type="application/pdf",
                      size_bytes=len(content), content=content)


@pytest.fixture
def model_reads(monkeypatch):
    """A model available, transcribing every page as SCAN_TEXT."""
    monkeypatch.setattr(extract, "transcription_available", lambda: True)
    monkeypatch.setattr(extract, "_transcribe", lambda pdf, n: [SCAN_TEXT] * n)


def test_a_scan_is_transcribed_by_the_model_and_says_so(model_reads):
    doc = extract.extract(scan())
    assert doc.transcribed is True
    texts = [b.text for b in doc.blocks if b.kind != "page-break"]
    assert any("thirty (13) days" in t for t in texts)


def test_the_deterministic_checks_stay_silent_on_a_transcription(model_reads):
    """"thirty (13)" is a planted defect the checks catch on real text. In a
    transcription it may be the model's slip, and a check that is wrong on
    scans is the check that gets the product filtered."""
    doc = extract.extract(scan())
    assert checks.run_all(doc) == []


def test_a_scan_becomes_a_word_copy_with_an_honest_note(model_reads):
    doc = extract.extract(scan())
    rebuilt = reflow.to_docx(scan(), doc)
    assert rebuilt.content[:2] == b"PK"
    assert "transcription" in rebuilt.note and "did not run" in rebuilt.note
    copy = extract.extract(Attachment(filename="c.docx", content_type="", size_bytes=1,
                                      content=rebuilt.content))
    assert any("thirty (13) days" in b.text for b in copy.blocks)


def test_without_a_model_a_scan_is_what_it_was(monkeypatch):
    monkeypatch.setattr(extract, "transcription_available", lambda: False)
    doc = extract.extract(scan())
    assert doc.transcribed is False
    assert not any(b.text.strip() for b in doc.blocks if b.kind != "page-break")
    with pytest.raises(reflow.CannotConvert, match="scan"):
        reflow.to_docx(scan(), doc)


def test_a_pdf_with_a_text_layer_is_not_transcribed(model_reads, monkeypatch):
    monkeypatch.setattr(extract, "_transcribe",
                        lambda pdf, n: (_ for _ in ()).throw(AssertionError("model was called")))
    content = pdf_of("1. Term. Thirty days.")
    doc = extract.extract(Attachment(filename="t.pdf", content_type="application/pdf",
                                     size_bytes=len(content), content=content))
    assert doc.transcribed is False


def test_a_very_long_scan_is_not_transcribed(model_reads, monkeypatch):
    monkeypatch.setattr(extract, "TRANSCRIBE_MAX_PAGES", 0)
    assert extract.extract(scan()).transcribed is False


def test_the_transcription_is_laid_back_onto_its_pages():
    two = extract._split_pages("=== PAGE 1 ===\nFirst.\n=== PAGE 2 ===\nSecond.", 2)
    assert two == ["First.", "Second."]
    # No markers: the words are kept, on the first page.
    assert extract._split_pages("All of it.", 3) == ["All of it.", "", ""]
    # A marker beyond the page count is not a new page.
    assert extract._split_pages("=== PAGE 1 ===\nA\n=== PAGE 9 ===\nB", 1) == ["A"]
