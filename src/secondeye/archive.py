"""The archive: every message the agent is copied on, stored and searchable.

This is what turns a document reviewer into something that knows what is going
on. It is also the most sensitive component in the system, because it holds
privileged client correspondence. See docs/archive.md for the constraints that
are launch blockers.

Two rules are enforced here in code rather than left to policy:

  1. Every read is scoped to one lawyer. There is no function in this module
     that returns another lawyer's mail, and `search` takes the owner as a
     required first argument rather than an optional filter.
  2. Every read is logged. If a document later becomes contested, the firm can
     say exactly what was accessed and when.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from secondeye import blobs
from secondeye.config import settings
from secondeye.models import InboundEmail
from secondeye.store import connect

log = logging.getLogger(__name__)

ARCHIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,           -- the lawyer this message belongs to
    message_id TEXT,
    thread_id TEXT,
    in_reply_to TEXT,
    direction TEXT,                -- sent | received
    from_address TEXT,
    to_addresses TEXT,             -- json list
    cc_addresses TEXT,             -- json list
    external TEXT,                 -- json list of outside-the-firm addresses
    subject TEXT,
    body TEXT,
    matter_id TEXT,
    counterparty TEXT,
    sent_at TEXT,
    archived_at TEXT,
    client_id TEXT                 -- whose matter, when the review knew (purge.py)
);
CREATE INDEX IF NOT EXISTS idx_msg_owner ON messages(owner, sent_at);
CREATE INDEX IF NOT EXISTS idx_msg_matter ON messages(owner, matter_id);
CREATE INDEX IF NOT EXISTS idx_msg_thread ON messages(owner, thread_id);

CREATE TABLE IF NOT EXISTS attachments (
    id TEXT PRIMARY KEY,
    message_pk TEXT NOT NULL,
    owner TEXT NOT NULL,
    filename TEXT,
    content_type TEXT,
    size_bytes INTEGER,
    sha256 TEXT,
    text_content TEXT,             -- extracted text, for search
    blob BLOB,                     -- encrypted at rest in production
    archived_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_att_msg ON attachments(message_pk);
CREATE INDEX IF NOT EXISTS idx_att_sha ON attachments(owner, sha256);

-- Full-text index. External-content tables were avoided deliberately: the
-- triggers needed to keep them in sync are a silent correctness risk, and this
-- archive is small enough per lawyer that the duplication is cheap.
CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(
    subject, body, participants, filenames, attachment_text,
    owner UNINDEXED, message_pk UNINDEXED, tokenize='porter unicode61'
);

-- Every read, for the day someone asks what was accessed.
CREATE TABLE IF NOT EXISTS access_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner TEXT NOT NULL,
    actor TEXT NOT NULL,           -- who the read was performed as
    action TEXT NOT NULL,
    detail TEXT,
    at TEXT
);
CREATE INDEX IF NOT EXISTS idx_access_owner ON access_log(owner, at);
"""


class CrossOwnerAccess(PermissionError):
    """Raised when something tries to read one lawyer's archive as another.

    This should never happen. If it does it is a bug worth failing loudly for,
    not a condition to handle gracefully.
    """


@dataclass
class ArchivedMessage:
    id: str
    owner: str
    direction: str
    from_address: str
    to: list[str]
    cc: list[str]
    external: list[str]
    subject: str
    body: str
    matter_id: str | None
    counterparty: str | None
    sent_at: str
    thread_id: str | None = None
    filenames: list[str] = field(default_factory=list)
    snippet: str = ""

    @property
    def sent_date(self) -> datetime | None:
        try:
            return datetime.fromisoformat(self.sent_at)
        except (TypeError, ValueError):
            return None


_migrated: set[str] = set()


def init() -> None:
    with connect() as c:
        c.executescript(ARCHIVE_SCHEMA)
        url = settings().database_url
        if url in _migrated:
            return
        # Archives from before a purge could be by client: their messages
        # are found by their matter's client instead (purge.py).
        columns = {r[1] for r in c.execute("PRAGMA table_info(messages)")}
        if "client_id" not in columns:
            c.execute("ALTER TABLE messages ADD COLUMN client_id TEXT")
        _migrated.add(url)


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------


