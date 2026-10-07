"""The whole path: inbound email to sent reply with a working redline attached.

The model call itself is stubbed, because it is a thin wrapper over the SDK and
because these tests must run offline in CI. Everything else is real: the same
intake, the same deterministic checks, the same OOXML writer, the same reply
composer, and the same verification gate that runs in production.
"""

from __future__ import annotations

import re
import zipfile
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import pytest
from docx import Document

from lra import handler
from lra.mail.console import ConsoleProvider
from lra.models import Attachment, Finding, InboundEmail, ReviewResult, Severity
from tests.conftest import documents

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class Captured(ConsoleProvider):
    def __init__(self):
        self.sent = []

    def send(self, email_out):
        self.sent.append(email_out)
        return "captured"


@pytest.fixture
def captured(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/e2e.sqlite3")
    monkeypatch.setenv("MAIL_AGENT_ADDRESS", "review@legal.firm.com")
    monkeypatch.setenv("FIRM_DOMAINS", "firm.com")
    from lra.config import settings

    settings.cache_clear()
    provider = Captured()
    monkeypatch.setattr(handler, "get_provider", lambda: provider)
    yield provider
    settings.cache_clear()


def contract() -> bytes:
    """A document with defects a real reviewer would actually find."""
    d = Document()
    d.add_heading("Mutual Non-Disclosure Agreement", 1)
    d.add_paragraph(
        "This Agreement is entered into as of March 3, 2026 by and between "
        'Acme Holdings LLC ("Discloser") and Beta Industries Inc ("Recipient").'
    )
    p = d.add_paragraph("1. Term. This Agreement continues for ")
    p.add_run("thirty").bold = True
    p.add_run(" (13) months from the Effective Date.")
    d.add_paragraph(
        "2. Confidentiality. The Reciever shall protect all Confidential Information."
    )
    d.add_paragraph("4. Governing Law. New York law governs, as set out in Section 7.")
    buf = BytesIO()
    d.save(buf)
    return buf.getvalue()


def inbound(body="Quick look before this goes to the client?") -> InboundEmail:
    raw = contract()
    return InboundEmail(
        message_id="e2e-1",
        from_address="jim@firm.com",
        to=["review@legal.firm.com"],
        subject="Acme / Beta NDA",
        text_body=body,
        attachments=[
            Attachment(filename="Acme NDA.docx", content_type=DOCX,
                       size_bytes=len(raw), content=raw)
        ],
        received_at=datetime.now(UTC),
    )


def stub_agent(*findings):
    """Stand in for the model, returning findings in exactly its output shape."""

    def _review(doc, mode, instructions, **kwargs):
        return ReviewResult(
            mode=mode,
            summary="A short NDA with one drafting error and one loose obligation.",
            findings=list(findings),
        )

    return _review


TYPO = Finding(
    severity=Severity.FORMATTING, category="typo",
    title='"Reciever" is misspelled',
    explanation="It should be Recipient, which is the defined term.",
    anchor="Reciever", suggested_text="Recipient", auto_apply=True,
)
JUDGMENT = Finding(
    severity=Severity.SUBSTANTIVE, category="confidentiality",
    title="The confidentiality obligation has no time limit",
    explanation="Clause 2 binds the Recipient forever. Most counterparties push back.",
    anchor="shall protect all Confidential Information",
    suggested_text="shall protect all Confidential Information for three years",
    auto_apply=False,
)


# --- the happy path -------------------------------------------------------

def test_a_document_comes_back_with_a_working_redline(captured, monkeypatch):
    monkeypatch.setattr(handler.review, "review", stub_agent(TYPO, JUDGMENT))
    handler.handle(inbound())

    assert len(captured.sent) == 1
    out = captured.sent[0]

    # It replied to the sender, and to nobody else.
    assert out.to == ["jim@firm.com"]
    assert out.cc == []

    # It attached a redline, named so it sorts next to the original.
    assert len(documents(out)) == 1
    assert out.attachments[0].filename == "Acme NDA (redline).docx"

    # The attachment is a real Word file with real revision markup.
    content = out.attachments[0].content
    ok, reason = handler.redline.verify(content, expect_revisions=True)
    assert ok, reason
    xml = zipfile.ZipFile(BytesIO(content)).read("word/document.xml").decode()
    assert 'w:author="Reviewer"' in xml
    assert ">Recipient<" in xml


def test_the_verdict_is_the_first_thing_a_phone_shows(captured, monkeypatch):
    monkeypatch.setattr(handler.review, "review", stub_agent(TYPO, JUDGMENT))
    handler.handle(inbound())
    first_line = captured.sent[0].text_body.split("\n")[0]
    assert first_line.startswith(("Don't send yet", "Nearly ready", "Ready to send"))


def test_deterministic_findings_appear_without_the_model_finding_them(captured, monkeypatch):
    """The planted placeholder, bad amount and broken cross-reference are caught
    by the check layer, not by the stubbed agent."""
    monkeypatch.setattr(handler.review, "review", stub_agent())
    handler.handle(inbound())
    body = captured.sent[0].text_body
    assert "Should this be 30 or 13?" in body   # words disagree with numerals
    assert "Section 7" in body                  # cross-reference to a missing section
    assert "2 to 4" in body                     # numbering gap


def test_judgment_calls_are_explained_not_silently_applied(captured, monkeypatch):
    monkeypatch.setattr(handler.review, "review", stub_agent(TYPO, JUDGMENT))
    handler.handle(inbound())
    body = captured.sent[0].text_body
    assert "no time limit" in body
    assert "Your call" in body

    xml = zipfile.ZipFile(
        BytesIO(captured.sent[0].attachments[0].content)
    ).read("word/document.xml").decode()
    assert "for three years" not in xml, "a judgment call was written into the document"


# --- degradation ----------------------------------------------------------

def test_memo_mode_returns_no_attachment(captured, monkeypatch):
    monkeypatch.setattr(handler.review, "review", stub_agent(TYPO))
    handler.handle(inbound(body="Don't edit it, just tell me what's wrong."))
    assert documents(captured.sent[0]) == []


def test_an_unlocatable_suggestion_degrades_to_a_memo_line(captured, monkeypatch):
    """A suggestion whose anchor is not in the document must be raised in the
    email, never guessed at, and must not produce an attachment on its own."""
    ghost = Finding(
        severity=Severity.FORMATTING, category="typo", title="Fix something",
        explanation="", anchor="text that is not in this document at all",
        suggested_text="x", auto_apply=True,
    )
    monkeypatch.setattr(handler.review, "review", stub_agent(ghost))
    raw = Path("samples/clean.docx").read_bytes()
    email = inbound()
    email.message_id = "e2e-ghost"
    email.attachments = [Attachment(filename="Clean.docx", content_type=DOCX,
                                    size_bytes=len(raw), content=raw)]
    handler.handle(email)
    assert documents(captured.sent[0]) == []
    assert "Fix something" in captured.sent[0].text_body


def test_the_original_document_is_never_mutated(captured, monkeypatch):
    monkeypatch.setattr(handler.review, "review", stub_agent(TYPO))
    original = contract()
    email = inbound()
    email.attachments[0].content = original
    handler.handle(email)
    assert email.attachments[0].content == original


def test_a_clean_document_says_so_and_attaches_nothing(captured, monkeypatch):
    monkeypatch.setattr(handler.review, "review", stub_agent())
    raw = Path("samples/clean.docx").read_bytes()
    email = inbound()
    email.message_id = "e2e-clean"
    email.attachments = [Attachment(filename="Clean.docx", content_type=DOCX,
                                    size_bytes=len(raw), content=raw)]
    handler.handle(email)
    body = captured.sent[0].text_body
    assert body.startswith("Ready to send")
    assert documents(captured.sent[0]) == []



# --- the associate loop: questions a lawyer can answer in one word --------

def test_ambiguous_defects_become_questions_rather_than_lectures(captured, monkeypatch):
    """The difference between a review that creates work and one that removes it."""
    monkeypatch.setattr(handler.review, "review", stub_agent())
    handler.handle(inbound())
    body = captured.sent[0].text_body
    assert "Cl" in body or "?" in body
    assert "Should this be 30 or 13?" in body


def test_the_reply_invites_plain_english_not_commands(captured, monkeypatch):
    monkeypatch.setattr(handler.review, "review", stub_agent())
    handler.handle(inbound())
    body = captured.sent[0].text_body
    assert 'Reply "30; 4; clean copy" for a clean copy with your answers in' in body


def test_the_suggested_reply_gets_a_sendable_copy_in_one_round_trip(captured, monkeypatch):
    """Draft in, one reply typed from the footer, clean copy back. It used to
    take two replies, and the first of them went to the model."""
    monkeypatch.setattr(handler.review, "review", stub_agent())

    def no_model(*a, **k):
        raise AssertionError("the model was called")

    monkeypatch.setattr(handler.instruct, "carry_out", no_model)
    handler.handle(inbound())
    suggested = re.search(r'Reply "([^"]+)"', captured.sent[0].text_body).group(1)

    handler.handle(InboundEmail(
        message_id="e2e-2", in_reply_to="captured", from_address="jim@firm.com",
        to=["review@legal.firm.com"], subject="Re: Acme / Beta NDA",
        text_body=suggested, received_at=datetime.now(UTC),
    ))
    assert len(captured.sent) == 2
    out = captured.sent[1]
    assert out.text_body.splitlines()[0] == "Clean copy attached."
    [copy] = out.attachments
    text = " ".join(p.text for p in Document(BytesIO(copy.content)).paragraphs)
    assert "thirty (30) months" in text and "Section 4" in text
    with zipfile.ZipFile(BytesIO(copy.content)) as z:
        assert "w:ins" not in z.read("word/document.xml").decode()


def test_a_question_is_not_also_listed_as_a_blocker(captured, monkeypatch):
    """Saying the same thing twice in one email is how a review starts to feel
    like homework."""
    monkeypatch.setattr(handler.review, "review", stub_agent())
    handler.handle(inbound())
    body = captured.sent[0].text_body
    assert body.count("30 or 13") == 1
