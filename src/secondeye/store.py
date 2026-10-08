"""Job persistence + retention.

Documents sent here are privileged client material. The default posture is that
we keep the file only as long as the job needs it, and the row only long enough
to be idempotent against webhook retries.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta

from secondeye.config import settings

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    message_id TEXT UNIQUE,
    status TEXT NOT NULL,
    sender TEXT,
    payload TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);
"""


def _path() -> str:
    return settings().database_url.replace("sqlite:///", "")



def is_d1() -> bool:
    """Whether rows live in Cloudflare D1 rather than a local file."""
    return settings().database_url.startswith("d1://")


def connect():
    """A connection to wherever this deployment keeps its rows."""
    if is_d1():
        from secondeye import d1

        conn = d1.connect()
        conn.executescript(SCHEMA)
        return conn
    return _connect_sqlite()


def _connect_sqlite() -> sqlite3.Connection:
    """A connection configured for concurrent readers and one writer.

    The default journal mode blocks readers during a write and gives up
    immediately on a locked database, which under concurrent webhook deliveries
    surfaces as "database is locked" in the middle of a review.
    """
    conn = sqlite3.connect(_path(), timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        # Changing journal mode needs exclusive access and is one of the few
        # statements that does not wait for the busy timeout. On a fresh
        # database several simultaneous deliveries race to convert it, and all
        # but one raised "database is locked" out of connect() -- before the
        # job had started, so the lawyer got "something went wrong" for a
        # document nothing had looked at yet. Whichever connection wins sets
        # the mode for every later one; the losers carry on in the mode the
        # file is already in.
        pass
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def claim(job_id: str, message_id: str, sender: str) -> bool:
    """Take ownership of a message, atomically. False means someone else has it.

    Webhook providers retry, and a retry that arrives while the first delivery
    is still running used to pass a separate SELECT and produce two full agent
    runs and two replies for the same document. The insert itself is the lock:
    the UNIQUE constraint on message_id decides the race in the database
    rather than between two reads.
    """
    now = datetime.now(UTC).isoformat()
    with connect() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO jobs (id, message_id, status, sender, payload, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (job_id, message_id, "received", sender, "{}", now, now),
        )
        # A process that died mid-review is not taken over here: the
        # Workflow retries the step under the same job id (claim_for), and
        # the lease that let a queue redelivery take a job over went with the
        # queue (docs/migration.md, phase 5).
        claimed = cur.rowcount == 1
    if claimed:
        # Retention rides on the inbound path. Nothing schedules purge(): its
        # only caller was `second-eye purge`, which nobody runs, so the promise in this
        # module's docstring was never kept and sender addresses accumulated
        # forever. One indexed DELETE per inbound email is cheaper than a
        # scheduler we do not have.
        _expire_old_jobs()
    return claimed


def claim_for(job_id: str, message_id: str, sender: str) -> bool:
    """claim(), for a caller that may ask twice with the same job id.

    A Workflow step is retried when its container call fails, and the retry
    carries the same job id (the instance id). The first attempt may already
    have claimed the message before it died, and to claim() that looks like
    somebody else's job. It is ours, so it is ours again."""
    if claim(job_id, message_id, sender):
        return True
    with connect() as c:
        row = c.execute("SELECT id FROM jobs WHERE message_id = ?", (message_id,)).fetchone()
    return row is not None and row[0] == job_id


def _expire_old_jobs() -> None:
    """Apply the retention window, and never let it fail a review.

    A retention sweep is housekeeping. If it throws, the lawyer should still
    get their review, so a database error is logged rather than raised into the
    handler that is about to run the actual job.
    """
    try:
        deleted = purge()
    except sqlite3.Error:
        log.warning("retention sweep failed", exc_info=True)
        return
    if deleted:
        log.info("retention removed %d job row(s)", deleted)


def seen(message_id: str) -> bool:
    """Whether this message has been taken. Prefer claim(), which is atomic."""
    with connect() as c:
        row = c.execute("SELECT 1 FROM jobs WHERE message_id = ?", (message_id,)).fetchone()
    return row is not None


def record(job_id: str, message_id: str, status: str, sender: str, payload: dict) -> None:
    now = datetime.now(UTC).isoformat()
    with connect() as c:
        c.execute(
            "INSERT INTO jobs (id, message_id, status, sender, payload, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(message_id) DO UPDATE SET status=excluded.status, payload=excluded.payload, "
            "updated_at=excluded.updated_at",
            (job_id, message_id, status, sender, json.dumps(payload), now, now),
        )
    # The audit row outlives this one (audit.py): same event, no contents.
    from secondeye import audit

    audit.status(job_id, message_id, status, sender, payload)


def purge() -> int:
    """Delete job rows older than the retention window.

    RETENTION_HOURS <= 0 means keep rows indefinitely, not "store nothing":
    idempotency against webhook retries needs the row to exist for as long as a
    provider might retry. Turning storage off entirely is not a setting, it is
    a different product.
    """
    hours = settings().retention_hours
    if hours <= 0:
        return 0
    cutoff = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    with connect() as c:
        cur = c.execute("DELETE FROM jobs WHERE created_at < ?", (cutoff,))
        return cur.rowcount
