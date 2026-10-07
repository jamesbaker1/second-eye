"""Thread state: what the associate knows about a document conversation.

A review used to be one request and one response. The product it is becoming is
a conversation about a document that accumulates state, so something has to
remember: which document, which version, what was changed, and what questions
are still open.

The key design decision is that **the ledger is the source of truth, not the
redlined file**. Every change the agent makes is recorded as an intent (anchor,
replacement, why), and the redline is always regenerated from the original
document plus the currently-active changes.

That makes undo trivial and safe. "Undo what you did to clause 7" deactivates a
ledger entry and the document is rebuilt without it. The alternative, surgically
removing revision markup from a file, means editing XML that Word has already
touched, and it would eventually corrupt somebody's contract.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from lra import blobs
from lra.models import Finding
from lra.store import connect

log = logging.getLogger(__name__)

THREAD_SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    id TEXT PRIMARY KEY,               -- mail thread id, or a derived key
    owner TEXT NOT NULL,
    matter_id TEXT,
    filename TEXT,
    original_sha256 TEXT,
    original BLOB,                     -- the document as first received
    round INTEGER NOT NULL DEFAULT 0,
    -- The lawyer said "stop". Nothing on this thread is answered until a new
    -- document arrives on it, which is a new request.
    muted INTEGER NOT NULL DEFAULT 0,
    -- What started the conversation: 'review' or 'compare'. A reply to a
    -- comparison used to arrive as a message about nothing; now the later
    -- version is the document, and the reply is read as instructions for a
    -- review of it.
    origin TEXT NOT NULL DEFAULT 'review',
    -- Whose document, as the review resolved it: what `lra purge --client`
    -- finds a conversation by when no matter number says so.
    client_id TEXT,
    created_at TEXT,
    -- When the document last changed (a new version, a mute). The day a
    -- reply names as when it "reviewed" the document.
    updated_at TEXT,
    -- When a message from the lawyer last reached this conversation: a reply,
    -- an answer, an undo. Retention counts from the later of the two, so a
    -- conversation is kept THREAD_RETENTION_DAYS after the last reply.
    active_at TEXT
);

CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL,
    round INTEGER NOT NULL,
    anchor TEXT NOT NULL,
    replacement TEXT,
    title TEXT,
    category TEXT,
    origin TEXT,                       -- check | agent | instruction
    -- How the writer applies this. Without it every row replayed as a
    -- replacement, so "add a clause after 7" deleted clause 7 and the email
    -- reported it as new language. The ledger is the only path to the writer,
    -- so anything the Finding carries and the ledger does not is lost.
    edit_kind TEXT NOT NULL DEFAULT 'replace',
    active INTEGER NOT NULL DEFAULT 1,
    undone_at TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_changes_thread ON changes(thread_id, active);

-- Every identifier that should resolve to a thread: the provider's thread id,
-- the first message's id, and the id of every reply the agent sends. Providers
-- differ in what they populate, and a mail client's In-Reply-To points at
-- whichever message was replied to, so a single derived key is not enough.
CREATE TABLE IF NOT EXISTS thread_aliases (
    alias TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alias_thread ON thread_aliases(thread_id);

CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL,
    round INTEGER NOT NULL,
    question TEXT NOT NULL,
    anchor TEXT,
    options TEXT,                      -- json list
    answered_with TEXT,
    answered_at TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_questions_thread ON questions(thread_id, answered_at);

-- Every finding a review showed that is not a tracked change, under the letter
-- the lawyer sees beside it, so "B is fine" means the same point on every
-- later reply. `current` is whether the latest review showed it: a letter from
-- a review the document has since moved past is not one a reply can name.
CREATE TABLE IF NOT EXISTS thread_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL,
    round INTEGER NOT NULL,
    label TEXT NOT NULL,
    severity TEXT,
    category TEXT,
    title TEXT,
    anchor TEXT,
    question TEXT,
    current INTEGER NOT NULL DEFAULT 1,
    dismissed_at TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_findings_thread ON thread_findings(thread_id, current);
"""


@dataclass
class Change:
    id: int
    anchor: str
    replacement: str | None
    title: str
    category: str
    origin: str
    round: int
    active: bool
    edit_kind: str = "replace"

    def as_finding(self) -> Finding:
        from lra.models import Severity

        return Finding(
            severity=Severity.FORMATTING,
            category=self.category,
            title=self.title,
            explanation="",
            anchor=self.anchor,
            suggested_text=self.replacement,
            auto_apply=True,
            edit_kind=self.edit_kind,
        )

    def describe(self) -> str:
        return f"{self.title} ({self.anchor!r} -> {self.replacement!r})"


