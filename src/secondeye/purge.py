"""Deleting everything held for one lawyer, one client or one matter.

A firm's general counsel asks one question about deletion: "can you delete
everything for client X, including what Anthropic holds?" Before this module
the answer was "per lawyer, and not the notes, the audit trail or Anthropic's
copies". `second-eye purge --client X`, `--matter Y` and `--lawyer Z` answer it with
one command:

- our rows and the documents they point to in object storage: conversations
  (and their ledgers, earlier versions and negotiation records), closings and
  signed pages, archived mail with its attachments and search entries, job
  rows;
- what was learned: remembered notes, learned suppressions and the
  suggestion outcomes they were learned from;
- Anthropic's copies: each session that worked on the scope is deleted with
  the files it wrote, every file uploaded for it is deleted, and each memory
  store of the scope is deleted (a store another scope shares, such as the
  client's when one matter goes, is rewritten without the deleted notes);
- the audit trail is kept and blanked, not deleted (`_redact` says why),
  and the purge itself leaves a content-free record: who ran it, the scope,
  how many of each kind, and when (`purges`).

**Dry run by default.** `plan` reads and changes nothing; `run(apply=True)`
does it. **Resumable.** The plan is written down as items before anything is
deleted, each item is marked done as it goes, and a row is deleted only after
the stored document or Anthropic object it points to has gone. A failure
(Anthropic unreachable, a session still running) leaves that item pending
and the rest carry on; running the same command again re-plans, adds anything
written since, and finishes. Everything is idempotent: an object already gone
counts as deleted.

How a row is tied to a client or matter: `matter_id` on conversations,
archived mail, closings, audit rows and suggestion outcomes; `client_id` on
the same (written from 2026-10-04, as the review resolves it); a matter
belongs to a client by `clients.for_matter` (a link, or its number). Rows
with neither cannot be attributed and are counted, not deleted, in a client
or matter purge: purging the lawyer who sent them reaches them.
"""

from __future__ import annotations

import getpass
import json
import logging
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime

