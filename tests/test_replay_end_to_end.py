"""From the bytes of a real .eml to the reply, with nothing stubbed but the model.

The other end-to-end tests build an InboundEmail by hand, which skips the part
real mail breaks: MIME structure, HTML-only bodies, display names, quoted
threads, a reply that has to find its conversation by Message-ID. These build
the message the way a mail client does, write it out as bytes, and put it
through the same `parse_eml` and `handle` that `lra replay` uses.

There is no API key in CI, so the model call fails for real here rather than
being stubbed, and what each test sees is what a lawyer would get on a day the
model is down. That is a path worth holding to a standard of its own.
"""

from __future__ import annotations

import re
from email.message import EmailMessage
from io import BytesIO

import pytest
from docx import Document

from lra import handler
from lra.mail.console import ConsoleProvider
from lra.pipeline import checks, compare, redline
from lra.pipeline.ooxml import Revision, RevisionWriter

DOCX = ("application", "vnd.openxmlformats-officedocument.wordprocessingml.document")

V1 = [
    "SUPPLY AGREEMENT",
    "1. Term. This Agreement continues for twelve (12) months from the Effective Date.",
    "2. Liability. The Supplier's liability is capped at the fees paid in the prior year.",
    "3. Notices. Notices shall be sent by post to the registered office.",
    "4. Governing law. This Agreement is governed by the laws of New York.",
]
V2 = [
    "SUPPLY AGREEMENT",
    "1. Term. This Agreement continues for thirty-six (36) months from the Effective Date.",
    "2. Liability. The Supplier's liability is capped at the fees paid in the prior year.",
    "2A. Exclusivity. The Buyer shall purchase the Products only from the Supplier.",
    "4. Governing law. This Agreement is governed by the laws of Delaware.",
]


class Replay(ConsoleProvider):
    """The console provider, keeping what it would have printed."""

    def __init__(self):
        self.sent = []

    def send(self, out):
        self.sent.append(out)
        return f"<reply-{len(self.sent)}@agent>"