@dataclass
class Question:
    id: int
    question: str
    anchor: str | None
    options: list[str]
    answered_with: str | None
    round: int

    @property
    def open(self) -> bool:
        return self.answered_with is None


@dataclass
class Flagged:
    """A finding a review showed with a letter beside it, for "B is fine"."""
    id: int
    label: str
    severity: str
    category: str
    title: str
    anchor: str
    question: str | None
    current: bool
    dismissed: bool
    round: int = 0


# The letters a finding is shown under. Letters, because a change already has a
# number ("undo 2") and an answer is usually one ("30"). No I or O: an I reads
# as the pronoun and as a 1, an O as a 0.
LABELS = "ABCDEFGHJKLMNPQRSTUVWXYZ"


@dataclass
class ThreadState:
    id: str
    owner: str
    filename: str
    original: bytes = field(repr=False, default=b"")
    matter_id: str | None = None
    round: int = 0
    muted: bool = False
    origin: str = "review"
    changes: list[Change] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)
    flagged: list[Flagged] = field(default_factory=list)
    # ISO timestamps from the row. The reply to a second review of a document
    # names the day the first one happened, and until these were carried the
    # date lived only in the database.
    created_at: str | None = None
    updated_at: str | None = None

    @property
    def seen_on(self) -> str:
        """The day this conversation was last touched, as a lawyer would write it."""
        stamp = self.updated_at or self.created_at or ""
        try:
            day = datetime.fromisoformat(stamp[:10])
        except ValueError:
            return stamp[:10]
        return f"{day.day} {day.strftime('%b')}"

    @property
    def active_changes(self) -> list[Change]:
        return [c for c in self.changes if c.active]

    def number(self, change: Change) -> int:
        """The number a lawyer sees beside this change, in every email.

        Its place in the ledger, which only grows, so the number printed on the
        first review still means the same change three rounds later. Undone
        changes keep theirs; nothing is renumbered under the lawyer.
        """
        return next(i for i, c in enumerate(self.changes, 1) if c.id == change.id)

    def number_for(self, anchor: str, replacement: str | None) -> int | None:
        """The number of the active change writing this text, if there is one."""
        for i, c in enumerate(self.changes, 1):
            if c.active and c.anchor == anchor and c.replacement == replacement:
                return i
        return None

    @property
    def open_questions(self) -> list[Question]:
        """Unanswered, and not on a point the lawyer dismissed: "B is fine"
        about a blocker's question settles it, so a clean copy made after it
        must not warn that the question is still open."""
        dismissed = {f.question for f in self.flagged if f.dismissed and f.question}
        return [q for q in self.questions if q.open and q.question not in dismissed]

    @property
    def labelled(self) -> dict[str, Flagged]:
        """The letters a reply can name: those the latest review showed."""
        return {f.label: f for f in self.flagged if f.current}

    def summary(self) -> str:
        """What the agent should be told it already knows about this document."""
        if not self.changes and not self.questions:
            return ""
        lines = [f"# This conversation is about {self.filename}", ""]
        active = self.active_changes
        if active:
            lines.append(f"You have already made {len(active)} change{'s' if len(active) != 1 else ''}:")
            lines += [f"  - {c.describe()}" for c in active]
            lines.append("")
        undone = [c for c in self.changes if not c.active]
        if undone:
            lines.append("They asked you to undo these, so do not make them again:")
            lines += [f"  - {c.describe()}" for c in undone]
            lines.append("")
        still_open = self.open_questions
        if still_open:
            lines.append("You asked these and have not had an answer:")
            lines += [f"  - {q.question}" for q in still_open]
            lines.append("")
        dismissed = [f for f in self.flagged if f.dismissed]
        if dismissed:
            lines.append("They told you these points are fine, so do not raise them again:")
            lines += [f"  - {f.title}" for f in dismissed]
            lines.append("")
        return "\n".join(lines)


# Databases this process has already brought up to date, by URL. Checking is a
# statement, which costs nothing against a local file and a network round trip
# against D1, and init() runs at the top of nearly every function here.
_migrated: set[str] = set()


