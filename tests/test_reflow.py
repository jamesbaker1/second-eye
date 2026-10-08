"""A real redline for every input.

Only a native .docx used to get a marked-up copy; a PDF, a .doc or a text file
got findings in an email and a sentence saying tracked changes were not
possible. They are, in a Word copy converted or built from the document, and
these tests hold that copy to two things: it opens and carries revisions like
any other redline, and its text is close enough to the extraction the review
ran on that every anchor is found again.

The model is stubbed. The PDF is real (reportlab), the Word copy is real, the
annotations are read back with pypdf.
"""

from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO

import pytest
from reportlab.pdfgen import canvas

from secondeye import handler
from secondeye.mail.console import ConsoleProvider
from secondeye.models import Attachment, Finding, InboundEmail, Mode, ReviewResult, Severity
from secondeye.pipeline import annotate, extract, intake, redline, reflow
from secondeye.pipeline.ooxml import _normalize
from tests.conftest import documents

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

BRIEF = (
    "MEMORANDUM OF LAW",
    "1. Introduction. Defendant moves to dismiss the complaint under Rule 12(b)(6),",
    "as further set out in Section 9 of this memorandum.",
    "2. Standard of Review. A complaint must state a claim that is plausible",
    "on its face, and must be signed by [COUNSEL NAME] before filing.",
    "3. Conclusion. The complaint should be dismissed with prejudice.",
)


def pdf_of(*lines: str, pages: int = 1) -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf)
    for _ in range(pages):
        y = 800
        for line in lines:
            c.drawString(60, y, line)
            y -= 20
        c.showPage()
    c.save()
    return buf.getvalue()


def blank_pdf() -> bytes:
    """A page with no text layer: what a scanner produces."""
    buf = BytesIO()
    c = canvas.Canvas(buf)
    c.rect(100, 100, 300, 500)
    c.showPage()
    c.save()
    return buf.getvalue()


def attach(name: str, content: bytes, ctype: str = "application/octet-stream") -> Attachment:
    return Attachment(filename=name, content_type=ctype, size_bytes=len(content),
                      content=content)


class Captured(ConsoleProvider):
    def __init__(self):
        self.sent = []

    def send(self, email_out):
        self.sent.append(email_out)
        return "captured"