from secondeye import blobs
from secondeye.store import connect

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS purges (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,          -- lawyer | client | matter
    scope_key TEXT NOT NULL,      -- the address, client number or matter id
    actor TEXT,                   -- who ran it
    started_at TEXT,
    finished_at TEXT,             -- null while anything is still to do
    counts TEXT                   -- json: kind -> how many
);
-- The plan of one purge, item by item, until it has all been done. Ids only.
CREATE TABLE IF NOT EXISTS purge_items (
    purge_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    item TEXT NOT NULL,
    done_at TEXT,
    error TEXT,
    PRIMARY KEY (purge_id, kind, item)
);
"""

SCOPES = ("lawyer", "client", "matter")

# Every kind of thing a purge deletes, in the order it is done, with what
# the command line calls it. Rows that point at something (a version at its
# conversation) go before what they point at; Anthropic after our rows, so a
# failure there leaves nothing of ours half-deleted; the audit rows last,
# so until the end they still say which sessions were whose.
KINDS: dict[str, str] = {
    "version": "earlier versions of a document",
    "negotiation": "negotiation records",
    "conversation": "conversations and their documents",
    "closing": "closings and their signed pages",
    "message": "archived messages, with attachments and search entries",
    "note": "remembered notes",
    "suppression": "learned suppressions",
    "outcome": "suggestion outcomes (what was undone, dismissed or kept)",
    "job": "job rows",
    "read": "archive read-log entries (detail blanked)",
    "store": "Anthropic memory stores (deleted)",
    "projection": "Anthropic memory stores shared with other work (rewritten)",
    "session": "Anthropic sessions (deleted, with the files they wrote)",
    "file": "files uploaded to Anthropic (deleted)",
    "access": "document-system access (revoked)",
    "audit": "audit rows (names and ids blanked; the row is kept)",
}
ANTHROPIC = ("store", "projection", "session", "file")

# D1 binds at most 100 parameters to a statement.
_CHUNK = 90


def init() -> None:
    with connect() as c:
        c.executescript(SCHEMA)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _q(sql: str, args=()) -> list:
    with connect() as c:
        return c.execute(sql, tuple(args)).fetchall()


def _in(sql: str, column: str, values, args=()) -> list:
    """`sql` (ending in WHERE or AND) with `lower(column) IN (values)`,
    in chunks D1 will take."""
    values = sorted({str(v).lower() for v in values if v})
    out: list = []
    for i in range(0, len(values), _CHUNK):
        chunk = values[i:i + _CHUNK]
        marks = ",".join("?" for _ in chunk)
        out += _q(f"{sql} lower({column}) IN ({marks})", (*args, *chunk))
    return out


# --------------------------------------------------------------------------
# The plan: what a scope holds. Reads only.
# --------------------------------------------------------------------------


@dataclass
class Plan:
    scope: str
    key: str
    items: dict[str, list[str]] = field(default_factory=dict)
    # For a client: the matters found to be its.
    matters: list[str] = field(default_factory=list)
    # Rows with no client or matter recorded, which a client or matter purge
    # cannot reach: kind -> how many.
    unattributed: dict[str, int] = field(default_factory=dict)

    def add(self, kind: str, ids) -> None:
        have = self.items.setdefault(kind, [])
        seen = set(have)
        for i in ids:
            i = str(i) if i is not None else ""
            if i and i not in seen:
                seen.add(i)
                have.append(i)

    @property
    def counts(self) -> dict[str, int]:
        return {k: len(self.items[k]) for k in KINDS if self.items.get(k)}

    def anthropic_ids(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for item in self.items.get("store", []):
            scope, key, store_id = item.split("|", 2)
            out.setdefault("memory stores", []).append(f"{store_id} ({scope} {key})")
        for item in self.items.get("projection", []):
            scope, key, store_id = item.split("|", 2)
            out.setdefault("memory stores rewritten", []).append(f"{store_id} ({scope} {key})")
        if self.items.get("session"):
            out["sessions"] = list(self.items["session"])
        if self.items.get("file"):
            out["uploaded files"] = list(self.items["file"])
        return out


def _schemas() -> None:
    """Every table a purge reads, so a deployment that never used one (the
    archive switched off, no closings) is read as empty, not as an error.
    Schema only: no row changes."""
    from secondeye import archive, audit, blackline, clients, closing, memory, negotiation, thread

    thread.init()
    archive.init()
    closing.init()
    audit.init()
    clients.init()
    memory.init()
    blackline.init()
    negotiation.init()
    init()


def matters_of(client_id: str) -> set[str]:
    """Every matter, lower case, that is this client's: linked to it, or
    numbered with its client number and linked to no other. Reads only:
    `clients.for_matter` would record the links it infers."""
    from secondeye import clients

    cid = client_id.strip().lower()
    links = {str(m).lower(): str(c).lower()
             for m, c in _q("SELECT matter_id, client_id FROM client_matters")}
    found = {m for m, c in links.items() if c == cid}
    if clients.get(cid) is None:
        return found
    seen: set[str] = set()
    for sql in ("SELECT DISTINCT matter_id FROM threads",
                "SELECT DISTINCT matter_id FROM messages",
                "SELECT DISTINCT matter_id FROM closings",
                "SELECT DISTINCT matter_id FROM audit_log",
                "SELECT DISTINCT matter_id FROM suggestion_outcomes",
                "SELECT DISTINCT scope_key FROM memory WHERE scope = 'matter'",
                "SELECT DISTINCT scope_key FROM memory_stores WHERE scope = 'matter'"):
        seen |= {str(r[0]).lower() for r in _q(sql) if r[0]}
    for m in seen - set(links):
        number = clients._MATTER_NUMBER.match(m)
        if number and number.group(1) == cid:
            found.add(m)
    return found


def _by_scope(plan: Plan, table: str, cols: str, *, owner: str = "owner",
              matter: str | None = "matter_id", client: str | None = "client_id") -> list:
    """The rows of `table` in the plan's scope: by owner for a lawyer, by
    matter for a matter, by client or any of its matters for a client."""
    key = plan.key
    head = f"SELECT {cols} FROM {table} WHERE"
    if plan.scope == "lawyer":
        return _q(f"{head} lower({owner}) = ?", (key,))
    if plan.scope == "matter":
        return _q(f"{head} lower({matter}) = ?", (key,)) if matter else []
    rows = _q(f"{head} lower({client}) = ?", (key,)) if client else []
    if matter:
        rows += _in(head, matter, plan.matters)
    return rows


def plan(scope: str, key: str) -> Plan:
    """Everything held for this lawyer, client or matter. Reads only."""
    if scope not in SCOPES:
        raise ValueError(f"a purge is by {', '.join(SCOPES)}, not {scope!r}")
    key = key.strip().lower()
    if not key:
        raise ValueError(f"a {scope} purge needs a {scope}")
    _schemas()
    p = Plan(scope=scope, key=key)
    if scope == "client":
        p.matters = sorted(matters_of(key))

    # Conversations, and what hangs off them.
    threads = [r[0] for r in _by_scope(p, "threads", "id")]
    p.add("version", [r[0] for r in _in("SELECT id FROM blackline_versions WHERE",
                                        "thread_id", threads)])
    for table in ("negotiation_points", "negotiation_sent", "negotiation_rounds"):
        p.add("negotiation", [f"{table}:{r[0]}" for r in
                              _in(f"SELECT id FROM {table} WHERE", "thread_id", threads)])
    p.add("negotiation", [f"negotiation_evidence:{r[0]}" for r in
                          _in("SELECT job_id FROM negotiation_evidence WHERE", "thread_id",
                              threads)])
    p.add("conversation", threads)

    # A closing's id is the thread key of the email that opened it, so one
    # opened before closings recorded a matter is still found through a
    # conversation of the same email.
    closings = [r[0] for r in _by_scope(p, "closings", "id")]
    if scope != "lawyer":
        closings += [r[0] for r in _in("SELECT id FROM closings WHERE", "id", threads)]
    p.add("closing", closings)

    messages = _by_scope(p, "messages", "id, thread_id")
    p.add("message", [r[0] for r in messages])

    # Memory: notes, suppressions, and the outcomes they were learned from.
    notes: list[tuple[str, str, str, str]] = []   # (id, kind, scope, key)
    if scope == "lawyer":
        rows = _q("SELECT id, kind, scope, scope_key FROM memory WHERE "
                  "(scope = 'personal' AND lower(scope_key) = ?) OR lower(owner) = ?",
                  (key, key))
        notes = [(str(r[0]), r[1], r[2], str(r[3]).lower()) for r in rows]
    elif scope == "matter":
        rows = _q("SELECT id, kind, scope, scope_key, provenance FROM memory WHERE "
                  "(scope = 'matter' AND lower(scope_key) = ?) OR scope = 'client'", (key,))
        for r in rows:
            if r[2] == "client":
                # Kept under the client, learned on this matter.
                try:
                    learned_on = str(json.loads(r[4] or "{}").get("matter") or "").lower()
                except ValueError:
                    learned_on = ""
                if learned_on != key:
                    continue
            notes.append((str(r[0]), r[1], r[2], str(r[3]).lower()))
    else:
        rows = _q("SELECT id, kind, scope, scope_key FROM memory WHERE "
                  "scope = 'client' AND lower(scope_key) = ?", (key,))
        rows += _in("SELECT id, kind, scope, scope_key FROM memory WHERE scope = 'matter' AND",
                    "scope_key", p.matters)
        notes = [(str(r[0]), r[1], r[2], str(r[3]).lower()) for r in rows]
    p.add("note", [i for i, kind, _, _ in notes if kind != "suppression"])
    p.add("suppression", [i for i, kind, _, _ in notes if kind == "suppression"])
    p.add("outcome", [r[0] for r in _by_scope(p, "suggestion_outcomes", "id",
                                               owner="user_address")])

    # Anthropic's memory stores: the scope's own are deleted; a store that
    # also holds other work (the client's, when a matter goes; a client's,
    # when a lawyer's suppression under it goes) is rewritten without it.
    if scope == "lawyer":
        own = [("personal", key)]
    elif scope == "matter":
        own = [("matter", key)]
    else:
        own = [("client", key)] + [("matter", m) for m in p.matters]
    stores = {(r[0], str(r[1]).lower()): r[2]
              for r in _q("SELECT scope, scope_key, store_id FROM memory_stores")}
    p.add("store", [f"{s}|{k}|{stores[(s, k)]}" for s, k in own if (s, k) in stores])
    touched = {(s, k) for _, _, s, k in notes} - set(own)
    p.add("projection", [f"{s}|{k}|{stores[(s, k)]}" for s, k in sorted(touched)
                         if (s, k) in stores])

    # Jobs: the audit row says which sessions and uploads were each job's.
    audit_rows = _by_scope(p, "audit_log", "job_id, session_ids, file_ids", owner="sender")
    if scope != "lawyer":
        audit_rows += _in("SELECT job_id, session_ids, file_ids FROM audit_log WHERE",
                          "thread_id", [*threads, *closings])
    jobs = list(dict.fromkeys(r[0] for r in audit_rows))
    for r in audit_rows:
        p.add("session", json.loads(r[1] or "[]"))
        p.add("file", json.loads(r[2] or "[]"))
    held_jobs = [r[0] for r in _in("SELECT id FROM jobs WHERE", "id", jobs)]
    if scope == "lawyer":
        held_jobs += [r[0] for r in _q("SELECT id FROM jobs WHERE lower(sender) = ?", (key,))]
    p.add("job", held_jobs)

    # The archive's read log names what was read: a search, a matter, a thread.
    if scope == "lawyer":
        reads = _q("SELECT id FROM access_log WHERE lower(owner) = ? AND detail != ''", (key,))
    else:
        matters = [key] if scope == "matter" else p.matters
        reads = _in("SELECT id FROM access_log WHERE action = 'by_matter' AND", "detail",
                    matters)
        reads += _in("SELECT id FROM access_log WHERE action = 'thread' AND", "detail",
                     [r[1] for r in messages if r[1]])
    p.add("read", [r[0] for r in reads])

    if scope == "lawyer":
        p.add("access", [key])
    p.add("audit", jobs)

    if scope != "lawyer":
        p.unattributed = _unattributed()
    return p


def _unattributed() -> dict[str, int]:
    """What no client or matter purge can reach: rows recorded with neither."""
    none = "(matter_id IS NULL OR matter_id = '') AND (client_id IS NULL OR client_id = '')"
    out = {
        "conversation": _q(f"SELECT COUNT(*) FROM threads WHERE {none}")[0][0],
        "closing": _q(f"SELECT COUNT(*) FROM closings WHERE {none} "
                      "AND id NOT IN (SELECT id FROM threads)")[0][0],
        "message": _q(f"SELECT COUNT(*) FROM messages WHERE {none}")[0][0],
        # A job is reached through its conversation, when that has a client
        # or matter and is still held.
        "audit": _q(f"SELECT COUNT(*) FROM audit_log WHERE {none} "
                    "AND (purged IS NULL OR purged = '') AND (thread_id IS NULL OR thread_id "
                    f"NOT IN (SELECT id FROM threads WHERE NOT ({none}) "
                    f"UNION SELECT id FROM closings WHERE NOT ({none})))")[0][0],
    }
    return {k: v for k, v in out.items() if v}


# --------------------------------------------------------------------------
# Doing it
# --------------------------------------------------------------------------


@dataclass
class Result:
    plan: Plan
    applied: bool = False
    purge_id: str = ""
    # kind -> how many this purge has deleted, across every run of it.
    counts: dict[str, int] = field(default_factory=dict)
    # (kind, item, error) still to do.
    failed: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def done(self) -> bool:
        return self.applied and not self.failed


def run(scope: str, key: str, *, apply: bool = False, actor: str | None = None) -> Result:
    """Plan, and with `apply` carry the plan out. Running it again finishes
    a purge that failed part way, and is otherwise a purge of whatever has
    been written since."""
    p = plan(scope, key)
    if not apply:
        return Result(plan=p)
    purge_id = _open(p, actor or _actor())
    with connect() as c:
        for kind in KINDS:
            for item in p.items.get(kind, []):
                c.execute("INSERT OR IGNORE INTO purge_items (purge_id, kind, item) "
                          "VALUES (?, ?, ?)", (purge_id, kind, item))
    pending = _q("SELECT kind, item FROM purge_items WHERE purge_id = ? AND done_at IS NULL",
                 (purge_id,))
    order = {k: i for i, k in enumerate(KINDS)}
    for kind, item in sorted(pending, key=lambda r: (order.get(r[0], 99), r[1])):
        try:
            _DO[kind](item, purge_id)
        except Exception as e:  # noqa: BLE001 - recorded, and the rest carry on
            log.warning("purge %s: %s %s failed: %s", purge_id, kind, item, e)
            with connect() as c:
                c.execute("UPDATE purge_items SET error = ? WHERE purge_id = ? AND kind = ? "
                          "AND item = ?", (str(e)[:300] or type(e).__name__, purge_id, kind,
                                           item))
            continue
        with connect() as c:
            c.execute("UPDATE purge_items SET done_at = ?, error = NULL WHERE purge_id = ? "
                      "AND kind = ? AND item = ?", (_now(), purge_id, kind, item))
    return _close(p, purge_id)


def _actor() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - no login name in a container
        return "unknown"


def _open(p: Plan, actor: str) -> str:
    """The unfinished purge of this scope, to finish, or a new one."""
    init()
    row = _q("SELECT id FROM purges WHERE scope = ? AND scope_key = ? AND finished_at IS NULL "
             "ORDER BY started_at DESC LIMIT 1", (p.scope, p.key))
    if row:
        return row[0][0]
    purge_id = secrets.token_hex(8)
    with connect() as c:
        c.execute("INSERT INTO purges (id, scope, scope_key, actor, started_at, counts) "
                  "VALUES (?, ?, ?, ?, ?, '{}')", (purge_id, p.scope, p.key, actor, _now()))
    return purge_id


def _close(p: Plan, purge_id: str) -> Result:
    """Count what is done and, when everything is, finish the record and
    forget the items: the record keeps how many of each, not which."""
    counts: dict[str, int] = {}
    for kind, n in _q("SELECT kind, COUNT(*) FROM purge_items WHERE purge_id = ? "
                      "AND done_at IS NOT NULL GROUP BY kind", (purge_id,)):
        counts[kind] = n
    failed = [(r[0], r[1], r[2] or "") for r in
              _q("SELECT kind, item, error FROM purge_items WHERE purge_id = ? "
                 "AND done_at IS NULL ORDER BY kind, item", (purge_id,))]
    with connect() as c:
        if failed:
            c.execute("UPDATE purges SET counts = ? WHERE id = ?",
                      (json.dumps(counts, sort_keys=True), purge_id))
        else:
            c.execute("UPDATE purges SET counts = ?, finished_at = ? WHERE id = ?",
                      (json.dumps(counts, sort_keys=True), _now(), purge_id))
            c.execute("DELETE FROM purge_items WHERE purge_id = ?", (purge_id,))
    return Result(plan=p, applied=True, purge_id=purge_id, counts=counts, failed=failed)


def history() -> list[dict]:
    """Every purge, newest first: the content-free record each leaves."""
    init()
    rows = _q("SELECT id, scope, scope_key, actor, started_at, finished_at, counts "
              "FROM purges ORDER BY started_at DESC")
    return [{"id": r[0], "scope": r[1], "scope_key": r[2], "actor": r[3],
             "started_at": r[4], "finished_at": r[5], "counts": json.loads(r[6] or "{}")}
            for r in rows]


# --- our rows: the stored document first, its row after ------------------------


def _version(item: str, _purge_id: str) -> None:
    rows = _q("SELECT content FROM blackline_versions WHERE id = ?", (item,))
    for (stored,) in rows:
        blobs.delete(stored)
    with connect() as c:
        c.execute("DELETE FROM blackline_versions WHERE id = ?", (item,))


def _negotiation(item: str, _purge_id: str) -> None:
    table, _, row_id = item.partition(":")
    if table == "negotiation_evidence":
        for (stored,) in _q("SELECT evidence FROM negotiation_evidence WHERE job_id = ?",
                            (row_id,)):
            blobs.delete(stored)
        with connect() as c:
            c.execute("DELETE FROM negotiation_evidence WHERE job_id = ?", (row_id,))
        return
    if table not in ("negotiation_points", "negotiation_sent", "negotiation_rounds"):
        raise ValueError(f"not a negotiation table: {table}")
    if table == "negotiation_sent":
        for (stored,) in _q("SELECT content FROM negotiation_sent WHERE id = ?", (row_id,)):
            blobs.delete(stored)
    with connect() as c:
        c.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))


def _conversation(item: str, _purge_id: str) -> None:
    for (stored,) in _q("SELECT original FROM threads WHERE id = ?", (item,)):
        blobs.delete(stored)
    with connect() as c:
        # Children first, the thread row last, as thread.purge does.
        for table in ("changes", "questions", "thread_findings", "thread_aliases"):
            c.execute(f"DELETE FROM {table} WHERE thread_id = ?", (item,))
        c.execute("DELETE FROM threads WHERE id = ?", (item,))


def _closing(item: str, _purge_id: str) -> None:
    for name, stored in _q("SELECT name, blob FROM closing_files WHERE closing_id = ?",
                           (item,)):
        blobs.delete(stored)
        with connect() as c:
            c.execute("DELETE FROM closing_files WHERE closing_id = ? AND name = ?",
                      (item, name))
    with connect() as c:
        c.execute("DELETE FROM closing_aliases WHERE closing_id = ?", (item,))
        c.execute("DELETE FROM closings WHERE id = ?", (item,))


def _message(item: str, _purge_id: str) -> None:
    for att_id, stored in _q("SELECT id, blob FROM attachments WHERE message_pk = ?", (item,)):
        blobs.delete(stored)
        with connect() as c:
            c.execute("DELETE FROM attachments WHERE id = ?", (att_id,))
    with connect() as c:
        c.execute("DELETE FROM message_fts WHERE message_pk = ?", (item,))
        c.execute("DELETE FROM messages WHERE id = ?", (item,))


def _row(table: str, column: str = "id"):
    def delete(item: str, _purge_id: str) -> None:
        with connect() as c:
            c.execute(f"DELETE FROM {table} WHERE {column} = ?", (item,))
    return delete


def _read(item: str, _purge_id: str) -> None:
    with connect() as c:
        c.execute("UPDATE access_log SET detail = '' WHERE id = ?", (item,))


def _access(item: str, _purge_id: str) -> None:
    from secondeye import dms_mcp, oauth

    oauth.revoke(item)
    if dms_mcp.enabled():
        dms_mcp.revoke(item)


def _redact(item: str, purge_id: str) -> None:
    """Keep the audit row and blank what names the client's work.

    The audit trail is the firm's record of supervising the tool (ABA Formal
    Opinion 512; docs/trust.md): who used it, when, what ran, on which model,
    where the reply went and how it ended. That stays, and deleting it would
    let a purge erase the evidence of what the tool did. What the row holds
    about the client's work goes: the documents' and returned files' names,
    the matter, the client, the conversation, the message id and the triage
    plan (which quotes file names), and the session and file ids, whose
    objects the purge has just deleted. `purged` says which purge did it."""
    with connect() as c:
        c.execute("UPDATE audit_log SET message_id = '', matter_id = NULL, client_id = NULL, "
                  "thread_id = NULL, documents = '[]', returned = '[]', session_ids = '[]', "
                  "file_ids = '[]', triage = NULL, purged = ?, updated_at = ? "
                  "WHERE job_id = ?", (purge_id, _now(), item))


# --- Anthropic's copies ----------------------------------------------------------


def _anthropic():
    """The client every other call is made with (config.anthropic_client)."""
    from secondeye import config

    if not config.settings().anthropic_api_key.strip():
        raise RuntimeError("ANTHROPIC_API_KEY is not set where this ran, so Anthropic's "
                           "copies could not be deleted")
    return config.anthropic_client()


def _gone(call, *args, **kwargs) -> None:
    """Make a delete call; an object already gone is deleted."""
    import anthropic

    try:
        call(*args, **kwargs)
    except anthropic.NotFoundError:
        pass


def _store(item: str, _purge_id: str) -> None:
    scope, key, store_id = item.split("|", 2)
    # Deleting a store deletes every memory and version in it.
    _gone(_anthropic().beta.memory_stores.delete, store_id)
    with connect() as c:
        c.execute("DELETE FROM memory_stores WHERE scope = ? AND scope_key = ? AND store_id = ?",
                  (scope, key, store_id))


def _projection(item: str, _purge_id: str) -> None:
    from secondeye import memory

    scope, key, _ = item.split("|", 2)
    _anthropic()
    if not memory.project(memory.Scope(scope), key):
        raise RuntimeError(f"the {scope} memory store for {key} could not be rewritten")


def _session(item: str, _purge_id: str) -> None:
    """Delete a session and every file it wrote. One still running is
    interrupted and left for the next run: a running session cannot be
    deleted, and it stops at its next safe boundary."""
    import anthropic

    from secondeye import managed

    client = _anthropic()
    try:
        status = str(getattr(client.beta.sessions.retrieve(item), "status", "") or "")
    except anthropic.NotFoundError:
        return
    if status in managed.ACTIVE:
        managed.interrupt(client, item)
        raise RuntimeError("the session was still running; it has been interrupted, so run "
                           "the purge again in a minute")
    for meta in client.beta.files.list(scope_id=item, betas=managed.BETAS):
        _gone(client.beta.files.delete, meta.id)
    _gone(client.beta.sessions.delete, item)


def _file(item: str, _purge_id: str) -> None:
    _gone(_anthropic().beta.files.delete, item)


_DO = {
    "version": _version,
    "negotiation": _negotiation,
    "conversation": _conversation,
    "closing": _closing,
    "message": _message,
    "note": _row("memory"),
    "suppression": _row("memory"),
    "outcome": _row("suggestion_outcomes"),
    "job": _row("jobs"),
    "read": _read,
    "store": _store,
    "projection": _projection,
    "session": _session,
    "file": _file,
    "access": _access,
    "audit": _redact,
}


__all__ = ["ANTHROPIC", "KINDS", "Plan", "Result", "history", "init", "matters_of", "plan",
           "run"]