def init() -> None:
    from lra.config import settings

    url = settings().database_url
    with connect() as c:
        c.executescript(THREAD_SCHEMA)
        if url in _migrated:
            return
        _migrated.add(url)
        # Databases created before edit_kind existed. Every row in them is a
        # replacement, which is what the default records.
        columns = {r[1] for r in c.execute("PRAGMA table_info(changes)")}
        if "edit_kind" not in columns:
            c.execute(
                "ALTER TABLE changes ADD COLUMN edit_kind TEXT NOT NULL "
                "DEFAULT 'replace'"
            )
        # Databases created before "stop" was remembered. Nothing in them is
        # muted, which is what the default records.
        columns = {r[1] for r in c.execute("PRAGMA table_info(threads)")}
        if "muted" not in columns:
            c.execute("ALTER TABLE threads ADD COLUMN muted INTEGER NOT NULL DEFAULT 0")
        if "origin" not in columns:
            c.execute("ALTER TABLE threads ADD COLUMN origin TEXT NOT NULL DEFAULT 'review'")
        # Databases from before a purge could be by client. Their rows have
        # none, and are found by their matter's client instead (purge.py).
        if "client_id" not in columns:
            c.execute("ALTER TABLE threads ADD COLUMN client_id TEXT")
        # Databases from before every reply counted as activity. Null: no
        # reply since the column arrived, and updated_at alone decides.
        if "active_at" not in columns:
            c.execute("ALTER TABLE threads ADD COLUMN active_at TEXT")


def key(thread_id: str | None, message_id: str, owner: str) -> str:
    """A stable identifier for the conversation.

    Providers that model threads give us one. For those that do not, the first
    message's id becomes the thread key and replies carry it in In-Reply-To.
    """
    return hashlib.sha256(f"{owner}|{thread_id or message_id}".encode()).hexdigest()[:32]


def add_alias(thread_key: str, *identifiers: str | None) -> None:
    """Record further ways this conversation can be recognised."""
    init()
    with connect() as c:
        for identifier in identifiers:
            if not identifier:
                continue
            for form in _forms(identifier):
                c.execute(
                    "INSERT OR IGNORE INTO thread_aliases (alias, thread_id) VALUES (?, ?)",
                    (_alias(form), thread_key),
                )


def new_tap_token(thread_key: str) -> str:
    """A fresh token for this conversation's one-tap reply links.

    Random rather than derived from the thread key, which is a hash of the
    owner and a message id anyone on the thread has seen. Stored as an alias,
    so it resolves through `resolve` and its owner check like every other way
    back into a conversation, and is swept with the thread by `purge`.
    """
    token = secrets.token_hex(8)
    add_alias(thread_key, _TAP_PREFIX + token)
    return token


_TAP_PREFIX = "tap:"


def resolve(owner: str, thread_id: str | None, message_id: str,
            in_reply_to: str | None, references: list[str] | None = None,
            tap: str | None = None) -> str | None:
    """Find this sender's conversation, or None.

    Tries every identifier the message carries, because a reply's In-Reply-To
    points at the agent's own reply rather than the original, which is why each
    outbound message id is registered as an alias too.

    **The owner check is the security boundary.** A message id travels in every
    reply and forward, so it is public to anyone the thread touched. Matching on
    the alias alone meant that quoting one took over the conversation: the
    document was rebuilt from another lawyer's original, handed to the agent
    with their matter id, edited, and mailed back to whoever asked. With the
    shipped empty allowlist that was any stranger who had seen one message.

    An alias resolves only for the lawyer who owns the thread. A colleague on an
    allowlisted domain is still a different lawyer here.
    """
    init()
    owner = owner.lower()
    # The References chain last, nearest ancestor first. It names every
    # message in the conversation, including the lawyer's own first one, so a
    # reply still finds its thread when the provider gave our reply a
    # Message-ID we never learned. A one-tap reply carries its conversation
    # in the address it was sent to and nothing in its headers, so its token
    # is first.
    wanted = list(dict.fromkeys(
        _alias(form) for i in (_TAP_PREFIX + tap if tap else None, in_reply_to, thread_id,
                               message_id, *reversed(references or []))
        if i for form in _forms(i)))
    found: dict[str, str] = {}
    with connect() as c:
        # All at once, and the first in that order wins. One query per
        # identifier was a D1 round trip each, before anything else could
        # happen, and a long forwarded chain carries dozens.
        for start in range(0, len(wanted), _ALIASES_PER_QUERY):
            chunk = wanted[start:start + _ALIASES_PER_QUERY]
            marks = ",".join("?" for _ in chunk)
            for row in c.execute(
                "SELECT a.alias, a.thread_id FROM thread_aliases a "
                "JOIN threads t ON t.id = a.thread_id "
                f"WHERE a.alias IN ({marks}) AND t.owner = ?",
                (*chunk, owner),
            ):
                found.setdefault(row[0], row[1])
        hit = next((found[alias] for alias in wanted if alias in found), None)
        if hit is None:
            # Fall back to the derived key, which already includes the owner.
            # Asked as a row, not a load: a load fetched the document and the
            # ledger to learn that the conversation exists.
            derived = key(thread_id, message_id, owner)
            row = c.execute("SELECT 1 FROM threads WHERE id = ?", (derived,)).fetchone()
            hit = derived if row else None
    return _on_job(hit) if hit else None