def store(
    email: InboundEmail,
    owner: str,
    direction: str,
    matter_id: str | None = None,
    external: list[str] | None = None,
    attachment_text: dict[str, str] | None = None,
    client_id: str | None = None,
) -> str:
    """Archive one message. Idempotent on (owner, message_id).

    `direction` is "sent" when the lawyer sent it and copied us, "received"
    when they forwarded us something that came in. The distinction matters for
    the tracker, which needs to know who owes whom. `client_id`, when the
    review knows it, is what `second-eye purge --client` finds the message by.
    """
    if not settings().archive_enabled:
        return ""

    init()
    owner = owner.lower()
    pk = _pk(owner, email.message_id)
    now = datetime.now(UTC).isoformat()
    external = external or []
    attachment_text = attachment_text or {}

    with connect() as c:
        existing = c.execute("SELECT id FROM messages WHERE id = ?", (pk,)).fetchone()
        if existing:
            return pk

        filenames = []
        for att in email.attachments:
            filenames.append(att.filename)
            c.execute(
                "INSERT OR REPLACE INTO attachments (id, message_pk, owner, filename, "
                "content_type, size_bytes, sha256, text_content, blob, archived_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    _pk(pk, att.filename), pk, owner, att.filename, att.content_type,
                    att.size_bytes, hashlib.sha256(att.content).hexdigest(),
                    attachment_text.get(att.filename, ""),
                    blobs.stash(att.content, "archive") if settings().archive_blobs
                    else None, now,
                ),
            )

        c.execute("DELETE FROM message_fts WHERE message_pk = ?", (pk,))
        c.execute(
            "INSERT INTO message_fts (subject, body, participants, filenames, "
            "attachment_text, owner, message_pk) VALUES (?,?,?,?,?,?,?)",
            (
                email.subject or "",
                email.text_body or "",
                " ".join([email.from_address, *email.to, *email.cc]),
                " ".join(filenames),
                " ".join(attachment_text.values()),
                owner,
                pk,
            ),
        )

        # The message row goes in last, because it is what the check at the top
        # reads as "already archived". On D1 each statement commits on its own
        # (d1.py), so written first, a failure here left a message that looked
        # archived with no attachments and no search entry, and the early
        # return above made sure it never got them. Written last, a partial
        # attempt is simply repeated: the attachment rows replace themselves
        # and the search entry is cleared before it is written.
        c.execute(
            "INSERT INTO messages (id, owner, message_id, thread_id, in_reply_to, "
            "direction, from_address, to_addresses, cc_addresses, external, subject, "
            "body, matter_id, counterparty, sent_at, archived_at, client_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                pk, owner, email.message_id, email.thread_id, email.in_reply_to,
                direction, email.from_address, json.dumps(email.to),
                json.dumps(email.cc), json.dumps(external), email.subject,
                email.text_body, matter_id, _counterparty(external),
                email.received_at.isoformat(), now, client_id,
            ),
        )

    # Retention rides on the write path. There is no scheduler in this
    # deployment, so an ARCHIVE_RETENTION_DAYS the firm set in good faith used
    # to expire nothing at all: purge() had no caller but the command line.
    # A sweep that never runs is a retention policy that does not exist, and
    # this archive holds privileged correspondence.
    _expire_old_messages()
    return pk


def _expire_old_messages() -> None:
    """Apply the retention window, and never let it break an archive write.

    Archiving is a side effect of a review. If the sweep fails, the lawyer
    should still get their review, so this swallows database errors and says so
    in the log rather than raising into the handler.
    """
    if settings().archive_retention_days <= 0:
        return
    try:
        deleted = purge()
    except sqlite3.Error:
        log.warning("archive retention sweep failed", exc_info=True)
        return
    if deleted:
        log.info("archive retention removed %d message(s)", deleted)


def log_access(owner: str, actor: str, action: str, detail: str = "") -> None:
    init()
    with connect() as c:
        c.execute(
            "INSERT INTO access_log (owner, actor, action, detail, at) VALUES (?,?,?,?,?)",
            (owner.lower(), actor.lower(), action, detail,
             datetime.now(UTC).isoformat()),
        )


# --------------------------------------------------------------------------
# Reading. Every function here takes the owner first, and it is not optional.
# --------------------------------------------------------------------------


def search(owner: str, query: str, limit: int = 20,
           matter_id: str | None = None) -> list[ArchivedMessage]:
    """Full-text search across one lawyer's archive."""
    init()
    owner = owner.lower()
    log_access(owner, owner, "search", query[:200])

    cleaned = _sanitize_fts(query)
    if not cleaned:
        return []

    sql = (
        "SELECT m.*, snippet(message_fts, 1, '', '', ' ... ', 18) AS snip "
        "FROM message_fts f JOIN messages m ON m.id = f.message_pk "
        "WHERE message_fts MATCH ? AND f.owner = ? "
    )
    params: list = [cleaned, owner]
    if matter_id:
        sql += "AND m.matter_id = ? "
        params.append(matter_id)
    sql += "ORDER BY rank LIMIT ?"
    params.append(limit)

    with connect() as c:
        c.row_factory = sqlite3.Row
        try:
            rows = c.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            # A malformed FTS expression is a user typing, not a crash.
            return []
    return [_row(r) for r in rows]


def thread(owner: str, thread_id: str) -> list[ArchivedMessage]:
    """Every archived message in one thread, oldest first."""
    init()
    owner = owner.lower()
    log_access(owner, owner, "thread", thread_id)
    with connect() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM messages WHERE owner = ? AND thread_id = ? ORDER BY sent_at",
            (owner, thread_id),
        ).fetchall()
    return [_row(r) for r in rows]


def by_matter(owner: str, matter_id: str, limit: int = 50) -> list[ArchivedMessage]:
    init()
    owner = owner.lower()
    log_access(owner, owner, "by_matter", matter_id)
    with connect() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM messages WHERE owner = ? AND matter_id = ? "
            "ORDER BY sent_at DESC LIMIT ?",
            (owner, matter_id, limit),
        ).fetchall()
    return [_row(r) for r in rows]


