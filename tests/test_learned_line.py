"""A review says, once, what it has learned to stop doing for this lawyer, and
"flag it again" takes it back."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from secondeye import handler, memory
from secondeye.models import InboundEmail
from secondeye.pipeline import router
from tests import test_end_to_end as e2e
from tests.test_end_to_end import inbound, stub_agent

# What undos, dismissals and the sent version teach: LEARN_FROM_OUTCOMES on.
pytestmark = pytest.mark.usefixtures("learning")

captured = e2e.captured  # the end-to-end fixture: a provider that keeps what is sent


@dataclass
class Undone:
    category: str
    title: str = "A change"


def undo_five(user="jim@firm.com", category="date-format"):
    for _ in range(5):
        memory.record_rejections("job", user, None, [Undone(category)])


def test_a_habit_learned_from_undos_is_announced_once(captured):
    undo_five()
    entry_id, line = memory.next_announcement("Jim@Firm.com")
    assert line.startswith(
        "I've stopped making date-format corrections for you, since you undid 5 of them")
    assert 'Reply "flag it again"' in line
    memory.mark_announced(entry_id)
    assert memory.next_announcement("jim@firm.com") is None
    assert memory.next_announcement("bob@firm.com") is None


def test_a_request_confirmed_in_its_own_reply_is_not_announced_again(captured):
    memory.remember_suppression("jim@firm.com", "stop flagging the Oxford comma",
                                announced=True)
    assert memory.next_announcement("jim@firm.com") is None


def test_a_request_that_rode_on_an_instruction_is_announced(captured):
    memory.remember_suppression("jim@firm.com", "stop flagging shall")
    _, line = memory.next_announcement("jim@firm.com")
    assert line == ("I've stopped flagging shall for you, as you asked. "
                    'Reply "flag it again" to have it back.')


def test_flag_it_again_lifts_what_was_last_announced_and_it_stays_lifted(captured):
    undo_five()
    entry_id, _ = memory.next_announcement("jim@firm.com")
    memory.mark_announced(entry_id)

    assert memory.lift_last_announced("jim@firm.com") == "date-format corrections"
    assert "date-format" not in memory.as_prompt_block(memory.recall("jim@firm.com"))
    # The undos behind it still count, and one more does not learn it again
    # or announce it again.
    memory.record_rejections("job", "jim@firm.com", None, [Undone("date-format")])
    assert "date-format" not in memory.as_prompt_block(memory.recall("jim@firm.com"))
    assert memory.next_announcement("jim@firm.com") is None
    assert memory.lift_last_announced("jim@firm.com") is None


def test_the_exact_phrase_is_routed():
    assert router.route_deterministic("Flag it again.").intent is router.Intent.UNSUPPRESS
    # Anything longer is not this.
    assert router.route_deterministic(
        "flag it again and tighten the indemnity") is None


def test_a_database_from_before_announcements_gains_the_column(captured):
    """What the lawyer asked for was confirmed to them when they asked; what
    was learned from their undos never was, so only that is announced."""
    memory.init()
    with memory.connect() as c:
        c.execute("DROP TABLE memory")
        c.execute("CREATE TABLE memory (id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT "
                  "NULL, scope_key TEXT NOT NULL, kind TEXT NOT NULL, statement TEXT NOT NULL, "
                  "confidence REAL NOT NULL, samples INTEGER NOT NULL DEFAULT 1, provenance "
                  "TEXT, status TEXT NOT NULL DEFAULT 'confirmed', created_at TEXT, "
                  "updated_at TEXT)")
        c.execute("INSERT INTO memory (scope, scope_key, kind, statement, confidence, "
                  "provenance) VALUES ('personal', 'jim@firm.com', 'suppression', "
                  "'Asked not to be told about this again: \"stop flagging shall\"', 1.0, "
                  "'{\"source\": \"reply\", \"message\": \"m1\"}')")
        c.execute("INSERT INTO memory (scope, scope_key, kind, statement, confidence, "
                  "samples, provenance) VALUES ('personal', 'jim@firm.com', 'suppression', "
                  "'Usually undoes changes of the kind “numbering”. Raise them as a note "
                  "rather than editing.', 0.8, 5, '{\"source\": \"undo\", \"rate\": 0.8}')")
    memory._migrated.clear()
    _, line = memory.next_announcement("jim@firm.com")
    assert line.startswith("I've stopped making numbering corrections for you, "
                           "since you undid 4 of them")


# --- through the handler ------------------------------------------------------

def test_the_next_review_says_it_once_and_flag_it_again_takes_it_back(captured, monkeypatch):
    monkeypatch.setattr(handler.review, "review", stub_agent())
    undo_five()

    handler.handle(inbound())
    first = captured.sent[0]
    assert first.text_body.count("I've stopped making date-format corrections") == 1
    assert first.html_body.count("I&#x27;ve stopped making date-format corrections") == 1
    # And the reply it asks for is one tap.
    assert "body=flag%20it%20again" in first.html_body

    handler.handle(inbound().model_copy(update={"message_id": "e2e-again"}))
    assert "stopped making" not in captured.sent[1].text_body

    handler.handle(InboundEmail(
        message_id="e2e-flag", in_reply_to="captured", from_address="jim@firm.com",
        to=["review@legal.firm.com"], subject="Re: Acme / Beta NDA",
        text_body="Flag it again", received_at=datetime.now(UTC),
    ))
    assert captured.sent[2].text_body.startswith(
        "Done. I will raise date-format corrections again from your next review on.")
    assert "date-format" not in memory.as_prompt_block(memory.recall("jim@firm.com"))


def test_a_line_that_did_not_arrive_is_said_next_time(captured, monkeypatch):
    monkeypatch.setattr(handler.review, "review", stub_agent())
    undo_five()
    sent = captured.send

    def refuse(out):
        raise RuntimeError("provider down")

    monkeypatch.setattr(captured, "send", refuse)
    handler.handle(inbound())
    monkeypatch.setattr(captured, "send", sent)
    handler.handle(inbound().model_copy(update={"message_id": "e2e-retry"}))
    assert "I've stopped making date-format corrections" in captured.sent[-1].text_body