def _on_job(thread_key: str) -> str:
    """Record that the job being handled is on this conversation (audit.thread),
    so a purge of its client or matter finds the job's sessions; and that the
    conversation is in use, so retention counts from this message (purge)."""
    from lra import audit

    audit.thread(thread_key)
    try:
        with connect() as c:
            c.execute("UPDATE threads SET active_at = ? WHERE id = ?",
                      (datetime.now(UTC).isoformat(), thread_key))
    except Exception:  # bookkeeping; never fails the reply
        log.warning("could not mark conversation %s active", thread_key, exc_info=True)
    return thread_key


# D1 binds at most 100 parameters to a statement.
_ALIASES_PER_QUERY = 90


def _alias(identifier: str) -> str:
    return hashlib.sha256(identifier.strip().encode()).hexdigest()[:32]


def _forms(identifier: str) -> tuple[str, ...]:
    """A message id with and without its angle brackets. The id a send call
    hands back need not be written the way the Message-ID header is, and the
    lawyer's In-Reply-To quotes the header; each is stored and looked up both
    ways, so neither spelling loses the conversation."""
    i = identifier.strip()
    bare = i[1:-1].strip() if i.startswith("<") and i.endswith(">") else i
    return (i, bare) if bare != i else (i, f"<{i}>")


def start(thread_key: str, owner: str, filename: str, original: bytes,
          matter_id: str | None = None, origin: str = "review",
          client_id: str | None = None) -> ThreadState:
    """Record a new document conversation, or resume an existing one.

    When a reply carries a document, that document supersedes what we held.
    Decision 21 names the pattern: accept some changes, edit a little, send it
    back. Returning the stored state unchanged meant the next instruction was
    carried out against the version the lawyer had already moved past, and
    their own edits came back deleted by a redline built from a stale original.

    `client_id` is whose document it is, as the review resolved it: kept so
    that `lra purge --client` finds a conversation whose client was known
    only from its parties, with no matter number to say so.
    """
    init()
    _on_job(thread_key)
    existing = load(thread_key)
    if existing and client_id:
        with connect() as c:
            c.execute("UPDATE threads SET client_id = ? WHERE id = ? AND client_id IS NULL",
                      (client_id, thread_key))
    if existing:
        if original and original != existing.original:
            return supersede(thread_key, filename, original)
        return existing

    # Retention rides on the write path, as it does for jobs: nothing here has
    # a scheduler, and one indexed DELETE per new conversation is cheaper than
    # building one. Never allowed to fail the review it is riding on.
    try:
        purge()
    except Exception:
        log.warning("thread retention sweep failed", exc_info=True)

    now = datetime.now(UTC).isoformat()
    with connect() as c:
        c.execute(
            "INSERT INTO threads (id, owner, matter_id, filename, original_sha256, "
            "original, round, origin, client_id, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (thread_key, owner.lower(), matter_id, filename,
             hashlib.sha256(original).hexdigest(), blobs.stash(original, "thread"),
             0, origin, client_id, now, now),
        )
    return ThreadState(id=thread_key, owner=owner.lower(), filename=filename,
                       original=original, matter_id=matter_id, round=0)


def _keep_previous(thread_key: str, previous, new_sha: str) -> None:
    """The document a newer version replaces is kept as an earlier version,
    so that "blackline against v1" has something to compare with
    (blackline.py). Its stored copy is handed over, not copied; if it cannot
    be kept it is discarded, as it always was."""
    stored, filename, sha, created_at = previous
    kept = False
    if sha != new_sha:
        try:
            from lra import blackline

            kept = blackline.keep_previous(thread_key, filename or "", stored, sha or "",
                                           created_at or "")
        except Exception:
            log.warning("could not keep the earlier version of %s", filename, exc_info=True)
    if not kept:
        blobs.discard(stored)


