"""The retention sweep: everything about a document, deleted once its window
has passed.

Jim's decision of 2026-10-04: by default the product keeps nothing beyond a
short window. THREAD_RETENTION_DAYS (7) is that window, counted from a
conversation's last activity, and this module is what makes it true. It
runs once a day from the Worker's cron (`POST /retention/sweep`, main.py),
and `second-eye purge` with no scope runs it by hand.

Before this, each sweep rode a write path: a conversation's ran when a new
conversation started, the archive's when mail was archived. A deployment
that received no new conversation for a fortnight deleted nothing, and
closings, unconfirmed notes and Anthropic's sessions had no sweep at all.

What it deletes, each step on its own so one failure leaves the rest to run:

- job rows past RETENTION_HOURS (store.purge);
- conversations quiet for the window, with their documents, every earlier
  version (thread.purge) and the negotiation ledger (negotiation.sweep);
- closings quiet for the window, with their signed pages (closing.expire);
- archived mail past ARCHIVE_RETENTION_DAYS (archive.purge), when on;
- notes the agent proposed that nobody confirmed, and, unless
  LEARN_FROM_OUTCOMES is on, the record of what was kept or undone
  (memory.expire);
- at Anthropic, any review session of a job received before the window
  that is still there (each is deleted when it ends; this catches a delete
  that failed), with the files it wrote, and any upload whose delete failed
  at the time (`_anthropic`). The audit row keeps the ids and says when they went.

What it keeps, by design: the audit trail (no contents), the purge records,
memory a lawyer asked for, and the firm's own settings. Mail held by the
kill switch waits, sealed, until it is released.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from secondeye.config import settings
from secondeye.store import connect

log = logging.getLogger(__name__)

# Jobs whose Anthropic copies one sweep deletes at most. A day's mail is far
# below it; the rest wait for tomorrow rather than holding the cron open.
ANTHROPIC_BATCH = 200


def cutoff(days: int | None = None) -> str | None:
    """The moment before which something about a document has outlived the
    window, or None when THREAD_RETENTION_DAYS keeps everything (0)."""
    days = settings().thread_retention_days if days is None else days
    if days <= 0:
        return None
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def sweep() -> dict[str, int]:
    """Delete everything past its retention. Returns how many of each went;
    a step that failed is logged and reported as -1, and runs again
    tomorrow."""
    from secondeye import archive, blackline, closing, memory, negotiation, store, thread

    before = cutoff()
    steps: list[tuple[str, Callable[[], int]]] = [
        ("job rows", store.purge),
        ("conversations", thread.purge),
        ("earlier versions left behind", _counted(blackline.sweep)),
        ("negotiation records left behind", _counted(negotiation.sweep)),
        ("closings", lambda: closing.expire(before) if before else 0),
        ("archived messages", archive.purge),
        ("unconfirmed notes and outcomes", lambda: memory.expire(before) if before else 0),
        ("Anthropic jobs", lambda: _anthropic(before) if before else 0),
    ]
    done: dict[str, int] = {}
    for name, step in steps:
        try:
            done[name] = int(step() or 0)
        except Exception:  # one step must not stop the others
            log.exception("retention: %s failed; it runs again on the next sweep", name)
            done[name] = -1
    log.info("retention sweep: %s", json.dumps(done))
    return done


def _counted(fn: Callable[[], None]) -> Callable[[], int]:
    """A sweep that reports nothing, as a step that counts nothing."""
    def run() -> int:
        fn()
        return 0
    return run


def _anthropic(before: str) -> int:
    """Each review session of a job received before `before`, deleted with
    the files it wrote, and any upload of it still held. A session is
    deleted when it ends (managed._clean_up); this is the backstop for one
    whose delete failed or that never settled. An object already gone counts
    as deleted. What Anthropic keeps under its own retention is separate and
    not ours to shorten.

    Skipped where no API key is set (a laptop): nothing was sent there."""
    if not settings().anthropic_api_key.strip():
        return 0
    from secondeye import audit, purge

    audit.init()
    with connect() as c:
        rows = c.execute(
            "SELECT job_id, session_ids, file_ids FROM audit_log WHERE received_at < ? "
            "AND (expired IS NULL OR expired = '') AND (purged IS NULL OR purged = '') "
            "AND ((session_ids IS NOT NULL AND session_ids != '[]') "
            "OR (file_ids IS NOT NULL AND file_ids != '[]')) "
            "ORDER BY received_at LIMIT ?", (before, ANTHROPIC_BATCH)).fetchall()
    done = 0
    for job_id, sessions, files in rows:
        try:
            for session_id in json.loads(sessions or "[]"):
                purge._session(session_id, "")
            for file_id in json.loads(files or "[]"):
                purge._file(file_id, "")
        except Exception as e:  # noqa: BLE001 - left for the next sweep
            log.warning("retention: Anthropic copies of job %s not deleted yet: %s", job_id, e)
            continue
        with connect() as c:
            c.execute("UPDATE audit_log SET expired = ? WHERE job_id = ?",
                      (datetime.now(UTC).isoformat(), job_id))
        done += 1
    return done


__all__ = ["ANTHROPIC_BATCH", "cutoff", "sweep"]