def latest_version(owner: str, filename: str) -> tuple[bytes, str] | None:
    """The most recent archived copy of a document by name.

    This is what makes "compare this to the last one you sent me" work without
    a second attachment.
    """
    init()
    owner = owner.lower()
    stem = _stem(filename)
    log_access(owner, owner, "latest_version", filename)
    with connect() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT a.filename, a.blob, m.sent_at FROM attachments a "
            "JOIN messages m ON m.id = a.message_pk "
            "WHERE a.owner = ? AND a.blob IS NOT NULL ORDER BY m.sent_at DESC LIMIT 200",
            (owner,),
        ).fetchall()
    for r in rows:
        if _stem(r["filename"]) == stem:
            return blobs.fetch(r["blob"]), r["sent_at"]
    return None


def counterparties(owner: str) -> list[tuple[str, int, str]]:
    """Who this lawyer corresponds with outside the firm, by volume."""
    init()
    owner = owner.lower()
    # Logged like every other read. This one is easy to think of as derived
    # statistics rather than a read, which is exactly how it came to be the one
    # function in the module that was missing from the audit trail: it exposes
    # who a lawyer deals with outside the firm and when they last did, which is
    # precisely what someone asks about when a matter becomes contested.
    log_access(owner, owner, "counterparties")
    with connect() as c:
        rows = c.execute(
            "SELECT counterparty, COUNT(*) AS n, MAX(sent_at) AS last "
            "FROM messages WHERE owner = ? AND counterparty IS NOT NULL "
            "GROUP BY counterparty ORDER BY n DESC",
            (owner,),
        ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def purge(owner: str | None = None) -> int:
    """Delete archived mail. The path that has to exist when a client asks."""
    init()
    with connect() as c:
        if owner:
            pks = [r[0] for r in c.execute(
                "SELECT id FROM messages WHERE owner = ?", (owner.lower(),)).fetchall()]
            for (stored,) in c.execute(
                    "SELECT blob FROM attachments WHERE owner = ? AND blob IS NOT NULL",
                    (owner.lower(),)).fetchall():
                blobs.discard(stored)
            c.execute("DELETE FROM messages WHERE owner = ?", (owner.lower(),))
            c.execute("DELETE FROM attachments WHERE owner = ?", (owner.lower(),))
            c.execute("DELETE FROM message_fts WHERE owner = ?", (owner.lower(),))
            return len(pks)

        hours = settings().archive_retention_days * 24
        if hours <= 0:
            return 0
        cutoff = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        pks = [r[0] for r in c.execute(
            "SELECT id FROM messages WHERE sent_at < ?", (cutoff,)).fetchall()]
        for pk in pks:
            for (stored,) in c.execute(
                    "SELECT blob FROM attachments WHERE message_pk = ? AND blob IS NOT NULL",
                    (pk,)).fetchall():
                blobs.discard(stored)
            c.execute("DELETE FROM messages WHERE id = ?", (pk,))
            c.execute("DELETE FROM attachments WHERE message_pk = ?", (pk,))
            c.execute("DELETE FROM message_fts WHERE message_pk = ?", (pk,))
        return len(pks)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _pk(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


def _stem(filename: str) -> str:
    """Filename without extension, version markers, or redline suffixes."""
    name = filename.rsplit(".", 1)[0].lower()
    name = re.sub(r"\(redline\)|\(clean\)|\(v?\d+\)|_v\d+|\bv\d+\b", "", name)
    return re.sub(r"[^a-z0-9]+", "", name)


def _counterparty(external: list[str]) -> str | None:
    """The outside-the-firm domain this message is really with."""
    from secondeye.pipeline.email_checks import _FREE_MAIL

    domains = [
        a.split("@")[-1].lower() for a in external
        if "@" in a and a.split("@")[-1].lower() not in _FREE_MAIL
    ]
    return domains[0] if domains else None


_FTS_SPECIAL = re.compile(r'["*():^-]')


def _sanitize_fts(query: str) -> str:
    """Quote each term so a lawyer's punctuation cannot become FTS syntax."""
    terms = [t for t in _FTS_SPECIAL.sub(" ", query).split() if len(t) > 1]
    return " ".join(f'"{t}"' for t in terms)


def _row(r: sqlite3.Row) -> ArchivedMessage:
    keys = r.keys()
    return ArchivedMessage(
        id=r["id"],
        owner=r["owner"],
        direction=r["direction"],
        from_address=r["from_address"] or "",
        to=json.loads(r["to_addresses"] or "[]"),
        cc=json.loads(r["cc_addresses"] or "[]"),
        external=json.loads(r["external"] or "[]"),
        subject=r["subject"] or "",
        body=r["body"] or "",
        matter_id=r["matter_id"],
        counterparty=r["counterparty"],
        sent_at=r["sent_at"] or "",
        thread_id=r["thread_id"],
        snippet=(r["snip"] if "snip" in keys else "") or "",
    )