def purge(owner: str | None = None) -> int:
    """Delete conversations, and the documents they hold.

    With an owner: everything of theirs, which is the path that has to exist
    when a lawyer leaves or a client asks. Without one: whatever has been quiet
    for longer than THREAD_RETENTION_DAYS, counted from the later of the last
    new document and the last message from the lawyer.

    Nothing deleted these before. The job row that records an email was
    arriving is gone in a day; the original document, which is the actual
    privileged material, was kept for ever in a table nothing swept.
    """
    from lra.config import settings

    init()
    with connect() as c:
        if owner:
            rows = c.execute("SELECT id, original FROM threads WHERE owner = ?",
                             (owner.lower(),)).fetchall()
        else:
            days = settings().thread_retention_days
            if days <= 0:
                return 0
            cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
            rows = c.execute("SELECT id, original FROM threads WHERE updated_at < ? "
                             "AND (active_at IS NULL OR active_at < ?)",
                             (cutoff, cutoff)).fetchall()
        for thread_id, stored in rows:
            # Children first, the thread row last: if this is interrupted the
            # thread is still there to be swept next time, rather than leaving
            # a ledger with nothing to find it by.
            for table in ("changes", "questions", "thread_findings", "thread_aliases"):
                c.execute(f"DELETE FROM {table} WHERE thread_id = ?", (thread_id,))
            blobs.discard(stored)
            c.execute("DELETE FROM threads WHERE id = ?", (thread_id,))
    # Outside the transaction above, which holds SQLite's write lock. One left
    # behind by an interruption is swept a day later (blackline.sweep).
    for thread_id, _ in rows:
        _forget_versions(thread_id)
    return len(rows)


def _forget_versions(thread_id: str) -> None:
    """The earlier versions go with their conversation, at once."""
    try:
        from lra import blackline

        blackline.forget(thread_id)
    except Exception:
        log.warning("could not delete the earlier versions of %s", thread_id, exc_info=True)


def supersede(thread_key: str, filename: str, original: bytes) -> ThreadState:
    """Replace the document a conversation is about with a newer version.

    Every change already in the ledger is stood down. They were written against
    text that may no longer exist, and replaying them onto a document the
    lawyer has since edited is how their work gets overwritten. They stay in
    the ledger, inactive, so the conversation still knows what was discussed.
    """
    init()
    now = datetime.now(UTC).isoformat()
    with connect() as c:
        previous = c.execute("SELECT original, filename, original_sha256, created_at "
                             "FROM threads WHERE id = ?", (thread_key,)).fetchone()
        c.execute(
            "UPDATE threads SET original = ?, original_sha256 = ?, filename = ?, "
            "updated_at = ? WHERE id = ?",
            (blobs.stash(original, "thread"), hashlib.sha256(original).hexdigest(),
             filename, now, thread_key),
        )
        c.execute(
            "UPDATE changes SET active = 0, undone_at = ? "
            "WHERE thread_id = ? AND active = 1",
            (now, thread_key),
        )
    if previous:
        _keep_previous(thread_key, previous, hashlib.sha256(original).hexdigest())
    state = load(thread_key)
    if state is None:  # pragma: no cover - the row was just updated
        raise RuntimeError(f"thread {thread_key} vanished while superseding it")
    return state


def load(thread_key: str) -> ThreadState | None:
    init()
    with connect() as c:
        row = c.execute(
            "SELECT owner, matter_id, filename, original, round, muted, origin, "
            "created_at, updated_at FROM threads WHERE id = ?",
            (thread_key,),
        ).fetchone()
        if not row:
            return None
        state = ThreadState(
            id=thread_key, owner=row[0], matter_id=row[1], filename=row[2],
            original=blobs.fetch(row[3]), round=row[4], muted=bool(row[5]),
            origin=row[6] or "review", created_at=row[7], updated_at=row[8],
        )
        for r in c.execute(
            "SELECT id, anchor, replacement, title, category, origin, round, active, "
            "edit_kind FROM changes WHERE thread_id = ? ORDER BY id", (thread_key,)
        ):
            state.changes.append(Change(
                id=r[0], anchor=r[1], replacement=r[2], title=r[3] or "",
                category=r[4] or "", origin=r[5] or "", round=r[6], active=bool(r[7]),
                edit_kind=r[8] or "replace",
            ))
        for r in c.execute(
            "SELECT id, question, anchor, options, answered_with, round "
            "FROM questions WHERE thread_id = ? ORDER BY id", (thread_key,)
        ):
            state.questions.append(Question(
                id=r[0], question=r[1], anchor=r[2],
                options=json.loads(r[3] or "[]"), answered_with=r[4], round=r[5],
            ))
        for r in c.execute(
            "SELECT id, label, severity, category, title, anchor, question, current, "
            "dismissed_at, round FROM thread_findings WHERE thread_id = ? ORDER BY id", (thread_key,)
        ):
            state.flagged.append(Flagged(
                id=r[0], label=r[1], severity=r[2] or "", category=r[3] or "",
                title=r[4] or "", anchor=r[5] or "", question=r[6], current=bool(r[7]),
                dismissed=r[8] is not None, round=r[9],
            ))
    return state


