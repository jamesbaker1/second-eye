"""Dismissing a point in one line: "B is fine".

Every finding a review shows that is not a tracked change carries a letter.
Fast dismissal is the defence against false positives (PRODUCT.md): a point
the lawyer cannot wave away in three words is one they learn to stop reading.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from lra import d1, followup, handler, memory, thread
from lra.models import Finding, Mode, ReviewResult, Severity
from lra.pipeline import reply
from tests import test_followup as tf
from tests.test_followup import TYPO, _plan, first_email, reply_email, run_first

# What undos, dismissals and the sent version teach: LEARN_FROM_OUTCOMES on.
pytestmark = pytest.mark.usefixtures("learning")

captured = tf.captured    # the follow-up fixtures: a provider that keeps what is sent
no_model = tf.no_model


def _state(_captured) -> thread.ThreadState:
    return thread.load(thread.resolve("jim@firm.com", "thread-1", "x", "agent-msg-1", []))


# --- the review letters what it shows ------------------------------------

def test_every_point_that_is_not_a_change_has_a_letter(captured, monkeypatch):
    run_first(captured, monkeypatch)
    body = captured.sent[0].text_body
    assert "  A. Section 1: 'thirty (13)' disagrees with itself." in body
    assert "  B. Section 2: Section 7 does not exist." in body
    assert "  C. Author metadata identifies python-docx." in body
    # A tracked change keeps its number and gets no letter.
    assert "  1. “Reciever” → “Recipient”" in body
    assert body.count('"A is fine" to dismiss a point') == 1
    # Not a one-tap link: pressing the example would dismiss a point unchosen.
    assert "A%20is%20fine" not in captured.sent[0].html_body
    assert [f.label for f in _state(captured).flagged] == ["A", "B", "C"]


def test_nothing_to_dismiss_means_no_footer_clause():
    result = ReviewResult(mode=Mode.REDLINE, summary="", findings=[TYPO])
    out = reply.compose(first_email(), result, b"x", "a.docx", [TYPO], [],
                        numbers=[1], labels=[])
    assert "to dismiss" not in out.text_body


def test_a_footer_of_its_own_when_there_is_nothing_else_to_reply():
    nit = Finding(severity=Severity.STYLE, category="style", title="Double space",
                  explanation="", anchor="x")
    result = ReviewResult(mode=Mode.REDLINE, summary="", findings=[nit])
    out = reply.compose(first_email(), result, None, "a.docx", [], [], labels=["A"])
    assert "  A. Double space." in out.text_body
    assert out.text_body.rstrip().endswith('Reply "A is fine" to dismiss a point.')


def test_a_point_seen_again_keeps_its_letter(captured, monkeypatch):
    run_first(captured, monkeypatch)
    before = {f.title: f.label for f in _state(captured).flagged}
    handler.handle(first_email().model_copy(update={"message_id": "first-2"}))
    state = _state(captured)
    assert {f.title: f.label for f in state.flagged} == before
    assert set(state.labelled) == {"A", "B", "C"}


# --- dismissing -----------------------------------------------------------

def test_b_is_fine_is_noted_in_one_line(captured, monkeypatch, no_model):
    run_first(captured, monkeypatch)
    handler.handle(reply_email("B is fine", "r2"))
    out = captured.sent[1]
    assert out.text_body.strip() == (
        "Noted: B is fine (Reference to Section 7, which does not exist).\n\n"
        "Privileged & Confidential — Attorney Work Product")
    assert out.attachments == []
    state = _state(captured)
    assert [f.label for f in state.flagged if f.dismissed] == ["B"]
    # Its question is settled with it.
    assert len(state.open_questions) == 1
    assert "30 or 13" in state.open_questions[0].question
    assert "do not raise them again" in state.summary()


def test_two_at_once_with_a_sign_off(captured, monkeypatch, no_model):
    run_first(captured, monkeypatch)
    handler.handle(reply_email("A and c are fine\n\nThanks\nJim", "r2"))
    assert captured.sent[1].text_body.startswith("Noted: A and C are fine (A: ")
    assert {f.label for f in _state(captured).flagged if f.dismissed} == {"A", "C"}


def test_a_dismissed_blocker_is_not_warned_about_in_the_clean_copy(captured, monkeypatch,
                                                                    no_model):
    run_first(captured, monkeypatch)
    handler.handle(reply_email("A is fine; 2; clean copy", "r2"))
    assert len(captured.sent) == 2
    out = captured.sent[1]
    assert out.text_body.splitlines()[0] == "Clean copy attached."
    assert "Noted: 2." in out.text_body and "Noted: A is fine" in out.text_body
    assert "30 or 13" not in out.text_body
    assert _state(captured).open_questions == []


def test_a_dismissed_question_is_not_still_waited_on(captured, monkeypatch, no_model):
    run_first(captured, monkeypatch)
    handler.handle(reply_email("B is fine; 30", "r2"))
    out = captured.sent[1]
    assert "Noted: 30." in out.text_body
    assert "Still waiting on" not in out.text_body


def test_a_letter_that_names_nothing_asks_and_does_nothing(captured, monkeypatch, no_model):
    run_first(captured, monkeypatch)
    handler.handle(reply_email("B and Z are fine; 30; clean copy", "r2"))
    out = captured.sent[1]
    assert out.text_body.startswith("Which one? There is no point Z in my last review")
    assert "not done the rest of your reply" in out.text_body
    assert out.attachments == []
    state = _state(captured)
    assert not any(f.dismissed for f in state.flagged)
    assert len(state.open_questions) == 2


def test_undo_with_a_letter_asks_rather_than_reversing_the_only_change(captured,
                                                                       monkeypatch):
    """With one change, "undo" plus a word it cannot match used to mean that
    change. B is a point, not a change."""
    run_first(captured, monkeypatch)
    handler.handle(reply_email("undo B", "r2"))
    out = captured.sent[1]
    assert out.text_body.startswith("Which one? B is a point I raised")
    assert len(_state(captured).active_changes) == 1


# --- reading the words ----------------------------------------------------

@pytest.mark.parametrize("text,letters", [
    ("B is fine", ["B"]),
    ("b is fine.", ["B"]),
    ("ignore B", ["B"]),
    ("dismiss C please", ["C"]),
    ("B and D are fine", ["B", "D"]),
    ("B, D and E are all fine", ["B", "D", "E"]),
    ("point B is intended", ["B"]),
    ("ok, B is fine as it is", ["B"]),
])
def test_the_ways_of_saying_it(text, letters):
    assert followup.dismissed_letters(text) == letters


@pytest.mark.parametrize("text", [
    "B is fine but tighten clause 4",
    "that is fine",
    "the cap is fine",
    "30",
    "fine",
    "ignore the indemnity",
])
def test_what_is_not_a_dismissal(text):
    assert followup.dismissed_letters(text) is None


def _flagged_state():
    return thread.ThreadState(
        id="t", owner="j@f.com", filename="a.docx",
        questions=[thread.Question(id=1, question="q", anchor="a", options=["30", "13"],
                                   answered_with=None, round=0)],
        flagged=[thread.Flagged(id=i, label=x, severity="blocker", category="c",
                                title=f"t{x}", anchor="", question=None, current=True,
                                dismissed=False) for i, x in enumerate("AB")],
    )


def test_a_dismissal_composes_with_the_rest():
    plan = _plan(_flagged_state(), "B is fine; 30; clean copy")
    assert plan.dismiss.labels == ["B"] and not plan.dismiss.ask
    assert [v for _, v in plan.answers] == ["30"] and plan.clean


def test_a_dismissal_beside_an_instruction_goes_to_the_agent_whole():
    assert _plan(_flagged_state(), "B is fine; make the cap five million") is None


def test_a_letter_from_an_earlier_review_is_not_nameable():
    state = _flagged_state()
    state.flagged[0].current = False
    assert _plan(state, "A is fine").dismiss.ask


# --- learning from it -----------------------------------------------------

@dataclass
class Shown:
    category: str
    severity: str = "style"
    title: str = "A point"
    anchor: str = ""


def test_five_dismissals_of_a_kind_stop_it_being_raised_and_say_so_once(captured):
    for _ in range(4):
        memory.record_dismissals("job", "Jim@Firm.com", None, [Shown("passive-voice")])
    assert memory.next_announcement("jim@firm.com") is None
    memory.record_dismissals("job", "jim@firm.com", None, [Shown("passive-voice")])
    entry_id, line = memory.next_announcement("jim@firm.com")
    assert line == ("I've stopped flagging passive-voice points for you, since you "
                    'dismissed 5 of them. Reply "flag it again" to have them back.')
    assert "passive-voice" in memory.as_prompt_block(memory.recall("jim@firm.com"))
    memory.mark_announced(entry_id)

    assert memory.lift_last_announced("jim@firm.com") == "passive-voice points"
    memory.record_dismissals("job", "jim@firm.com", None, [Shown("passive-voice")])
    assert "passive-voice" not in memory.as_prompt_block(memory.recall("jim@firm.com"))
    assert memory.next_announcement("jim@firm.com") is None


def test_dismissals_and_undos_are_counted_apart(captured):
    for _ in range(5):
        memory.record_dismissals("job", "jim@firm.com", None, [Shown("date")])
    rate, total = memory.rejection_rate("jim@firm.com", "date")
    assert (rate, total) == (0.0, 0)
    assert "Usually undoes" not in memory.as_prompt_block(memory.recall("jim@firm.com"))


def test_a_dismissal_through_the_handler_is_recorded(captured, monkeypatch, no_model):
    run_first(captured, monkeypatch)
    handler.handle(reply_email("dismiss C", "r2"))
    with memory.connect() as c:
        rows = c.execute("SELECT category, outcome FROM suggestion_outcomes").fetchall()
    assert [r[1] for r in rows] == ["dismissed"]


# --- storage --------------------------------------------------------------

def test_a_database_from_before_letters_gains_the_table(captured, monkeypatch):
    run_first(captured, monkeypatch)
    with thread.connect() as c:
        c.execute("DROP TABLE thread_findings")
    # As a process that last ran the old schema: a new schema text is applied
    # afresh on both backends, which is what creates the table.
    thread._migrated.clear()
    d1._applied_scripts.clear()
    state = _state(captured)
    assert state.flagged == []
    assert thread.record_findings(state.id, 1, [TYPO]) == ["A"]


def test_purge_takes_the_letters_with_the_thread(captured, monkeypatch):
    run_first(captured, monkeypatch)
    thread.purge("jim@firm.com")
    with thread.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM thread_findings").fetchone()[0] == 0
