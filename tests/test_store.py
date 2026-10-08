"""Job persistence: claiming a message, and keeping the retention promise.

Two behaviours are worth a test of their own. A webhook retry that arrives
while the first delivery is still running must not produce a second review,
and it is the concurrent case that used to slip through -- the sequential one
always passed. And the retention window has to be applied by something that
actually runs, rather than by a command nobody types.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest

from secondeye import store
from secondeye.config import settings


@pytest.fixture
def db(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/jobs.sqlite3")
    settings.cache_clear()
    yield
    settings.cache_clear()


def test_only_one_of_many_simultaneous_deliveries_claims_the_message(db):
    """The race the separate seen()/record() pair could not win.

    Providers retry, FastAPI runs the handler in a threadpool, so two
    deliveries of the same message_id are genuinely concurrent. A SELECT
    followed by an INSERT let all of them through; the UNIQUE constraint then
    collapsed the rows, so the database looked right while eight full reviews
    ran and eight replies went out.
    """
    workers = 8
    start = threading.Barrier(workers)
    results: list[bool] = []
    lock = threading.Lock()

    def attempt(n: int) -> None:
        start.wait(timeout=10)
        won = store.claim(f"job-{n}", "msg-same", "jim@firm.com")
        with lock:
            results.append(won)

    threads = [threading.Thread(target=attempt, args=(n,)) for n in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert results.count(True) == 1, results
    assert len(results) == workers


def test_a_claimed_message_keeps_its_first_job_id(db):
    assert store.claim("job-first", "msg-1", "jim@firm.com") is True
    assert store.claim("job-second", "msg-1", "jim@firm.com") is False
    with store.connect() as c:
        row = c.execute("SELECT id FROM jobs WHERE message_id = ?", ("msg-1",)).fetchone()
    assert row[0] == "job-first"


def _age(message_id: str, hours: int) -> None:
    old = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    with store.connect() as c:
        c.execute("UPDATE jobs SET created_at = ? WHERE message_id = ?", (old, message_id))


def test_an_expired_job_row_is_deleted_by_the_next_inbound_message(db, monkeypatch):
    """Retention has to happen on a path that runs.

    purge() had exactly one caller, the `second-eye purge` command, and nothing
    scheduled it. Sender addresses and message ids accumulated forever while
    the module docstring promised they would not.
    """
    monkeypatch.setenv("RETENTION_HOURS", "24")
    settings.cache_clear()

    store.claim("job-old", "msg-old", "jim@firm.com")
    _age("msg-old", hours=48)

    store.claim("job-new", "msg-new", "jim@firm.com")

    assert store.seen("msg-old") is False
    assert store.seen("msg-new") is True


def test_retention_of_zero_keeps_rows_rather_than_deleting_them(db, monkeypatch):
    """0 means keep indefinitely. It has never meant "store nothing"."""
    monkeypatch.setenv("RETENTION_HOURS", "0")
    settings.cache_clear()

    store.claim("job-old", "msg-old", "jim@firm.com")
    _age("msg-old", hours=10_000)
    store.claim("job-new", "msg-new", "jim@firm.com")

    assert store.seen("msg-old") is True


def test_a_failing_retention_sweep_does_not_fail_the_claim(db, monkeypatch):
    """Housekeeping must never cost a lawyer their review."""
    import sqlite3

    def broken() -> int:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "purge", broken)
    assert store.claim("job-1", "msg-1", "jim@firm.com") is True