def original_of(thread_key: str) -> tuple[str, bytes] | None:
    """(filename, original) and nothing else. For telling one document from
    another across a lawyer's conversations, where `load` also read every
    change and question of each candidate: on D1 two more round trips per
    conversation, for rows nobody looked at unless it matched."""
    init()
    with connect() as c:
        row = c.execute("SELECT filename, original FROM threads WHERE id = ?",
                        (thread_key,)).fetchone()
    if not row:
        return None
    return row[0] or "", blobs.fetch(row[1])


def is_first_conversation(owner: str, thread_key: str) -> bool:
    """Whether this is the only conversation we hold for this lawyer. Threads
    are kept for THREAD_RETENTION_DAYS, so a lawyer who has been away that
    long is treated as new again, which is about when the primer is useful."""
    init()
    with connect() as c:
        row = c.execute(
            "SELECT COUNT(*) FROM threads WHERE owner = ? AND id != ?",
            (owner.lower(), thread_key),
        ).fetchone()
    return not row or row[0] == 0


def remember_stop(thread_key: str, owner: str) -> None:
    """Mute the thread, creating it if the lawyer said "stop" before any
    document was ever reviewed on it (a reply to a rejection, say). The stub
    holds no document; the first one to arrive supersedes it."""
    init()
    now = datetime.now(UTC).isoformat()
    with connect() as c:
        c.execute(
            "INSERT OR IGNORE INTO threads (id, owner, filename, round, muted, "
            "created_at, updated_at) VALUES (?, ?, '', 0, 1, ?, ?)",
            (thread_key, owner.lower(), now, now),
        )
        c.execute("UPDATE threads SET muted = 1, updated_at = ? WHERE id = ?", (now, thread_key))


def recent_for_owner(owner: str, limit: int = 50) -> list[tuple[str, str]]:
    """(thread key, filename) for a lawyer's conversations, newest first."""
    init()
    with connect() as c:
        rows = c.execute(
            "SELECT id, filename FROM threads WHERE owner = ? "
            "ORDER BY updated_at DESC LIMIT ?",
            (owner.lower(), limit),
        ).fetchall()
    return [(r[0], r[1] or "") for r in rows]


def set_origin(thread_key: str, origin: str) -> None:
    init()
    with connect() as c:
        c.execute("UPDATE threads SET origin = ? WHERE id = ?", (origin, thread_key))


def mute(thread_key: str, muted: bool = True) -> None:
    """Remember that the lawyer said "stop" on this thread, or that they sent
    a new document to it, which lifts it.

    The confirmation used to promise "I will not reply on this thread again"
    and persist nothing, so the next reply on the thread was handled like any
    other. A promise the product breaks is the fastest route to the mail
    filter.
    """
    init()
    now = datetime.now(UTC).isoformat()
    with connect() as c:
        c.execute("UPDATE threads SET muted = ?, updated_at = ? WHERE id = ?",
                  (1 if muted else 0, now, thread_key))


def next_round(thread_key: str) -> int:
    init()
    now = datetime.now(UTC).isoformat()
    with connect() as c:
        c.execute(
            "UPDATE threads SET round = round + 1, updated_at = ? WHERE id = ?",
            (now, thread_key),
        )
        row = c.execute("SELECT round FROM threads WHERE id = ?", (thread_key,)).fetchone()
    return row[0] if row else 0