@pytest.fixture
def replay(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/replay.sqlite3")
    monkeypatch.setenv("MAIL_AGENT_ADDRESS", "review@legal.firm.com")
    monkeypatch.setenv("FIRM_DOMAINS", "firm.com")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("ARCHIVE_ENABLED", "true")
    from lra.config import settings

    settings.cache_clear()
    provider = Replay()

    def run(raw: bytes):
        handler.handle(provider.parse_eml(raw), provider=provider)
        return provider.sent[-1]

    provider.run = run
    yield provider
    settings.cache_clear()


def docx(paragraphs, dirty: bool = False) -> bytes:
    d = Document()
    for text in paragraphs:
        d.add_paragraph(text)
    if dirty:
        d.core_properties.author = "Jane Associate"
        writer = RevisionWriter(d, "Jane Associate")
        writer.apply(Revision("twelve (12)", "eighteen (18)", "Jane Associate"))
        writer.comment("registered office", "whose? ask the client before this goes")
    buf = BytesIO()
    d.save(buf)
    return buf.getvalue()


def eml(body: str, files: dict[str, bytes], *, html: bool = False, message_id="<m1@firm.com>",
        in_reply_to: str | None = None, subject="Supply agreement") -> bytes:
    msg = EmailMessage()
    msg["From"] = "Jim Baker <jim@firm.com>"
    msg["To"] = "Review <review@legal.firm.com>"
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg["Date"] = "Mon, 21 Sep 2026 09:00:00 +0000"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    if html:
        msg.set_content(f"<html><body><div>{body}</div></body></html>", subtype="html")
    else:
        msg.set_content(body)
    for name, content in files.items():
        msg.add_attachment(content, maintype=DOCX[0], subtype=DOCX[1], filename=name)
    return msg.as_bytes()


# --- comparison -----------------------------------------------------------


def test_compare_from_an_html_only_email(replay):
    """Outlook sends HTML with no text part. The word "compare" is in there."""
    out = replay.run(eml("Hi &mdash; can you <b>compare</b> these two? Thanks, Jim",
                         {"Supply v2.docx": docx(V2), "Supply v1.docx": docx(V1)},
                         html=True))

    assert out.to == ["jim@firm.com"]
    assert out.text_body.startswith("4 changes")
    assert "Supply v1.docx as the earlier version" in out.text_body
    # The model is unreachable, and the reply says so rather than going quiet.
    assert "could not assess which changes matter" in out.text_body
    for words in ("thirty-six (36", "Delaware", "only from the Supplier", "registered office"):
        assert words in out.text_body

    [attached] = out.attachments
    assert attached.filename == "Supply v2 (comparison).docx"
    ok, reason = redline.verify(attached.content, expect_revisions=True)
    assert ok, reason
    assert redline.revision_authors(attached.content) == {compare.AUTHOR}
    assert out.html_body and "<li>" in out.html_body


def test_compare_is_not_triggered_by_the_other_sides_words(replay):
    body = ("Please review the attached.\n\n"
            "On Fri, Sep 18, 2026 at 4:02 PM Opposing Counsel <oc@other.com> wrote:\n"
            "> We suggest you compare this against our last turn.\n")
    out = replay.run(eml(body, {"a.docx": docx(V1), "b.docx": docx(V2)}))
    assert "more than one document" in out.text_body
    assert out.attachments == []


def test_compare_to_the_last_version_finds_it_in_the_archive(replay):
    """Two emails, days apart. The second has one attachment and no second file."""
    first = replay.run(eml("Quick look please.", {"Supply agreement.docx": docx(V1)},
                           message_id="<monday@firm.com>"))
    assert first.to == ["jim@firm.com"]

    out = replay.run(eml("They sent it back. Compare this to the last version you saw.",
                         {"Supply agreement v2.docx": docx(V2)},
                         message_id="<friday@firm.com>"))
    assert re.match(r"\d+ changes?", out.text_body), out.text_body
    assert "against the copy I last saw on" in out.text_body
    assert [a.filename for a in out.attachments] == ["Supply agreement v2 (comparison).docx"]


# --- clean copy -----------------------------------------------------------


def test_clean_copy_from_a_fresh_email(replay):
    out = replay.run(eml("Could you send me a clean copy of this for the client?",
                         {"Supply.docx": docx(V1, dirty=True)}))
    assert out.text_body.startswith("Clean copy attached")
    [attached] = out.attachments
    assert attached.filename == "Supply (clean).docx"
    assert checks.leftovers(attached.content) == []
    text = [p.text for p in Document(BytesIO(attached.content)).paragraphs]
    assert "eighteen (18) months" in text[1]
    assert "Jane Associate" in out.text_body and "1 comment" in out.text_body


def test_a_review_then_a_reply_asking_for_a_clean_copy(replay):
    """The reply carries no attachment and has to find its conversation."""
    review = replay.run(eml("Can you check this before it goes?",
                            {"Supply.docx": docx(V1, dirty=True)},
                            message_id="<first@firm.com>"))
    # Model down: the mechanical findings still arrive, and still name the problem.
    assert "tracked changes" in review.text_body
    assert "mechanical checks" in review.text_body

    out = replay.run(eml("Thanks. Please send me a clean copy.", {},
                         message_id="<second@firm.com>", in_reply_to="<reply-1@agent>",
                         subject="Re: Supply agreement"))
    assert out.text_body.startswith("Clean copy attached"), out.text_body
    assert checks.leftovers(out.attachments[0].content) == []


# --- the conversation still works around the new routes --------------------


def test_stop_flagging_is_remembered_even_when_the_model_is_down(replay):
    from lra import memory

    replay.run(eml("Check this please.", {"Supply.docx": docx(V1)},
                   message_id="<a@firm.com>"))
    out = replay.run(eml("Fine, but stop flagging the governing law clause.", {},
                         message_id="<b@firm.com>", in_reply_to="<reply-1@agent>"))
    # A message that is only a suppression never needs the model: it is
    # remembered and said back, so the lawyer sees it learn.
    assert "will not raise the governing law clause" in out.text_body
    block = memory.as_prompt_block(memory.recall("jim@firm.com"))
    assert "governing law clause" in block


def test_every_reply_goes_to_the_sender_and_nobody_else(replay):
    raw = eml("compare please", {"x v1.docx": docx(V1), "x v2.docx": docx(V2)})
    raw = raw.replace(b"To: Review <review@legal.firm.com>",
                      b"To: Review <review@legal.firm.com>\r\nCc: Client <bob@client.com>")
    out = replay.run(raw)
    assert out.to == ["jim@firm.com"] and out.cc == []
