"""The review with no client attached, the only way a review runs.

docs/migration.md, phases 1 and 5. The session is started and left alone; `finish`
polls it until it stops, reads why, and reads the files it left. What is ours
is exercised here against a fake of the platform as a poller sees it: which
stop means what, the one correction when the findings file is missing or
bad, the indexing lag, the time budget, the notes, and leaving nothing behind.
"""

from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import patch

import pytest
from docx import Document

from secondeye import managed
from secondeye.config import settings
from secondeye.memory import pending_notes, recall
from secondeye.models import Attachment, Mode
from secondeye.pipeline import extract, review
from tests import fake_sessions as fs

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

BLOCKER = {"severity": "blocker", "category": "amount", "title": "Mismatch",
           "explanation": "Words and figure disagree.", "anchor": "thirty (13)",
           "suggested_text": "thirty (30)", "auto_apply": True}


@pytest.fixture(autouse=True)
def configured(monkeypatch, tmp_path):
    fs.configure(monkeypatch, DATABASE_URL=f"sqlite:///{tmp_path}/d.sqlite3")
    yield
    settings.cache_clear()


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """Polling without waiting: every sleep moves a fake clock on."""
    now = [0.0]
    monkeypatch.setattr(managed.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(managed.time, "sleep", lambda s: now.__setitem__(0, now[0] + s))
    return now


def a_document(filename="a.docx") -> tuple[extract.ExtractedDoc, bytes]:
    d = Document()
    d.add_paragraph("1. Term. The term is thirty (13) months.")
    buf = BytesIO()
    d.save(buf)
    raw = buf.getvalue()
    return extract.extract(Attachment(filename=filename, content_type=DOCX,
                                      size_bytes=len(raw), content=raw)), raw


def recorded(*findings, summary="") -> bytes:
    return json.dumps({"summary": summary, "findings": list(findings)}).encode()


def done(outputs=None, reason="end_turn", polls=1, events=()) -> fs.Turn:
    return fs.Turn(*events, fs.idle(reason), outputs=outputs, polls=polls)


def run(turns, mode=Mode.REDLINE, lag=0, **kwargs):
    fake = fs.DetachedSessions(turns, lag=lag)
    doc, raw = a_document()
    with patch.object(managed, "anthropic_client", lambda: fake.client):
        result = review.review(doc, mode, "", user_address="jim@firm.com",
                               original=raw, **kwargs)
    return result, fake


def user_messages(fake) -> list[str]:
    return [e["content"][0]["text"] for batch in fake.sent for e in batch
            if e["type"] == "user.message"]


# --- starting it -------------------------------------------------------------

def test_the_detached_reviewer_is_started_with_the_detached_rubric_and_no_tools_to_answer():
    _, fake = run([done({"findings.json": recorded(BLOCKER)})])
    [create] = fake.created
    assert create["agent"] == "agent_review"
    [outcome] = create["initial_events"]
    assert outcome["type"] == "user.define_outcome"
    assert "validate_findings.py" in outcome["rubric"]["content"]
    assert "report_findings" not in outcome["description"]
    assert "--findings /mnt/session/outputs/findings.json" in outcome["description"]
    assert create["metadata"] == {"lra": "review", "mode": "redline", "their_paper": "no"}
    assert not [e for batch in fake.sent for e in batch
                if e["type"] == "user.custom_tool_result"], "nothing was asked, nothing answered"


def test_the_uploads_are_deleted_as_soon_as_the_session_exists():
    """The session mounts its own copy, and whoever finishes it may be a
    process that never knew the upload ids."""
    fake = fs.DetachedSessions([done()])
    doc, raw = a_document()
    session_id = review.start(doc, Mode.REDLINE, "", original=raw, client=fake.client)
    assert session_id == "sesn_fake"
    assert fake.deleted == ["file_1"]
    assert fake.retrieves == 0, "start returns without waiting"


# --- finishing it ------------------------------------------------------------

def test_a_finished_review_brings_back_the_findings_the_redline_and_the_notes():
    notes = json.dumps({"notes": [{"statement": "Jim writes 'will'.", "scope": "personal"}]})
    result, fake = run([done({
        "findings.json": recorded(BLOCKER, summary="Context."),
        "a (redline).docx": b"PK-redline",
        "notes.json": notes.encode(),
    })])
    assert result.summary == "Context."
    assert [f.title for f in result.findings] == ["Mismatch"]
    assert result.outputs == [("a (redline).docx", b"PK-redline")]
    assert result.cut_short is False and result.session_id == "sesn_fake"
    assert [e.statement for e in pending_notes("jim@firm.com")] == ["Jim writes 'will'."]
    assert recall("jim@firm.com") == [], "a note from a review waits for the lawyer"
    assert fake.deleted_sessions == ["sesn_fake"]
    assert set(fake.deleted) >= {"out_0", "out_1", "out_2"}, "its files do not outlive it"
    assert not user_messages(fake), "a good file needs no correction"


def test_it_waits_while_the_session_runs_and_beats_the_heartbeat_on_every_poll():
    """The heartbeat is `second-eye review --live`'s progress, one dot a poll."""
    beats = []
    fake = fs.DetachedSessions([done({"findings.json": recorded()}, polls=4)])
    doc, raw = a_document()
    session_id = review.start(doc, Mode.REDLINE, "", original=raw, client=fake.client)
    result = review.finish(session_id, Mode.REDLINE, user_address="jim@firm.com",
                           heartbeat=lambda: beats.append(1), client=fake.client)
    assert result.findings == []
    # Five polls, each with a beat, and one more read before deleting: a
    # session is deleted only once its status says it has stopped.
    assert len(beats) == 5 and fake.retrieves == 6


def test_the_outputs_are_read_through_the_indexing_lag():
    result, _ = run([done({"findings.json": recorded(BLOCKER)})], lag=2)
    assert [f.title for f in result.findings] == ["Mismatch"]


def test_a_clean_document_is_an_empty_list_not_a_failure():
    result, _ = run([done({"findings.json": recorded()})])
    assert result.findings == [] and result.outputs == []


# --- one correction, then degrade as today ------------------------------------

def test_a_missing_findings_file_gets_one_correction_and_the_answer_counts():
    result, fake = run([done(),
                        done({"findings.json": recorded(BLOCKER)})])
    [asked] = user_messages(fake)
    assert "findings.json does not exist" in asked and "validate_findings.py" in asked
    assert [f.title for f in result.findings] == ["Mismatch"]


def test_a_file_that_fails_the_schema_is_named_back_to_the_agent_once():
    bad = dict(BLOCKER, severity="critical")
    with pytest.raises(RuntimeError, match="never recorded findings we could read"):
        run([done({"findings.json": recorded(bad)}),
             done({"findings.json": recorded(bad)}),
             done({"findings.json": recorded(BLOCKER)})])
    # Only one correction: the third turn, which would have fixed it, never ran.


def test_the_correction_says_what_was_wrong_in_the_validators_words():
    bad = dict(BLOCKER, severity="critical")
    fake = fs.DetachedSessions([done({"findings.json": recorded(bad)}),
                                done({"findings.json": recorded(BLOCKER)})])
    doc, raw = a_document()
    with patch.object(managed, "anthropic_client", lambda: fake.client):
        result = review.review(doc, Mode.REDLINE, "", user_address="jim@firm.com",
                               original=raw)
    [asked] = user_messages(fake)
    assert asked.startswith("NOTHING WAS RECORDED") and "critical" in asked
    assert "validate_findings.py" in asked and "report_findings" not in asked
    assert [f.severity.value for f in result.findings] == ["blocker"]


def test_a_correction_nobody_answers_ends_in_the_honest_failure():
    """A lost message looks like a session that stays idle on the old stop.
    It must not be read as the answer to the correction."""
    with pytest.raises(RuntimeError, match="never recorded findings"):
        run([done()])


def test_a_file_that_is_not_json_is_corrected_too():
    result, fake = run([done({"findings.json": b"{nope"}),
                        done({"findings.json": recorded()})])
    assert "not JSON" in user_messages(fake)[0]
    assert result.findings == []


# --- how it stopped -----------------------------------------------------------

def test_a_budget_stop_with_a_draft_recorded_is_a_partial_review():
    """The prompt asks for a draft early for exactly this. A session paused at
    its budget accepts no new message, so no correction is attempted."""
    result, fake = run([done({"findings.json": recorded(BLOCKER)}, reason="budget_reached")])
    assert result.cut_short is True
    assert [f.title for f in result.findings] == ["Mismatch"]
    assert not user_messages(fake)


def test_a_budget_stop_with_nothing_recorded_fails_as_today():
    with pytest.raises(RuntimeError, match="out of budget"):
        run([done(reason="budget_reached")])


def test_exhausted_retries_fall_back_to_the_mechanical_findings_even_with_a_draft():
    """A platform failure mid-review: the draft may be anything, and the
    handler's mechanical-only reply is the honest answer."""
    with pytest.raises(RuntimeError, match="exhausted its retries"):
        run([done({"findings.json": recorded(BLOCKER)}, reason="retries_exhausted",
                  events=[fs.error("model overloaded")])])


def test_a_refusal_fails_the_job_rather_than_sending_an_empty_review():
    with pytest.raises(RuntimeError, match="declined"):
        run([done(events=[fs.error("The model refused this request.")])])


def test_a_session_waiting_on_a_client_that_does_not_exist_is_a_failure():
    with pytest.raises(RuntimeError, match="client that does not exist"):
        run([done({"findings.json": recorded()}, reason="requires_action")])


def test_a_session_past_the_time_budget_is_interrupted_and_its_draft_stands(monkeypatch):
    monkeypatch.setenv("AGENT_TIME_BUDGET_SECONDS", "60")
    settings.cache_clear()
    slow = fs.Turn(fs.idle(), outputs={"findings.json": recorded(BLOCKER)}, polls=10_000)
    fake = fs.DetachedSessions([slow])
    # The draft was recorded before the clock ran out.
    fake.files["findings.json"] = recorded(BLOCKER)
    doc, raw = a_document()
    with patch.object(managed, "anthropic_client", lambda: fake.client):
        result = review.review(doc, Mode.REDLINE, "", user_address="jim@firm.com",
                               original=raw)
    assert {"type": "user.interrupt"} in [e for batch in fake.sent for e in batch]
    assert result.cut_short is True
    assert [f.title for f in result.findings] == ["Mismatch"]
    assert not user_messages(fake)


def test_a_session_past_the_time_budget_with_nothing_recorded_fails_as_today(monkeypatch):
    monkeypatch.setenv("AGENT_TIME_BUDGET_SECONDS", "60")
    settings.cache_clear()
    with pytest.raises(RuntimeError, match="out of time"):
        run([fs.Turn(fs.idle(), polls=10_000)])


def test_a_session_that_will_not_stop_is_abandoned_and_still_cleaned_up(monkeypatch):
    monkeypatch.setenv("AGENT_TIME_BUDGET_SECONDS", "60")
    settings.cache_clear()
    fake = fs.DetachedSessions([fs.Turn(fs.idle(), polls=10_000, on_interrupt=None)])
    doc, raw = a_document()
    fake.files["draft.docx"] = b"a copy of the document"
    with patch.object(managed, "anthropic_client", lambda: fake.client), \
            pytest.raises(RuntimeError, match="out of time"):
        review.review(doc, Mode.REDLINE, "", user_address="jim@firm.com", original=raw)
    interrupts = [e for batch in fake.sent for e in batch if e["type"] == "user.interrupt"]
    assert len(interrupts) == 2, "once at the budget, once more on the way out"
    # Deleting a running session is a 400, so it is not attempted; what it
    # wrote is still deleted.
    assert fake.deleted_sessions == []
    assert "out_0" in fake.deleted


# --- the notes -----------------------------------------------------------------

def test_notes_break_nothing_when_they_break_the_rules():
    notes = json.dumps({"notes": [{"statement": "Standard here.", "scope": "firm"}]})
    result, _ = run([done({"findings.json": recorded(), "notes.json": notes.encode()})])
    assert result.findings == []
    assert pending_notes("jim@firm.com") == []


def test_a_notes_file_that_is_nonsense_does_not_fail_the_review():
    result, _ = run([done({"findings.json": recorded(BLOCKER), "notes.json": b"\x00\xff"})])
    assert [f.title for f in result.findings] == ["Mismatch"]