def record_changes(thread_key: str, round_number: int, findings: list[Finding],
                   origin: str = "check") -> list[int]:
    """Add changes to the ledger. Returns the row ids, in the order given.

    Two behaviours worth stating, because both were bugs.

    Deduplication is against ACTIVE rows only, with one asymmetry that matters.
    It used to match every row for the thread, so once a lawyer undid a change,
    asking for it again did nothing for the life of the conversation and the
    reply told them it could not be placed -- a specific claim that was false.

    A matching inactive row is now reactivated rather than duplicated, but only
    when `origin` is "instruction", which means the lawyer asked in their own
    words. The deterministic checks re-find the same defect on every round, and
    a check must never resurrect something the lawyer deliberately undid.

    The `seen` set is updated inside the loop. It used to be computed once
    before it, so the same change returned twice in one batch -- which a model
    does routinely -- produced two rows and an email that over-reported what
    was written.
    """
    init()
    now = datetime.now(UTC).isoformat()
    ids: list[int] = []
    with connect() as c:
        # One read for both, in id order so that, as with two queries, the
        # latest row wins when a pair recurs. On D1 each statement is a round
        # trip, and this runs on every review and every reply.
        active: dict[tuple, int] = {}
        inactive: dict[tuple, int] = {}
        for r in c.execute(
            "SELECT id, anchor, replacement, active FROM changes "
            "WHERE thread_id = ? ORDER BY id",
            (thread_key,),
        ):
            (active if r[3] else inactive)[(r[1], r[2])] = r[0]
        for f in findings:
            key_pair = (f.anchor, f.suggested_text)
            if key_pair in active:
                ids.append(active[key_pair])
                continue
            if key_pair in inactive:
                if origin != "instruction":
                    # A deterministic check re-finding the same defect every
                    # round must never resurrect something the lawyer undid.
                    # Only an explicit instruction counts as asking again.
                    continue
                row_id = inactive.pop(key_pair)
                c.execute(
                    "UPDATE changes SET active = 1, undone_at = NULL, round = ? "
                    "WHERE id = ?",
                    (round_number, row_id),
                )
                active[key_pair] = row_id
                ids.append(row_id)
                continue
            cur = c.execute(
                "INSERT INTO changes (thread_id, round, anchor, replacement, title, "
                "category, origin, active, created_at, edit_kind) "
                "VALUES (?,?,?,?,?,?,?,1,?,?)",
                (thread_key, round_number, f.anchor, f.suggested_text, f.title,
                 f.category, origin, now, f.edit_kind),
            )
            active[key_pair] = cur.lastrowid
            ids.append(cur.lastrowid)
    return ids


def deactivate_unlanded(thread_key: str, ids: list[int]) -> int:
    """Stand down changes that were recorded but did not reach the document.

    A change the writer refused -- an ambiguous anchor, one it could not place --
    used to stay active in the ledger. Rebuilding is attempted afresh every
    round, so a later undo that removed the competing text let the refused
    change resolve and apply silently, to a clause nobody had discussed. The
    lawyer asked to undo one thing and got a new edit.

    Deliberately not marked undone: nobody rejected these, so re-requesting one
    later is allowed.
    """
    init()
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    with connect() as c:
        cur = c.execute(
            f"UPDATE changes SET active = 0 "
            f"WHERE thread_id = ? AND id IN ({placeholders}) AND active = 1",
            [thread_key, *ids],
        )
        return cur.rowcount


def record_questions(thread_key: str, round_number: int, findings: list[Finding]) -> None:
    init()
    now = datetime.now(UTC).isoformat()
    with connect() as c:
        asked = {r[0] for r in c.execute(
            "SELECT question FROM questions WHERE thread_id = ?", (thread_key,))}
        for f in findings:
            if not f.question or f.question in asked:
                continue
            c.execute(
                "INSERT INTO questions (thread_id, round, question, anchor, options, "
                "created_at) VALUES (?,?,?,?,?,?)",
                (thread_key, round_number, f.question, f.anchor,
                 json.dumps(f.options), now),
            )


def record_findings(thread_key: str, round_number: int,
                    findings: list[Finding]) -> list[str | None]:
    """Give each finding a review is about to show its letter, and remember it.
    Returns the letters, in the order given; None past the last letter.

    A finding the thread has shown before (same kind, title and text) keeps its
    letter, so "B" means the same point on the second review of a document as
    on the first; a new one takes the next letter never used on the thread.
    Everything not shown this time stops being current, and so nameable.
    """
    init()
    now = datetime.now(UTC).isoformat()
    labels: list[str | None] = []
    with connect() as c:
        known: dict[tuple, tuple[int, str]] = {}
        used: set[str] = set()
        for r in c.execute(
            "SELECT id, label, category, title, anchor FROM thread_findings "
            "WHERE thread_id = ? ORDER BY id", (thread_key,)
        ):
            known[(r[2] or "", r[3] or "", r[4] or "")] = (r[0], r[1])
            used.add(r[1])
        c.execute("UPDATE thread_findings SET current = 0 WHERE thread_id = ? AND current = 1",
                  (thread_key,))
        free = [x for x in LABELS if x not in used]
        taken: set[str] = set()
        for f in findings:
            key_triple = (f.category or "", f.title or "", f.anchor or "")
            if key_triple in known and known[key_triple][1] not in taken:
                row_id, label = known[key_triple]
                c.execute("UPDATE thread_findings SET current = 1, round = ?, question = ? "
                          "WHERE id = ?", (round_number, f.question, row_id))
            elif free:
                label = free.pop(0)
                c.execute(
                    "INSERT INTO thread_findings (thread_id, round, label, severity, category, "
                    "title, anchor, question, current, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,1,?)",
                    (thread_key, round_number, label, f.severity.value, f.category,
                     f.title, f.anchor, f.question, now),
                )
            else:
                labels.append(None)
                continue
            taken.add(label)
            labels.append(label)
    return labels


