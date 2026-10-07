"""The deadlines as a calendar file: attached to a review with two or more
dated deadlines, and sent on request. Only what the document dates goes in;
the periods that run from an undated event are named in the email instead."""

from __future__ import annotations

import datetime
from datetime import UTC
from datetime import datetime as dt

import pytest

from evals import realistic
from lra.models import Attachment, InboundEmail, ReviewResult
from lra.pipeline import extract, ics, intake

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def docx_doc(content: bytes, name: str = "Falcon SPA.docx"):
    return extract.extract(Attachment(filename=name, content_type=DOCX,
                                      size_bytes=len(content), content=content))


def text_doc(*paragraphs):
    raw = "\n".join(paragraphs).encode()
    return extract.extract(Attachment(filename="t.txt", content_type="text/plain",
                                      size_bytes=len(raw), content=raw))


@pytest.fixture(scope="module")
def falcon_clean():
    return docx_doc(realistic.falcon_spa(3))


# --- The calendar -------------------------------------------------------------


NOW = dt(2026, 9, 28, 9, 30, tzinfo=UTC)


def test_the_calendar_is_rfc_5545(falcon_clean):
    found = ics.read(falcon_clean)
    assert [(e.day, e.what, e.where) for e in found.dated] == [
        (datetime.date(2026, 10, 30), "Completion Date", "clause 1.1"),
        (datetime.date(2026, 12, 31), "Long Stop Date", "clause 1.1")]
    made = ics.attachment(found, "Falcon SPA v3.docx", now=NOW)
    assert made.filename == "Falcon SPA v3 (deadlines).ics"
    assert made.content_type == "text/calendar"
    raw = made.content.decode("utf-8")
    assert raw.startswith("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:")
    assert raw.endswith("END:VCALENDAR\r\n")
    assert "\n" not in raw.replace("\r\n", "")
    assert all(len(line.encode()) <= 75 for line in raw.split("\r\n"))
    unfolded = raw.replace("\r\n ", "")
    assert unfolded.count("BEGIN:VEVENT") == 2 == unfolded.count("TRIGGER:-P7D")
    assert "DTSTART;VALUE=DATE:20261231\r\nDTEND;VALUE=DATE:20270101" in unfolded
    assert "SUMMARY:Long Stop Date (clause 1.1) - Falcon SPA v3" in unfolded
    assert "DTSTAMP:20260928T093000Z" in unfolded
    # The same deadline gives the same UID, so a second import updates it.
    again = ics.attachment(found, "Falcon SPA v3.docx", now=NOW).content
    assert again == made.content


def test_text_is_escaped():
    assert ics._text("a, b; c\\d\ne") == "a\\, b\\; c\\\\d\\ne"


def test_undated_periods_are_listed_and_never_invented(falcon_clean):
    found = ics.read(falcon_clean)
    said = ics.note(found, "Falcon SPA v3 (deadlines).ics")
    assert said.startswith("Falcon SPA v3 (deadlines).ics puts the 2 dated deadlines")
    assert "Clause 5.1: Retention period, 18 months after Completion" in said
    raw = ics.calendar(found.dated, "Falcon.docx", NOW).decode()
    assert "Retention" not in raw and "20260901" not in raw     # nor the agreement's date


def test_one_dated_deadline_is_not_worth_a_file():
    doc = text_doc("This Agreement is dated 1 September 2026.",
                   "1. “Long Stop Date” means 31 December 2026.")
    assert ics.attachment(ics.read(doc), "x.docx") is None
    assert ics.attachment(ics.read(doc), "x.docx", minimum=1) is not None


@pytest.mark.parametrize("words,asked", [
    ("Send me the deadlines as a calendar", True),
    ("can you put the key dates in my diary?", True),
    ("Add the deadlines to my calendar please", True),
    ("ics please", True),
    ("Check the dates are calendar days, not business days", False),
    ("Is 30 calendar days right in clause 4?", False),
])
def test_asking_for_a_calendar(words, asked):
    email = InboundEmail(message_id="m", from_address="jim@firm.com", to=["r@x.com"],
                         subject="Falcon", text_body=words, received_at=dt.now(UTC))
    assert intake.wants_calendar(email) is asked


# --- Through the handler --------------------------------------------------------


@pytest.fixture
def captured(monkeypatch, tmp_path):
    from lra import handler
    from lra.config import settings
    from lra.mail.console import ConsoleProvider

    class Captured(ConsoleProvider):
        def __init__(self):
            self.sent = []

        def send(self, email_out):
            self.sent.append(email_out)
            return f"<sent-{len(self.sent)}@test>"

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/d.sqlite3")
    monkeypatch.setenv("MAIL_AGENT_ADDRESS", "review@legal.firm.com")
    monkeypatch.setenv("FIRM_DOMAINS", "firm.com")
    settings.cache_clear()
    provider = Captured()
    monkeypatch.setattr(handler, "get_provider", lambda: provider)
    monkeypatch.setattr(handler.review, "review", lambda doc, mode, *a, **k: ReviewResult(
        mode=mode, summary="", findings=[]))
    yield provider
    settings.cache_clear()


def _email(n, content, body="Quick look?", filename="Falcon SPA.docx", **kw):
    attachments = [Attachment(filename=filename, content_type=DOCX, size_bytes=len(content),
                              content=content)] if content else []
    return InboundEmail(message_id=f"<f-{n}@firm.com>", from_address="jim@firm.com",
                        to=["review@legal.firm.com"], subject="Falcon SPA", text_body=body,
                        attachments=attachments, received_at=dt.now(UTC), **kw)


def test_a_review_attaches_the_deadlines_calendar(captured):
    from lra import handler

    handler.handle(_email(1, realistic.falcon_spa(3)))
    out = captured.sent[0]
    names = [a.filename for a in out.attachments]
    assert "Falcon SPA (deadlines).ics" in names
    assert "Falcon SPA (deadlines).ics puts the 2 dated deadlines in your calendar" \
        in out.text_body
    assert "18 months after Completion" in out.text_body


def test_the_calendar_on_request_for_the_attached_document(captured):
    from lra import handler

    handler.handle(_email(1, realistic.falcon_spa(3), body="Send me the deadlines as a calendar"))
    out = captured.sent[0]
    assert [a.filename for a in out.attachments] == ["Falcon SPA (deadlines).ics"]
    assert out.text_body.startswith("2 dated deadlines from Falcon SPA.docx")
    assert "31 December 2026: Long Stop Date (clause 1.1)" in out.text_body
    assert "Clause 6.1: Time limit for claims, within 24 months after Completion" \
        in out.text_body


def test_the_calendar_on_request_on_a_review_thread(captured):
    from lra import handler

    handler.handle(_email(1, realistic.falcon_spa(3)))
    first = captured.sent[0]
    handler.handle(_email(2, None, body="thanks - can you send me the deadlines as a calendar?",
                          in_reply_to="<f-1@firm.com>", references=["<f-1@firm.com>"]))
    assert len(captured.sent) == 2, first.text_body
    assert [a.filename for a in captured.sent[1].attachments] == ["Falcon SPA (deadlines).ics"]
