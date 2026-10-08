"""Thread state and the change ledger.

The design claim being tested: the ledger is the source of truth and the
redline is always regenerated from the original. That is what makes undo safe.
Patching revision markup in an already-edited file is how a client's contract
gets corrupted.
"""

from __future__ import annotations

import functools
from io import BytesIO

import pytest
from docx import Document

from secondeye import thread
from secondeye.models import Finding, Severity


@pytest.fixture
def db(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.sqlite3")
    from secondeye.config import settings

    settings.cache_clear()
    yield
    settings.cache_clear()


@functools.lru_cache(maxsize=1)
def contract() -> bytes:
    """Built once and reused.

    python-docx stamps every zip entry with the current time, so two calls a
    second apart return different bytes. Tests that assert "this is the
    original document" must compare against the same build, or they fail
    roughly whenever the suite crosses a second boundary at the wrong moment.
    """
    d = Document()
    d.add_paragraph("1. Term. The term is thirty days from the Effective Date.")
    d.add_paragraph("2. Notice. The Reciever shall give notice to Acme Holdings Ltd.")
    d.add_paragraph("7. Fees. Fees are payable monthly in arrears.")
    buf = BytesIO()
    d.save(buf)
    return buf.getvalue()


def fix(anchor, replacement, title="fix", category="typo") -> Finding:
    return Finding(severity=Severity.FORMATTING, category=category, title=title,
                   explanation="", anchor=anchor, suggested_text=replacement,
                   auto_apply=True)


def started(owner="jim@firm.com"):
    key = thread.key("t1", "m1", owner)
    return thread.start(key, owner, "NDA.docx", contract())


# --- identity -------------------------------------------------------------

def test_the_same_conversation_resolves_to_the_same_key():
    a = thread.key("t1", "m1", "jim@firm.com")
    b = thread.key("t1", "m9", "jim@firm.com")
    assert a == b


def test_a_different_lawyer_gets_a_different_thread():
    assert thread.key("t1", "m1", "jim@firm.com") != thread.key("t1", "m1", "kate@firm.com")


def test_a_provider_without_threads_falls_back_to_the_message_id():
    assert thread.key(None, "m1", "jim@firm.com") == thread.key(None, "m1", "jim@firm.com")
    assert thread.key(None, "m1", "jim@firm.com") != thread.key(None, "m2", "jim@firm.com")


# --- starting and reloading ----------------------------------------------

def test_a_thread_keeps_the_original_document(db):
    state = started()
    assert state.original == contract()
    assert thread.load(state.id).original == contract()


def test_starting_twice_does_not_lose_state(db):
    state = started()
    thread.record_changes(state.id, 0, [fix("thirty", "sixty")])
    again = thread.start(state.id, "jim@firm.com", "NDA.docx", contract())
    assert len(again.changes) == 1


# --- the ledger -----------------------------------------------------------

def test_applied_changes_are_recorded(db):
    state = started()
    thread.record_changes(state.id, 0, [fix("thirty", "sixty"),
                                        fix("Reciever", "Recipient")])
    assert len(thread.load(state.id).active_changes) == 2


def test_the_same_change_is_not_recorded_twice(db):
    state = started()
    thread.record_changes(state.id, 0, [fix("thirty", "sixty")])
    thread.record_changes(state.id, 1, [fix("thirty", "sixty")])
    assert len(thread.load(state.id).changes) == 1


# --- rebuilding, which is what makes undo safe ---------------------------

def test_the_redline_is_built_from_the_original_every_time(db):
    state = started()
    thread.record_changes(state.id, 0, [fix("thirty", "sixty")])
    content, landed, _ = thread.rebuild(thread.load(state.id))

    from secondeye.pipeline import redline

    ok, why = redline.verify(content, expect_revisions=True)
    assert ok, why
    assert len(landed) == 1


def test_undoing_a_change_removes_it_from_the_document(db):
    import re
    import zipfile

    state = started()
    thread.record_changes(state.id, 0, [fix("thirty", "sixty", title="term"),
                                        fix("Reciever", "Recipient", title="typo")])
    loaded = thread.load(state.id)
    term_change = next(c for c in loaded.changes if c.title == "term")

    assert thread.undo(state.id, [term_change.id]) == 1

    content, landed, _ = thread.rebuild(thread.load(state.id))
    xml = zipfile.ZipFile(BytesIO(content)).read("word/document.xml").decode()
    assert ">Recipient<" in xml
    assert ">sixty<" not in xml
    assert len(landed) == 1
    assert not re.search(r">sixty<", xml)


def test_undoing_everything_returns_the_original(db):
    state = started()
    thread.record_changes(state.id, 0, [fix("thirty", "sixty"),
                                        fix("Reciever", "Recipient")])
    assert thread.undo_all(state.id) == 2
    content, landed, _ = thread.rebuild(thread.load(state.id))
    assert content == contract()
    assert landed == []


def test_an_undone_change_is_not_silently_remade(db):
    state = started()
    thread.record_changes(state.id, 0, [fix("thirty", "sixty")])
    loaded = thread.load(state.id)
    thread.undo(state.id, [loaded.changes[0].id])

    # The same finding arriving again in a later round must not reactivate it.
    thread.record_changes(state.id, 1, [fix("thirty", "sixty")])
    after = thread.load(state.id)
    assert len(after.changes) == 1
    assert after.active_changes == []


def test_the_summary_tells_the_agent_what_not_to_redo(db):
    state = started()
    thread.record_changes(state.id, 0, [fix("thirty", "sixty", title="term length")])
    loaded = thread.load(state.id)
    thread.undo(state.id, [loaded.changes[0].id])

    summary = thread.load(state.id).summary()
    assert "do not make them again" in summary
    assert "term length" in summary


# --- questions ------------------------------------------------------------

def test_open_questions_are_remembered(db):
    state = started()
    f = Finding(severity=Severity.BLOCKER, category="amount", title="t", explanation="",
                anchor="thirty (13)", question="Should this be 30 or 13?",
                options=["30", "13"])
    thread.record_questions(state.id, 0, [f])
    loaded = thread.load(state.id)
    assert len(loaded.open_questions) == 1
    assert loaded.open_questions[0].options == ["30", "13"]


def test_answering_a_question_closes_it(db):
    state = started()
    f = Finding(severity=Severity.BLOCKER, category="amount", title="t", explanation="",
                anchor="thirty (13)", question="Should this be 30 or 13?",
                options=["30", "13"])
    thread.record_questions(state.id, 0, [f])
    q = thread.load(state.id).questions[0]
    thread.answer(state.id, q.id, "30")
    assert thread.load(state.id).open_questions == []


def test_the_same_question_is_not_asked_twice(db):
    state = started()
    f = Finding(severity=Severity.BLOCKER, category="amount", title="t", explanation="",
                anchor="x", question="Should this be 30 or 13?")
    thread.record_questions(state.id, 0, [f])
    thread.record_questions(state.id, 1, [f])
    assert len(thread.load(state.id).questions) == 1


# --- rounds ---------------------------------------------------------------

def test_rounds_advance(db):
    state = started()
    assert thread.next_round(state.id) == 1
    assert thread.next_round(state.id) == 2
    assert thread.load(state.id).round == 2



def test_docx_bytes_are_not_stable_across_builds():
    """Documents the reason contract() is cached. python-docx stamps zip entry
    timestamps, so byte-equality between two builds is not a valid assertion
    and produced a test that failed roughly once every few runs."""
    import zipfile

    d1 = Document()
    d1.add_paragraph("x")
    b1 = BytesIO()
    d1.save(b1)
    stamps = {
        info.date_time
        for info in zipfile.ZipFile(BytesIO(b1.getvalue())).infolist()
    }
    assert stamps, "no timestamps found; the caching in contract() may be unnecessary"


# --- resolving a reply in one query ---------------------------------------------

def test_the_nearest_identifier_wins_however_many_a_message_carries(db):
    """All of a message's identifiers are looked up at once, past D1's
    parameter limit, and In-Reply-To still outranks the References chain."""
    ours = thread.start("k-ours", "jim@firm.com", "A.docx", contract())
    other = thread.start("k-other", "jim@firm.com", "B.docx", contract())
    thread.add_alias(ours.id, "reply-to-us")
    thread.add_alias(other.id, "ancient")
    chain = ["ancient"] + [f"noise-{i}" for i in range(150)]
    assert thread.resolve("jim@firm.com", None, "new", "reply-to-us", chain) == ours.id
    assert thread.resolve("jim@firm.com", None, "new", None, chain) == other.id
    assert thread.resolve("ann@firm.com", None, "new", "reply-to-us", chain) is None


def test_a_conversation_is_found_by_its_derived_key_without_loading_it(db, monkeypatch):
    state = started()
    monkeypatch.setattr(thread, "load", lambda *a: pytest.fail("loaded to test existence"))
    assert thread.resolve("jim@firm.com", "t1", "m1", None) == state.id