def dismiss(thread_key: str, finding_ids: list[int]) -> int:
    """The lawyer said these are fine. Kept, not deleted: the letter stays
    theirs, and the agent is told not to raise them again."""
    init()
    if not finding_ids:
        return 0
    now = datetime.now(UTC).isoformat()
    placeholders = ",".join("?" for _ in finding_ids)
    with connect() as c:
        cur = c.execute(
            f"UPDATE thread_findings SET dismissed_at = ? WHERE thread_id = ? "
            f"AND id IN ({placeholders}) AND dismissed_at IS NULL",
            [now, thread_key, *finding_ids],
        )
        return cur.rowcount


def undo(thread_key: str, change_ids: list[int]) -> int:
    """Deactivate changes. The document is rebuilt without them, never patched."""
    init()
    if not change_ids:
        return 0
    now = datetime.now(UTC).isoformat()
    placeholders = ",".join("?" for _ in change_ids)
    with connect() as c:
        answered = [r[0] for r in c.execute(
            f"SELECT anchor FROM changes WHERE thread_id = ? AND id IN ({placeholders}) "
            "AND active = 1 AND category = 'answered'",
            [thread_key, *change_ids],
        )]
        cur = c.execute(
            f"UPDATE changes SET active = 0, undone_at = ? "
            f"WHERE thread_id = ? AND id IN ({placeholders}) AND active = 1",
            [now, thread_key, *change_ids],
        )
        undone = cur.rowcount
        # Undoing an answer reopens its question. Left answered, "30" after
        # "undo 1" matched nothing, and the reply went to the model as an
        # instruction instead of being the answer the lawyer meant.
        for anchor in answered:
            c.execute(
                "UPDATE questions SET answered_with = NULL, answered_at = NULL "
                "WHERE thread_id = ? AND anchor = ?",
                (thread_key, anchor),
            )
        return undone


def undo_all(thread_key: str) -> int:
    init()
    now = datetime.now(UTC).isoformat()
    with connect() as c:
        cur = c.execute(
            "UPDATE changes SET active = 0, undone_at = ? WHERE thread_id = ? AND active = 1",
            (now, thread_key),
        )
        return cur.rowcount


def answer(thread_key: str, question_id: int, value: str) -> None:
    init()
    now = datetime.now(UTC).isoformat()
    with connect() as c:
        c.execute(
            "UPDATE questions SET answered_with = ?, answered_at = ? "
            "WHERE thread_id = ? AND id = ?",
            (value, now, thread_key, question_id),
        )


def rebuild(state: ThreadState) -> tuple[bytes, list[Change], list[str]]:
    """Regenerate the redline from the original plus the active changes.

    Always from the original. Stacking edits onto an already-edited file means
    editing revision markup, which is how a client's contract gets corrupted.
    """
    from lra.models import Mode, ReviewResult
    from lra.pipeline import redline

    active = state.active_changes
    if not active:
        return state.original, [], []

    result = ReviewResult(
        mode=Mode.REDLINE, summary="",
        findings=[c.as_finding() for c in active],
    )
    out = redline.apply(state.original, result)
    applied_anchors = {(f.anchor, f.suggested_text) for f in out.applied}
    landed, dropped = [], []
    for change in active:
        (landed if (change.anchor, change.replacement) in applied_anchors
         else dropped).append(change)

    notes = list(out.notes)
    if dropped:
        # A change that stops landing because an earlier one was undone used to
        # vanish with no mention anywhere, while the ledger still called it
        # active. The lawyer undid one thing and lost two.
        notes.append(
            "These are no longer in the document, because the change they "
            "depended on was undone: "
            + "; ".join(f'"{c.title or c.anchor}"' for c in dropped[:4])
        )
    return out.content, landed, notes