@pytest.fixture
def captured(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/reflow.sqlite3")
    monkeypatch.setenv("MAIL_AGENT_ADDRESS", "review@firm.com")
    monkeypatch.setenv("FIRM_DOMAINS", "firm.com")
    from secondeye.config import settings

    settings.cache_clear()
    provider = Captured()
    monkeypatch.setattr(handler, "get_provider", lambda: provider)
    yield provider
    settings.cache_clear()


def email(body: str, *attachments: Attachment, message_id: str = "r-1") -> InboundEmail:
    return InboundEmail(
        message_id=message_id, from_address="jim@firm.com", to=["review@firm.com"],
        subject="Brief", text_body=body, attachments=list(attachments),
        received_at=datetime.now(UTC),
    )


def stub_review(*findings: Finding):
    def _review(doc, mode, instructions, **kwargs):
        return ReviewResult(mode=mode, summary="Looked it over.", findings=list(findings))

    return _review


@pytest.fixture
def only_the_stub(monkeypatch):
    """The deterministic checks find real things in BRIEF (a placeholder, a
    cross-reference to nowhere). Where a test counts annotations, only the
    stubbed findings may be in play."""
    monkeypatch.setattr(handler.checks, "run_all", lambda *a, **k: [])
    monkeypatch.setattr(handler.email_checks, "run_all", lambda *a, **k: [])


def substantive(anchor="Section 9", title="Section 9 does not exist") -> Finding:
    return Finding(severity=Severity.SUBSTANTIVE, category="cross-reference", title=title,
                   explanation="The memorandum has three sections.", anchor=anchor)


def edit() -> Finding:
    return Finding(severity=Severity.STYLE, category="style", title="Wrong court name",
                   explanation="", anchor="dismissed with prejudice",
                   suggested_text="dismissed with prejudice and without leave to amend",
                   auto_apply=True)


# --- the copy carries the text the review saw --------------------------------

def every_block_is_in(source: Attachment, built: bytes) -> None:
    extracted = extract.extract(source)
    copy = extract.extract(attach("copy.docx", built, DOCX))
    haystack = _normalize(" ".join(b.text for b in copy.blocks))
    for block in extracted.blocks:
        if block.kind == "page-break" or not block.text.strip():
            continue
        assert _normalize(block.text) in haystack, block.text


def test_a_pdf_becomes_a_word_document_carrying_every_block():
    source = attach("brief.pdf", pdf_of(*BRIEF), "application/pdf")
    rebuilt = reflow.to_docx(source)
    assert rebuilt.source == "pdf"
    assert "layout is not the original's" in rebuilt.note
    assert redline.verify_opens(rebuilt.content)
    every_block_is_in(source, rebuilt.content)


def test_pages_become_page_breaks_but_not_a_trailing_one():
    source = attach("brief.pdf", pdf_of(*BRIEF, pages=2), "application/pdf")
    rebuilt = reflow.to_docx(source)
    xml = rebuilt.content
    from zipfile import ZipFile

    body = ZipFile(BytesIO(xml)).read("word/document.xml").decode()
    assert body.count('w:type="page"') == 1


def test_a_text_file_becomes_paragraphs_at_the_blank_lines():
    text = ("1. Term. The term is thirty (13) days\nfrom the Effective Date.\n\n"
            "2. Payment. Fees are due on invoice.\n")
    source = attach("notes.txt", text.encode(), "text/plain")
    rebuilt = reflow.to_docx(source)
    copy = extract.extract(attach("copy.docx", rebuilt.content, DOCX))
    paragraphs = [b.text for b in copy.blocks if b.text.strip()]
    assert paragraphs == [
        "1. Term. The term is thirty (13) days from the Effective Date.",
        "2. Payment. Fees are due on invoice.",
    ]
    every_block_is_in(source, rebuilt.content)


def test_a_scan_is_refused_with_a_sentence():
    with pytest.raises(reflow.CannotConvert) as e:
        reflow.to_docx(attach("scan.pdf", blank_pdf(), "application/pdf"))
    assert "scan" in str(e.value)
    assert "notes only" in str(e.value)


def test_a_docx_passes_through_untouched():
    from docx import Document

    d = Document()
    d.add_paragraph("x")
    buf = BytesIO()
    d.save(buf)
    rebuilt = reflow.to_docx(attach("a.docx", buf.getvalue(), DOCX))
    assert rebuilt.content == buf.getvalue() and rebuilt.note == ""


def test_a_legacy_doc_is_convertible_only_where_libreoffice_or_the_sandbox_is():
    from secondeye import convert

    ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 400
    assert reflow.convertible(attach("old.doc", ole)) is (convert.available() or False)
    if not convert.available():
        with pytest.raises(reflow.CannotConvert) as e:
            reflow.to_docx(attach("old.doc", ole))
        assert "Send me the .docx" in str(e.value)


@pytest.mark.skipif(not __import__("secondeye.convert", fromlist=["x"]).available(),
                    reason="LibreOffice is not installed")
def test_a_legacy_doc_is_converted_and_says_where_the_layout_may_differ():
    from secondeye import convert

    d = __import__("docx").Document()
    d.add_paragraph("The Reciever pays thirty (13) days after closing.")
    buf = BytesIO()
    d.save(buf)
    legacy = convert._convert(buf.getvalue(), "a.docx", "doc")
    rebuilt = reflow.to_docx(attach("a.doc", legacy))
    assert rebuilt.source == "doc"
    assert "converted from your .doc" in rebuilt.note
    assert "Reciever" in extract.extract(attach("c.docx", rebuilt.content, DOCX)).as_prompt()


def test_odt_is_recognised_from_its_bytes():
    from zipfile import ZipFile

    from secondeye.pipeline.filetype import Kind, identify

    buf = BytesIO()
    with ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        z.writestr("content.xml", "<office:document-content/>")
    assert identify(buf.getvalue(), "a.odt") is Kind.ODT
    assert identify(buf.getvalue(), "a.docx") is Kind.ODT


# --- through the handler -------------------------------------------------------

def test_a_pdf_gets_a_word_redline_and_an_annotated_pdf(captured, only_the_stub, monkeypatch):
    monkeypatch.setattr(handler.review, "review", stub_review(substantive(), edit()))
    handler.handle(email("Quick look please.",
                         attach("Motion brief.pdf", pdf_of(*BRIEF), "application/pdf")))

    [out] = captured.sent
    assert out.to == ["jim@firm.com"]
    names = [a.filename for a in documents(out)]
    assert names == ["Motion brief (redline).docx", "Motion brief (annotated).pdf"]

    ok, why = redline.verify(out.attachments[0].content, expect_revisions=True)
    assert ok, why
    # The tracked change that was asked for is in the copy.
    assert "Tracked in the attached copy" in out.text_body
    # The copy is described honestly, near the top, and the old sentence is gone.
    body = out.text_body
    assert "built from the PDF's text, so its layout is not the original's" in body
    assert "cannot put tracked changes" not in body
    assert body.index("built from the PDF's text") < body.index("Tracked in the attached copy")

    # The PDF carries one note per located finding; nothing was a blocker, so
    # nothing is highlighted.
    marked = out.attachments[1].content
    assert annotate.count_annotations(marked) == 2
    assert "each finding as a note beside the text" in body
    assert "Nothing is changed in it" in body


def test_a_blocker_is_highlighted_on_the_pdf_and_instructions_can_change_that(captured, only_the_stub,
                                                                              monkeypatch):
    blocker = Finding(severity=Severity.BLOCKER, category="placeholder",
                      title="[COUNSEL NAME] left in", explanation="Still a placeholder.",
                      anchor="[COUNSEL NAME]")
    monkeypatch.setattr(handler.review, "review", stub_review(blocker, substantive()))
    pdf = pdf_of(*BRIEF)

    handler.handle(email("Quick look.", attach("b.pdf", pdf, "application/pdf"),
                         message_id="r-default"))
    handler.handle(email("Highlight everything please.",
                         attach("b.pdf", pdf, "application/pdf"), message_id="r-all"))
    handler.handle(email("Notes please, no highlights.",
                         attach("b.pdf", pdf, "application/pdf"), message_id="r-none"))

    def annotated(out):
        return next(a for a in out.attachments if a.filename.endswith(".pdf")).content

    # Two notes plus one highlight; two notes plus two; two notes only.
    assert annotate.count_annotations(annotated(captured.sent[0])) == 3
    assert annotate.count_annotations(annotated(captured.sent[1])) == 4
    assert annotate.count_annotations(annotated(captured.sent[2])) == 2
    assert "the blockers highlighted" in captured.sent[0].text_body


def test_an_anchor_not_on_the_page_gets_no_note_and_stays_in_the_email(captured, only_the_stub, monkeypatch):
    monkeypatch.setattr(handler.review, "review", stub_review(
        substantive(anchor="unicorns and rainbows", title="Nowhere to be found")))
    handler.handle(email("Look.", attach("b.pdf", pdf_of(*BRIEF), "application/pdf")))
    [out] = captured.sent
    assert "Nowhere to be found" in out.text_body
    assert not any(a.filename.endswith(".pdf") for a in out.attachments)


def test_a_text_file_gets_a_word_redline(captured, monkeypatch):
    monkeypatch.setattr(handler.review, "review", stub_review(edit()))
    text = "3. Conclusion. The complaint should be dismissed with prejudice.\n"
    handler.handle(email("Check this.", attach("notes.txt", text.encode(), "text/plain")))
    [out] = captured.sent
    assert [a.filename for a in documents(out)] == ["notes (redline).docx"]
    ok, why = redline.verify(out.attachments[0].content, expect_revisions=True)
    assert ok, why
    assert "built from your text file" in out.text_body


def test_a_scan_gets_notes_only_and_says_so_plainly(captured, monkeypatch):
    seen = {}

    def _review(doc, mode, instructions, **kwargs):
        seen["mode"] = mode
        return ReviewResult(mode=mode, summary="Read the pages.", findings=[substantive()])

    monkeypatch.setattr(handler.review, "review", _review)
    handler.handle(email("Look.", attach("scan.pdf", blank_pdf(), "application/pdf")))
    [out] = captured.sent
    assert documents(out) == []
    assert seen["mode"] is Mode.MEMO_ONLY
    body = out.text_body
    assert "scan with no text layer" in body
    # Not the words we put in the lawyer's mouth for a requested memo, and not
    # a claim about safety that was never tested.
    assert "you asked me not to edit" not in body
    assert "none of this was safe" not in body


def test_asking_for_a_memo_still_gives_no_attachment_for_a_pdf(captured, monkeypatch):
    monkeypatch.setattr(handler.review, "review", stub_review(edit()))
    handler.handle(email("Memo only please, no redline.",
                         attach("b.pdf", pdf_of(*BRIEF), "application/pdf")))
    [out] = captured.sent
    assert documents(out) == []
    assert "Notes only, as you asked" in out.text_body


def test_the_conversation_holds_the_word_copy_so_undo_can_rebuild_it(captured, monkeypatch):
    """The thread's document is what "undo" and "add a clause" rebuild the
    redline from, and a PDF cannot be rebuilt into."""
    from secondeye import thread

    monkeypatch.setattr(handler.review, "review", stub_review(edit()))
    handler.handle(email("Look.", attach("Motion brief.pdf", pdf_of(*BRIEF), "application/pdf")))
    [(key, filename)] = thread.recent_for_owner("jim@firm.com")
    state = thread.load(key)
    assert filename == "Motion brief.docx"
    assert intake.is_redlineable(attach(filename, state.original))
    content, landed, _ = thread.rebuild(state)
    assert landed and redline.verify(content, expect_revisions=True)[0]


def test_a_failure_building_the_copy_never_fails_the_review(captured, monkeypatch):
    monkeypatch.setattr(handler.review, "review", stub_review(substantive()))
    monkeypatch.setattr(handler.reflow, "to_docx",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    handler.handle(email("Look.", attach("b.pdf", pdf_of(*BRIEF), "application/pdf")))
    [out] = captured.sent
    assert "Section 9 does not exist" in out.text_body
    assert "could not build a copy to mark up" in out.text_body
    assert documents(out) == []


def test_the_annotator_caps_the_size_of_pdf_it_will_read(monkeypatch):
    monkeypatch.setattr(annotate, "MAX_BYTES", 10)
    result = annotate.annotate(pdf_of(*BRIEF), [substantive()])
    assert result.content is None
    assert "too large" in result.notes[0]


def test_the_annotator_caps_the_number_of_pages(monkeypatch):
    monkeypatch.setattr(annotate, "MAX_PAGES", 1)
    result = annotate.annotate(pdf_of(*BRIEF, pages=2), [substantive()])
    assert result.content is None
    assert "pages" in result.notes[0]


def test_an_anchor_appearing_twice_is_not_annotated():
    """Same rule as the writer: a note beside the wrong one of two identical
    phrases is worse than none."""
    result = annotate.annotate(pdf_of(*BRIEF, pages=2), [substantive()])
    assert result.content is None and result.placed == 0
