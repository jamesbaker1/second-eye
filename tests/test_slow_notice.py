"""A slow review says, once, that it is under way; a quick one says nothing.

The Workflow sends the notice (cloudflare/test/flow.test.ts holds when: once,
before the verdict, never after it). What is held here is what the container
decides: its words, whether one is owed at all, and that a review run in one
process sends none.
"""

from __future__ import annotations

import time

from secondeye import handler, notice
from secondeye.pipeline import identity
from tests import test_end_to_end as e2e
from tests.test_end_to_end import inbound, stub_agent

captured = e2e.captured  # the end-to-end fixture: a provider that keeps what is sent


def test_the_wording_is_a_duration_rounded_up():
    assert notice.wording("Acme NDA.docx", 239) == "Reviewing Acme NDA.docx; back in about 4 minutes."
    assert notice.wording("x.docx", 20) == "Reviewing x.docx; back in about 1 minute."
    assert notice.wording("x.docx", -5) == "Reviewing x.docx; back in about 1 minute."


def test_the_notice_is_a_reply_in_the_lawyers_conversation():
    out = notice.compose(inbound(), "Acme NDA.docx", 300)
    assert out.to == ["jim@firm.com"]
    assert out.in_reply_to == "e2e-1"
    assert out.subject == "Re: Acme / Beta NDA"
    assert not out.attachments


def test_one_is_owed_only_for_a_model_backed_review_somebody_is_waiting_on(monkeypatch):
    monkeypatch.setattr(handler, "_model_backed", lambda: True)
    assert handler.notice_delay(identity.EntryMode.FORWARD) == handler.SLOW_NOTICE_AFTER
    # A document already sent: an audit of what went out, nobody waiting.
    assert handler.notice_delay(identity.EntryMode.BCC_SENT) is None
    monkeypatch.setattr(handler, "_model_backed", lambda: False)
    assert handler.notice_delay(identity.EntryMode.FORWARD) is None


def test_a_review_in_one_process_sends_the_verdict_and_nothing_before_it(captured, monkeypatch):
    """The timer thread that sent it from the container went with the queue."""
    monkeypatch.setattr(handler, "_model_backed", lambda: True)
    monkeypatch.setattr(handler, "SLOW_NOTICE_AFTER", 0.01)
    quick = stub_agent()

    def slow(*a, **k):
        time.sleep(0.1)
        return quick(*a, **k)

    monkeypatch.setattr(handler.review, "review", slow)
    handler.handle(inbound())
    assert len(captured.sent) == 1
    assert not hasattr(notice, "SlowReviewNotice") and not hasattr(notice, "Guarded")
