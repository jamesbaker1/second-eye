"""Memory in four scopes: firm, personal, client, matter, walled by client.

Storage is deliberately boring. Structured records with a scope, a confidence,
and provenance live in our database, which is the source of truth. What the
agent reads is an Anthropic memory store per scope (DECISIONS 29): one for the
firm, one per lawyer, one per matter, each mounted read-only into the review
session, where the agent reads it with its file tools. After every write here
the scope's entries are rendered into one small Markdown file in its store, so
the store is a projection of the table, never a second source.

Why a projection rather than letting the agent write the store: a memory store
holds files, not records. It has no confidence, no sample count and no
provenance, and those are what let a note planted by a forwarded draft be
traced to the review that wrote it and deleted (docs/memory.md). The table
keeps them; the store gets the sentence. The ledger of suggestion outcomes,
which is training signal rather than memory, stays in the table only.

The wall (ABA Formal Opinion 512, docs/memory.md): anything learned from a
client's documents is kept under that client (CLIENT) or that matter (MATTER)
and read only there. PERSONAL follows a lawyer onto every client's documents,
so it takes only the kinds that can be drafting style, and only when the
sentence carries no client content (`personal_ok`). FIRM is read by every
review and is written only from the firm's own playbook. `remember` refuses
anything else, and `enforce_walls` moves or drops rows that predate the wall.

The value is in the accept/reject loop (see docs/memory.md), not in retrieval
sophistication.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

from secondeye import clients
from secondeye.store import connect

log = logging.getLogger(__name__)

MEMORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,          -- firm | personal | client | matter
    scope_key TEXT NOT NULL,      -- '' | user address | client number | matter id
    kind TEXT NOT NULL,           -- convention | preference | suppression | position
    statement TEXT NOT NULL,      -- one sentence, human readable, human editable
    confidence REAL NOT NULL,
    samples INTEGER NOT NULL DEFAULT 1,
    provenance TEXT,              -- json: where this came from
    status TEXT NOT NULL DEFAULT 'confirmed',  -- confirmed | pending | lifted
    -- When the lawyer was told about this note, for the notes a review reply
    -- says it learned ("I've stopped flagging X for you"). Null: not yet.
    announced_at TEXT,
    -- The lawyer an entry is about, when it is about one: a personal entry's
    -- lawyer, or a suppression kept under a client or matter because what
    -- it names came from that client's documents. Null: the whole matter's.
    owner TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory(scope, scope_key);

-- Every suggestion the agent made, and what happened to it. This table is the
-- training signal; everything else in memory is derived from it.
CREATE TABLE IF NOT EXISTS suggestion_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT,
    user_address TEXT,
    matter_id TEXT,
    category TEXT,
    severity TEXT,
    title TEXT,
    anchor TEXT,
    suggested_text TEXT,
    outcome TEXT,                 -- accepted | rejected | unknown
    observed_at TEXT,
    client_id TEXT                -- the matter's client, when known: what a rate is walled by
);
CREATE INDEX IF NOT EXISTS idx_outcomes_user ON suggestion_outcomes(user_address, category);

-- Which Anthropic memory store holds each scope's projection. Created on
-- first use; the firm's comes from settings.
CREATE TABLE IF NOT EXISTS memory_stores (
    scope TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    store_id TEXT NOT NULL,
    created_at TEXT,
    PRIMARY KEY (scope, scope_key)
);
"""


class Scope(str, Enum):
    FIRM = "firm"
    PERSONAL = "personal"
    CLIENT = "client"
    MATTER = "matter"


class Status(str, Enum):
    # Read into every later review for its scope.
    CONFIRMED = "confirmed"
    # Written by the agent and not yet agreed by the lawyer. Kept, so it can be
    # shown and confirmed, but never read back into a review: the text that
    # prompted it may have come from a document someone outside the firm wrote.
    PENDING = "pending"
    # A suppression learned from undos that the lawyer then asked to have
    # back ("flag it again"). Never read into a review, and kept rather than
    # deleted: the undos that made it are still counted, and a deleted one
    # was learned again on the next undo and announced all over again.
    LIFTED = "lifted"


class Kind(str, Enum):
    CONVENTION = "convention"    # "this firm writes dates as January 1, 2026" (playbook)
    PREFERENCE = "preference"    # "Jim uses 'will', not 'shall'"
    SUPPRESSION = "suppression"  # "stop flagging passive voice for Jim"
    POSITION = "position"        # "Acme has never accepted an uncapped indemnity"


# Which kinds each scope may hold. PERSONAL is read on every client's
# documents, so it holds only the kinds that can be pure drafting style, and
# `personal_ok` then checks the sentence itself. A position observed on a
# deal is never personal. FIRM holds the playbook's conventions and nothing
# a review saw.
ALLOWED_KINDS = {
    Scope.FIRM: {Kind.CONVENTION},
    Scope.PERSONAL: {Kind.PREFERENCE, Kind.SUPPRESSION},
    Scope.CLIENT: {Kind.PREFERENCE, Kind.SUPPRESSION, Kind.POSITION},
    Scope.MATTER: {Kind.PREFERENCE, Kind.SUPPRESSION, Kind.POSITION},
}


class WallError(ValueError):
    """An entry that would carry one client's content somewhere another
    client's review reads it."""


@dataclass
class MemoryEntry:
    scope: Scope
    scope_key: str
    kind: Kind
    statement: str
    confidence: float
    samples: int = 1
    provenance: dict | None = None
    status: Status = Status.CONFIRMED
    id: int | None = None
    owner: str = ""

    def as_prompt_line(self) -> str:
        hedge = "" if self.confidence >= 0.8 else " (low confidence, treat as a hint)"
        # A client or matter store is read by every lawyer on it; a
        # suppression kept there is one lawyer's, and says whose.
        walled = self.scope in (Scope.CLIENT, Scope.MATTER)
        who = f"For {self.owner}: " if self.owner and walled else ""
        return f"- [{self.kind.value}] {who}{self.statement}{hedge}"


# --------------------------------------------------------------------------
# The wall
# --------------------------------------------------------------------------

# Categories of change that are drafting mechanics, never deal content. A
# suppression learned from undos or dismissals of one of these may follow a
# lawyer across clients; any other category was named after what a document
# said, and stays with the client it came from.
STYLE_LABELS = frozenset({
    "defined-term", "defined-terms", "cross-reference", "cross-references", "numbering",
    "date", "date-format", "amount", "party-name", "placeholder", "leftovers",
    "formatting", "style", "typo", "spelling", "punctuation", "drafting",
    "passive-voice", "capitalisation", "capitalization", "oxford-comma", "shall-will",
    "grammar", "whitespace", "headings", "consistency", "house-style",
})

# What a sentence about drafting style talks about. A personal note has to
# name one of these to be personal at all; "Jim accepts uncapped liability"
# names none. Modals count only in quotes: "Acme will not accept a cap" is
# about Acme, "'will', not 'shall'" is about drafting.
_STYLE = re.compile(
    r"""['"‘“](?:shall|will|must|may)['"’”]|\bshall\b|"""
    r"\b(?:oxford|serial) comma|\bpassive(?: voice)?\b|\bcapitali[sz]|\bdefined[- ]terms?\b|"
    r"\bdate[- ]formats?\b|\bdates?\b(?= as\b| in the form| written)|\bwrit\w* dates\b|"
    r"\bnumbering\b|\bcross[- ]ref|\bheadings?\b|\bfonts?\b|\bspelling\b|\btypos?\b|"
    r"\bpunctuation\b|\bhyphen|\b(?:em|en)[- ]dash|\bdouble spac|\bnumerals?\b|"
    r"\bwords and (?:figures|numerals|numbers)\b|\bampersand|\bplain english\b|"
    r"\blegalese\b|\bhere(?:in|by|of|under|to)\b|\bgender[- ]neutral\b|\bcontractions?\b|"
    r"\b(?:british|american|us|uk) (?:english|spelling)\b|\bformatting\b|"
    r"\bquotation marks\b|\bbold\b|\bitalics?\b|\bstyle\b",
    re.IGNORECASE,
)

# Capitalised words mid-sentence are names, and names are client content,
# except these.
_NOT_NAMES = frozenset({
    "oxford", "british", "american", "english", "uk", "us", "usa", "word", "latin",
    "roman", "arabic", "iso", "bluebook", "i", "ok", "january", "february", "march",
    "april", "may", "june", "july", "august", "september", "october", "november",
    "december", "monday", "tuesday", "wednesday", "thursday", "friday",
})
_MID_SENTENCE_NAME = re.compile(r"(?<=[a-z0-9,;)]\s)([A-Z][\w&'-]+)|\b([A-Z]{3,})\b")
_CLIENTISH = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+|https?://|www\.|[$£€¥]\s?\d|\d[\d,.]*\s?%")


def carries_client_content(text: str) -> bool:
    """Whether a sentence names anything a client could own: a registered
    client's number, name or domain, an address, a sum, or a proper name.
    Deliberately over-eager: a style note refused here is kept under the
    matter instead, which costs a lawyer one repetition on the next client."""
    lowered = text.lower()
    for marker in clients.markers():
        if re.search(rf"(?<![\w]){re.escape(marker.lower())}(?![\w])", lowered):
            return True
    if _CLIENTISH.search(text):
        return True
    for m in _MID_SENTENCE_NAME.finditer(text):
        word = (m.group(1) or m.group(2)).strip("'")
        if word.lower() not in _NOT_NAMES:
            return True
    return False


def is_style(text: str) -> bool:
    """Drafting style, with nothing in it from a client."""
    return bool(_STYLE.search(text)) and not carries_client_content(text)


def personal_ok(kind: Kind, statement: str, provenance: dict | None = None) -> bool:
    """Whether an entry may be kept as a lawyer's own, read on every client's
    documents. By kind first: only preferences and suppressions can be
    personal. Then by where the words came from:

    - a suppression learned from undos is its category, which a model named,
      so it must be one of ours (STYLE_LABELS);
    - "stop flagging X" is the lawyer's own words about their own review, so
      it is personal unless X carries client content ("the governing law
      clause" is theirs everywhere; "the Acme change of control point" is
      Acme's);
    - anything else (a note an agent wrote after reading a document) must be
      drafting style as well as clean of client content."""
    if kind not in ALLOWED_KINDS[Scope.PERSONAL]:
        return False
    provenance = provenance or {}
    if kind is Kind.SUPPRESSION and provenance.get("source") in _LEARNED:
        label = provenance.get("label") or ""
        if not label:
            m = _LEARNED_LABEL.search(statement)
            label = m.group(1) if m else ""
        return label in STYLE_LABELS
    if kind is Kind.SUPPRESSION and provenance.get("source") == "reply":
        return not carries_client_content(statement)
    return is_style(statement)


def wall_refusal(entry: MemoryEntry) -> str | None:
    """Why an entry cannot be kept where it says, or None."""
    if entry.kind not in ALLOWED_KINDS[entry.scope]:
        return f"{entry.kind.value} entries cannot be kept in {entry.scope.value} scope"
    if entry.scope is Scope.FIRM and (entry.provenance or {}).get("source") != "playbook":
        return "firm memory is written only from the firm's own playbook"
    if entry.scope is Scope.PERSONAL and not personal_ok(entry.kind, entry.statement,
                                                           entry.provenance):
        return "personal memory holds drafting style only, with no client content"
    if entry.scope in (Scope.CLIENT, Scope.MATTER) and not entry.scope_key:
        return f"a {entry.scope.value} entry needs its {entry.scope.value}"
    return None


def walled_scope(matter_id: str | None, client_id: str | None = None
                 ) -> tuple[Scope, str] | None:
    """Where something learned on a matter is kept when it cannot be
    personal: under its client, when that is known, so the client's other
    matters read it; otherwise under the matter alone; otherwise nowhere."""
    client_id = client_id or clients.for_matter(matter_id)
    if client_id:
        return Scope.CLIENT, client_id
    if matter_id:
        return Scope.MATTER, matter_id
    return None


_migrated: set[str] = set()


def init() -> None:
    from secondeye.config import settings

    url = settings().database_url
    with connect() as c:
        if url not in _migrated:
            # Databases created before notes could be pending. Everything in
            # them was written before the gate existed; the default keeps it
            # readable, and `pending_notes` is how an agent-written one found
            # later is reviewed. The index needs the column, so it waits.
            c.executescript(MEMORY_SCHEMA.replace(
                "CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory(scope, scope_key);", ""))
            columns = {r[1] for r in c.execute("PRAGMA table_info(memory)")}
            if "status" not in columns:
                c.execute("ALTER TABLE memory ADD COLUMN status TEXT NOT NULL "
                          "DEFAULT 'confirmed'")
            # Databases from before a review said what it had learned. A
            # "stop flagging X" the lawyer wrote was confirmed to them in a
            # reply of its own, so it counts as told; one learned from undos
            # never was, and the next review says so once.
            if "announced_at" not in columns:
                c.execute("ALTER TABLE memory ADD COLUMN announced_at TEXT")
                c.execute("UPDATE memory SET announced_at = COALESCE(created_at, ?) "
                          "WHERE kind = ? AND provenance LIKE ?",
                          (datetime.now(UTC).isoformat(), Kind.SUPPRESSION.value,
                           '%"source": "reply"%'))
            # Databases from before memory was walled by client. A personal
            # entry is its lawyer's; a lift or an announcement asks by owner.
            if "owner" not in columns:
                c.execute("ALTER TABLE memory ADD COLUMN owner TEXT")
                c.execute("UPDATE memory SET owner = lower(scope_key) WHERE scope = ?",
                          (Scope.PERSONAL.value,))
            outcome_columns = {r[1] for r in c.execute("PRAGMA table_info(suggestion_outcomes)")}
            if "client_id" not in outcome_columns:
                c.execute("ALTER TABLE suggestion_outcomes ADD COLUMN client_id TEXT")
            _migrated.add(url)
            wall = True
        else:
            wall = False
        c.executescript(MEMORY_SCHEMA)
    if wall:
        # Once per database per process: rows written before the wall that
        # would carry one client's content onto another's documents.
        enforce_walls()


def remember(entry: MemoryEntry) -> None:
    """Keep an entry, if the wall allows it where it says. Raises WallError
    when it does not: every writer decides where a thing belongs before it
    gets here (`walled_scope`), so a refusal is a bug, never a user's error."""
    refusal = wall_refusal(entry)
    if refusal:
        raise WallError(refusal)
    now = datetime.now(UTC).isoformat()
    owner = entry.owner or (entry.scope_key.lower() if entry.scope is Scope.PERSONAL else "")
    init()
    with connect() as c:
        c.execute(
            "INSERT INTO memory (scope, scope_key, kind, statement, confidence, samples, "
            "provenance, status, owner, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry.scope.value,
                entry.scope_key,
                entry.kind.value,
                entry.statement,
                entry.confidence,
                entry.samples,
                json.dumps(entry.provenance or {}),
                entry.status.value,
                owner or None,
                now,
                now,
            ),
        )
    if entry.status is Status.CONFIRMED:
        project(entry.scope, entry.scope_key)


def _row(r) -> MemoryEntry:
    keys = r.keys()
    return MemoryEntry(
        scope=Scope(r["scope"]), scope_key=r["scope_key"], kind=Kind(r["kind"]),
        statement=r["statement"], confidence=r["confidence"], samples=r["samples"],
        provenance=json.loads(r["provenance"] or "{}"),
        status=Status(r["status"] or Status.CONFIRMED.value), id=r["id"],
        owner=(r["owner"] if "owner" in keys else "") or "",
    )


def enforce_walls() -> int:
    """Move or drop every row that sits on the wrong side of the wall, and
    re-project the stores it touched. Returns how many rows changed.

    Run once per database when this code first meets it, and again whenever
    a client is registered (a name becomes client content the moment the
    firm says whose it is). A personal entry that is not pure style moves to
    the matter it was learned on, which is the narrowest wall there is; with
    no matter recorded nobody can say whose it was, and it is deleted. A firm
    entry that did not come from the playbook is deleted."""
    with connect() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute("SELECT * FROM memory WHERE scope IN (?, ?)",
                         (Scope.PERSONAL.value, Scope.FIRM.value)).fetchall()
    touched: set[tuple[Scope, str]] = set()
    changed = 0
    for r in rows:
        entry = _row(r)
        if not wall_refusal(entry):
            continue
        matter = str((entry.provenance or {}).get("matter") or "").strip()
        with connect() as c:
            if entry.scope is Scope.PERSONAL and matter and entry.kind in ALLOWED_KINDS[Scope.MATTER]:
                c.execute("UPDATE memory SET scope = ?, scope_key = ?, owner = ?, updated_at = ? "
                          "WHERE id = ?", (Scope.MATTER.value, matter, entry.scope_key.lower(),
                                           datetime.now(UTC).isoformat(), entry.id))
                touched.add((Scope.MATTER, matter))
                log.warning("memory %s moved from personal to matter %s: not pure style",
                            entry.id, matter)
            else:
                c.execute("DELETE FROM memory WHERE id = ?", (entry.id,))
                log.warning("memory %s (%s) deleted: it would cross the client wall",
                            entry.id, entry.scope.value)
        touched.add((entry.scope, entry.scope_key))
        changed += 1
    for scope, key in touched:
        project(scope, key)
    return changed


def pending_notes(user_address: str, matter_id: str | None = None) -> list[MemoryEntry]:
    """Notes the agent wrote for this lawyer, or on this matter or its
    client, that nobody has confirmed. What a reply shows the lawyer so they
    can say yes or no."""
    init()
    clauses = ["(scope = ? AND lower(scope_key) = lower(?))"]
    args: list[str] = [Scope.PERSONAL.value, user_address]
    if matter_id:
        clauses.append("(scope = ? AND scope_key = ?)")
        args += [Scope.MATTER.value, matter_id]
        client = clients.for_matter(matter_id)
        if client:
            clauses.append("(scope = ? AND scope_key = ?)")
            args += [Scope.CLIENT.value, client]
    with connect() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            f"SELECT * FROM memory WHERE status = ? AND ({' OR '.join(clauses)}) ORDER BY id",
            (Status.PENDING.value, *args),
        ).fetchall()
    return [_row(r) for r in rows]


def confirm_notes(user_address: str, ids: list[int] | None = None,
                  matter_id: str | None = None) -> int:
    """The lawyer agreed: these pending notes become memory every later review
    reads. `ids=None` confirms all of this lawyer's pending notes (and the
    matter's, when given). Only ever called from the lawyer's own reply.
    Returns how many were confirmed."""
    wanted = {e.id for e in pending_notes(user_address, matter_id)}
    if ids is not None:
        wanted &= set(ids)
    if not wanted:
        return 0
    marks = ",".join("?" * len(wanted))
    with connect() as c:
        c.row_factory = sqlite3.Row
        touched = c.execute(
            f"SELECT DISTINCT scope, scope_key FROM memory WHERE id IN ({marks})",
            tuple(wanted)).fetchall()
        c.execute(f"UPDATE memory SET status = ?, updated_at = ? WHERE id IN ({marks})",
                  (Status.CONFIRMED.value, datetime.now(UTC).isoformat(), *wanted))
    for r in touched:
        project(Scope(r["scope"]), r["scope_key"])
    return len(wanted)


def learning() -> bool:
    """Whether undos, dismissals and sent versions are learned from without
    the lawyer asking (LEARN_FROM_OUTCOMES). Off by default."""
    from secondeye.config import settings

    return settings().learn_from_outcomes


def expire(cutoff: str) -> int:
    """The retention sweep's part (retention.py): pending notes nobody
    confirmed by `cutoff` are deleted, and, unless LEARN_FROM_OUTCOMES is on,
    the record of what was kept, undone or dismissed. A pending note is a
    sentence the agent took from a document; unconfirmed, it is never read,
    so keeping it has no use. Confirmed memory is untouched: the lawyer asked
    for it. Returns how many rows went."""
    init()
    with connect() as c:
        gone = c.execute("DELETE FROM memory WHERE status = ? AND created_at < ?",
                         (Status.PENDING.value, cutoff)).rowcount
        if not learning():
            gone += c.execute("DELETE FROM suggestion_outcomes WHERE observed_at < ?",
                              (cutoff,)).rowcount
    return max(gone, 0)


def discard_notes(user_address: str, ids: list[int] | None = None,
                  matter_id: str | None = None) -> int:
    """The lawyer said no, or never answered: drop pending notes. Confirmed
    memory is untouched."""
    wanted = {e.id for e in pending_notes(user_address, matter_id)}
    if ids is not None:
        wanted &= set(ids)
    if not wanted:
        return 0
    marks = ",".join("?" * len(wanted))
    with connect() as c:
        c.execute(f"DELETE FROM memory WHERE status = ? AND id IN ({marks})",
                  (Status.PENDING.value, *wanted))
    return len(wanted)


# --------------------------------------------------------------------------
# Notes an agent writes
# --------------------------------------------------------------------------

# Memory written during a review is read back into every future review for that
# lawyer, and the text that prompts a write can come from a document an
# outsider sent in. Firm scope is refused outright; these two bounds are what
# keeps the scopes that remain from being used as a channel. One sentence, a
# few times, is what a note is for.
MAX_NOTE_CHARACTERS = 300
MAX_NOTES_PER_REVIEW = 3


def note_refusal(statement: str, scope: str, matter_id: str | None,
                 already: int) -> str | None:
    """Why an agent's note cannot be kept, in a sentence the agent can act on,
    or None when it can. `statement` is already stripped; `already` is how
    many notes this review has kept. The same rules whichever way the note
    arrives: the `note_for_next_time` tool on a streamed session, or
    `notes.json` from a detached one (`accept_agent_notes`)."""
    try:
        s = Scope(scope)
    except ValueError:
        return f"'{scope}' is not a valid scope. Use personal, client or matter."
    if s is Scope.FIRM:
        # Firm memory is read by every lawyer's reviews on every client's
        # documents, so nothing a review saw may write to it. It comes from
        # the firm's own playbook, approved by a playbook admin.
        return ("Firm-wide memory cannot be written from a review. Record this as "
                "personal (drafting style only), client or matter scope instead.")
    if s is Scope.MATTER and not matter_id:
        return "No matter identified, so a matter-scoped note would be unattached."
    if s is Scope.CLIENT and not clients.for_matter(matter_id):
        return ("No client identified for this matter, so nothing can be kept across its "
                "client's matters. Record this as matter scope instead."
                if matter_id else
                "No client or matter identified, so this cannot be kept beyond this review.")
    if not statement:
        return "Nothing to note: the statement was empty."
    if len(statement) > MAX_NOTE_CHARACTERS:
        return (f"That note is {len(statement)} characters. A memory entry is one "
                f"sentence someone will read back in six months; keep it under "
                f"{MAX_NOTE_CHARACTERS}.")
    if already >= MAX_NOTES_PER_REVIEW:
        return (f"I have already recorded {MAX_NOTES_PER_REVIEW} notes for this "
                "review, which is as many durable facts as one document yields. "
                "Put anything else in the finding instead.")
    if s is Scope.PERSONAL and not personal_ok(Kind.PREFERENCE, statement):
        # A personal note follows this lawyer onto every client's documents.
        return ("A personal note is for drafting style only (for example \"uses 'will', "
                "not 'shall'\" or how dates are written) and must not name a party, a "
                "client or a sum, because it is read on every client's documents. "
                "Anything learned from this document belongs to its matter: record it "
                "as matter or client scope instead.")
    return None


def accept_agent_notes(notes, user_address: str, matter_id: str | None,
                       document: str = "") -> list[str]:
    """Keep what a detached review left in `notes.json`, under the tool's rules.

    A session with no client attached cannot call `note_for_next_time`, so it
    writes its notes to a file instead. That file came out of a sandbox that
    read a document someone outside the firm may have written, so it is held
    to exactly the rules the tool applies (`note_refusal`), and every note is
    stored pending: a review has no instruction from the lawyer, only a
    document, and a document is never allowed to confirm its own note.

    `notes` is the file as bytes or text, or already parsed: a list of
    `{statement, scope, confidence}` objects or `{"notes": [...]}`. Returns
    one line per note saying what became of it, for the log.
    """
    if isinstance(notes, (bytes, str)):
        try:
            notes = json.loads(notes)
        except ValueError as e:
            log.warning("notes.json could not be read: %s", e)
            return [f"notes.json could not be read: {e}"]
    if isinstance(notes, dict):
        notes = notes.get("notes")
    if not isinstance(notes, list):
        return ["notes.json holds no list of notes"]

    out: list[str] = []
    kept = 0
    for item in notes:
        if not isinstance(item, dict) or not isinstance(item.get("statement"), str):
            out.append("Not kept: a note needs a statement.")
            continue
        statement = item["statement"].strip()
        scope = str(item.get("scope") or Scope.PERSONAL.value)
        refusal = note_refusal(statement, scope, matter_id, kept)
        if refusal:
            out.append(f"Not kept: {refusal}")
            continue
        try:
            confidence = min(1.0, max(0.0, float(item.get("confidence", 0.6))))
        except (TypeError, ValueError):
            confidence = 0.6
        s = Scope(scope)
        remember(MemoryEntry(
            scope=s, scope_key=note_key(s, user_address, matter_id),
            kind=note_kind(s), statement=statement, confidence=confidence,
            status=Status.PENDING,
            provenance={"source": "agent", "via": "notes.json", "user": user_address,
                        "document": document, "matter": matter_id or "", "asked": False},
        ))
        kept += 1
        out.append(f"Noted ({s.value}), pending the lawyer's confirmation: {statement}")
    return out


def note_key(scope: Scope, user_address: str, matter_id: str | None) -> str:
    """Whose an agent's note is, by the scope it asked for."""
    if scope is Scope.PERSONAL:
        return user_address
    if scope is Scope.CLIENT:
        return clients.for_matter(matter_id) or ""
    return matter_id or ""


def note_kind(scope: Scope) -> Kind:
    """A personal note can only be a style preference; one kept under a
    client or matter is something observed on the deal."""
    return Kind.PREFERENCE if scope is Scope.PERSONAL else Kind.POSITION


def recall(user_address: str, matter_id: str | None = None,
           client_id: str | None = None) -> list[MemoryEntry]:
    """Everything a review should know, for this lawyer and this matter: the
    same entries the mounted stores hold (`stores_for`).

    Ordering matters: firm conventions first so personal preferences visibly
    override them, then the client's, then the matter's, the most specific.
    Another lawyer's suppression under the client or matter is theirs.
    """
    init()
    client_id = client_id or clients.for_matter(matter_id)
    scopes: list[tuple[str, str]] = [(Scope.FIRM.value, ""), (Scope.PERSONAL.value, user_address)]
    if client_id:
        scopes.append((Scope.CLIENT.value, client_id))
    if matter_id:
        scopes.append((Scope.MATTER.value, matter_id))

    out: list[MemoryEntry] = []
    with connect() as c:
        c.row_factory = sqlite3.Row
        for scope, key in scopes:
            rows = c.execute(
                "SELECT * FROM memory WHERE scope = ? AND lower(scope_key) = lower(?) "
                "AND status = ? AND (owner IS NULL OR owner = '' OR lower(owner) = lower(?)) "
                "ORDER BY confidence DESC LIMIT 60",
                (scope, key, Status.CONFIRMED.value, user_address),
            ).fetchall()
            out.extend(_row(r) for r in rows)
    return out


_LABELS = {
    Scope.FIRM: "House conventions (firm-wide, from the firm's playbook)",
    Scope.PERSONAL: "This lawyer's drafting style (these override firm conventions)",
    Scope.CLIENT: "This client, learned on its matters (use it only for this client)",
    Scope.MATTER: "This matter (most specific, use it)",
}


def as_prompt_block(entries: list[MemoryEntry]) -> str:
    """Entries rendered for the model. This is what each scope's memory-store
    file holds; nothing splices it into a prompt any more."""
    if not entries:
        return ""
    by_scope: dict[Scope, list[MemoryEntry]] = {}
    for e in entries:
        by_scope.setdefault(e.scope, []).append(e)

    parts = ["# What you know about this firm, this lawyer, and this matter", ""]
    for scope in (Scope.FIRM, Scope.PERSONAL, Scope.CLIENT, Scope.MATTER):
        if scope in by_scope:
            parts.append(f"## {_LABELS[scope]}")
            parts.extend(e.as_prompt_line() for e in by_scope[scope])
            parts.append("")
    parts.append(
        "When you flag something because of one of these rather than because it is "
        "objectively wrong, say so in the explanation. The lawyer needs to be able "
        "to tell a rule from a habit."
    )
    return "\n".join(parts)


# --------------------------------------------------------------------------
# The Anthropic memory stores: one per scope, a projection of the table
# --------------------------------------------------------------------------

# One file per store. Small on purpose: a store holds documents up to 100KB
# and the agent reads this one whole.
STORE_PATH = "/memory.md"
_STORE_NAMES = {
    Scope.FIRM: "LRA firm memory",
    Scope.PERSONAL: "LRA lawyer memory",
    Scope.CLIENT: "LRA client memory",
    Scope.MATTER: "LRA matter memory",
}
_STORE_DESCRIPTIONS = {
    Scope.FIRM: "House conventions for this firm's documents, from the firm's own "
                "playbook. Personal preferences override these.",
    Scope.PERSONAL: "One lawyer's drafting style, and the kinds of point they have asked "
                    "not to be told about again. Never anything from a client's documents.",
    Scope.CLIENT: "What has been learned on one client's matters: positions observed, "
                  "how its counterparties behave. Mounted only on that client's matters.",
    Scope.MATTER: "What has happened on one matter: prior drafts, positions taken, "
                  "what was conceded. Read before reviewing a document on it.",
}


def _stores_enabled() -> bool:
    from secondeye import managed

    return managed.configured()


def store_for(scope: Scope, scope_key: str, create: bool = True) -> str:
    """The memory store id for a scope, creating the store on first use and
    recording it. Empty when the stores are off or the create failed: memory
    then simply does not reach the session, and the review carries on."""
    from secondeye.config import anthropic_client, settings

    if scope is Scope.FIRM:
        return settings().managed_firm_memory_store_id.strip()
    if not scope_key:
        return ""
    key = scope_key.lower()
    init()
    with connect() as c:
        row = c.execute("SELECT store_id FROM memory_stores WHERE scope = ? AND scope_key = ?",
                        (scope.value, key)).fetchone()
    if row:
        return row[0]
    if not create or not _stores_enabled():
        return ""
    try:
        store = anthropic_client().beta.memory_stores.create(
            name=f"{_STORE_NAMES[scope]}: {key}"[:120],
            description=_STORE_DESCRIPTIONS[scope],
        )
    except Exception:
        log.exception("could not create a %s memory store", scope.value)
        return ""
    with connect() as c:
        c.execute("INSERT OR REPLACE INTO memory_stores (scope, scope_key, store_id, created_at) "
                  "VALUES (?, ?, ?, ?)",
                  (scope.value, key, store.id, datetime.now(UTC).isoformat()))
    return store.id


def stores_for(user_address: str, matter_id: str | None = None,
               client_id: str | None = None) -> dict[str, str]:
    """The stores to mount on a review for this lawyer and matter, by scope:
    the firm's (playbook-derived only), the lawyer's (style only), and this
    client's and this matter's. Never another client's: a store is mounted
    only by the key this review resolved, and an unknown client mounts none.
    Only the ones that exist or could be created; never raises."""
    out: dict[str, str] = {}
    try:
        firm = store_for(Scope.FIRM, "")
        if firm:
            out["firm"] = firm
        personal = store_for(Scope.PERSONAL, user_address)
        if personal:
            out["personal"] = personal
        client_id = client_id or clients.for_matter(matter_id)
        if client_id:
            client = store_for(Scope.CLIENT, client_id)
            if client:
                out["client"] = client
        if matter_id:
            matter = store_for(Scope.MATTER, matter_id)
            if matter:
                out["matter"] = matter
        # The firm's own playbook, once one is approved (playbook.py). Mounted
        # beside the memory rather than folded into it: it replaces the
        # starter positions, and is versioned and approved on its own.
        from secondeye import playbook

        book = playbook.mounted_store()
        if book:
            out["playbook"] = book
    except Exception:
        log.exception("could not resolve memory stores")
    return out


def project(scope: Scope, scope_key: str) -> bool:
    """Rewrite a scope's memory-store file from the table. True if it reached
    the store. Called after every write; a failure is logged, never raised,
    because the table already holds the entry and the next write retries."""
    from secondeye.config import anthropic_client

    if not _stores_enabled():
        return False
    store_id = store_for(scope, scope_key)
    if not store_id:
        return False
    entries = _entries(scope, scope_key)
    content = as_prompt_block(entries) or (
        "# Nothing recorded yet\n\nNo conventions, preferences or positions have been "
        "recorded for this scope."
    )
    try:
        client = anthropic_client()
        memories = client.beta.memory_stores.memories
        existing = None
        for item in memories.list(store_id, path_prefix="/"):
            if getattr(item, "type", "") == "memory" and item.path == STORE_PATH:
                existing = item
                break
        if existing is None:
            memories.create(store_id, path=STORE_PATH, content=content)
        else:
            memories.update(existing.id, memory_store_id=store_id, content=content)
        return True
    except Exception:
        log.exception("could not project %s memory for %s", scope.value, scope_key)
        return False


def _entries(scope: Scope, scope_key: str) -> list[MemoryEntry]:
    init()
    with connect() as c:
        c.row_factory = sqlite3.Row
        # Case-insensitive on the key: addresses arrive in whatever case the
        # mail client used, and a preference stored as Jim@ must reach jim@.
        rows = c.execute(
            "SELECT * FROM memory WHERE scope = ? AND lower(scope_key) = lower(?) "
            "AND status = ? ORDER BY confidence DESC LIMIT 60",
            (scope.value, scope_key, Status.CONFIRMED.value),
        ).fetchall()
    return [_row(r) for r in rows]


def record_outcome(
    job_id: str,
    user_address: str,
    matter_id: str | None,
    category: str,
    severity: str,
    title: str,
    anchor: str,
    suggested_text: str | None,
    outcome: str,
) -> None:
    """Called when we learn what happened to a suggestion.

    Sources: the lawyer's reply ('stop doing that'), or a diff of the agent's
    redline against the version the lawyer actually sent.

    Nothing is recorded unless LEARN_FROM_OUTCOMES is on: the row quotes the
    suggestion, and with learning off nothing reads it.
    """
    if not learning():
        return
    init()
    with connect() as c:
        c.execute(
            "INSERT INTO suggestion_outcomes (job_id, user_address, matter_id, category, "
            "severity, title, anchor, suggested_text, outcome, observed_at, client_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, user_address, matter_id, category, severity, title, anchor,
             suggested_text, outcome, datetime.now(UTC).isoformat(),
             clients.for_matter(matter_id)),
        )


def _within(scope: Scope | None, key: str) -> tuple[str, tuple]:
    """The outcomes a count may look at: all of the lawyer's for a style
    category, or only this client's or this matter's for anything else, so
    a habit learned on one client's documents is counted on that client's."""
    if scope is Scope.CLIENT:
        return " AND client_id = ?", (key,)
    if scope is Scope.MATTER:
        return " AND matter_id = ?", (key,)
    return "", ()


def rejection_rate(user_address: str, category: str, scope: Scope | None = None,
                   key: str = "") -> tuple[float, int]:
    """How often this lawyer rejects suggestions in this category, across
    everything or, given a client or matter scope, within it.

    A high rate with enough samples should become a SUPPRESSION memory entry,
    which is how 'stop flagging passive voice' happens without anyone saying it.
    """
    init()
    where, extra = _within(scope, key)
    with connect() as c:
        row = c.execute(
            # Edits only: a dismissed flag was never an edit, and counting it
            # here made every "B is fine" dilute the undo rate.
            "SELECT SUM(outcome = 'rejected') AS rej, "
            "SUM(outcome IN ('accepted', 'rejected')) AS total "
            f"FROM suggestion_outcomes WHERE user_address = ? AND category = ?{where}",
            (user_address, category, *extra),
        ).fetchone()
    rej, total = (row[0] or 0), (row[1] or 0)
    return ((rej / total) if total else 0.0, total)


# --------------------------------------------------------------------------
# Learning from the conversation
# --------------------------------------------------------------------------

# Enough rejections of one kind of suggestion to stop making it unasked.
_PROMOTE_AFTER = 5
_PROMOTE_RATE = 0.7

_STOP_FLAGGING = re.compile(
    r"\b(?:stop|quit|don'?t|do not|no need to|never|please don'?t)\s+"
    r"(?:flag(?:ging)?|rais(?:e|ing)|mention(?:ing)?|tell(?:ing)? me about|"
    r"point(?:ing)? out|correct(?:ing)?|chang(?:e|ing)|suggest(?:ing)?|"
    r"comment(?:ing)? on)\b\s*([^.\n!?]{3,160})",
    re.IGNORECASE,
)


def _one_line(text: str, limit: int) -> str:
    """Safe to splice into the system prompt: one line, no markup, bounded."""
    return re.sub(r"[<>`\r\n\t]+", " ", text).strip()[:limit].strip()


# Where the subject of "stop flagging ..." ends and a second request begins:
# "stop flagging shall and fix clause 4". The subject pattern runs to the end
# of the sentence, so the instruction has to be cut off by its verb.
_SECOND_REQUEST = re.compile(
    r"[,;]?\s+\b(?:and|but|then|also|plus)\s+(?:please\s+|can you\s+|could you\s+)?"
    r"(?:fix|cap|change|add|remove|delete|undo|make|tighten|insert|move|replace|"
    r"correct|update|redraft|rewrite|check|look|put|take|strike|shorten|reword|"
    r"revert|swap|clean)\b.*$",
    re.IGNORECASE | re.DOTALL,
)


def _suppression_match(text: str):
    match = _STOP_FLAGGING.search(text or "")
    if not match:
        return None, None, None
    subject = _SECOND_REQUEST.sub("", match.group(1))
    tail = match.group(1)[len(subject):]
    return match, subject, tail


def suppression_request(text: str) -> str | None:
    """What the lawyer asked not to be told about again, in their own words."""
    match, subject, _ = _suppression_match(text)
    if not match:
        return None
    phrase = match.group(0)[: match.start(1) - match.start(0)] + subject
    return _one_line(phrase, 200)


def suppression_subject(text: str) -> str | None:
    """The thing itself: "the Oxford comma" out of "stop flagging the Oxford comma"."""
    match, subject, _ = _suppression_match(text)
    return _one_line(subject, 120).rstrip(" ,;") if match else None


def is_only_a_suppression(text: str) -> bool:
    """Whether the message says "stop flagging X" and nothing else.

    Decides whether the request is answered with a confirmation and nothing
    more, or handed on to the instruction agent as well. "Stop flagging shall
    and fix clause 4" is both, and the agent has to see it.
    """
    match, _, tail = _suppression_match(text)
    if not match:
        return False
    rest = text[:match.start()] + " " + tail + " " + text[match.end():]
    rest = re.sub(r"^\s*(?:also|and|please|hi|hello|thanks|thank you)[,\s]*", "", rest.strip(),
                  flags=re.IGNORECASE)
    return len(re.sub(r"[^\w\s]", "", rest).split()) <= 2


# "flag the Oxford comma again", "start flagging shall again", "you can raise
# passive voice again": the reversal, in the same words.
_FLAG_AGAIN = re.compile(
    r"\b(?:start\s+)?(?:flag(?:ging)?|rais(?:e|ing)|mention(?:ing)?|point(?:ing)?\s+out|"
    r"correct(?:ing)?|comment(?:ing)?\s+on)\s+([^.\n!?]{3,120}?)\s+again\b",
    re.IGNORECASE,
)


def unsuppression_request(text: str) -> str | None:
    match = _FLAG_AGAIN.search(text or "")
    return _one_line(match.group(1), 120).rstrip(" ,;") if match else None


def forget_suppression(user_address: str, subject: str, matter_id: str | None = None) -> int:
    """Lift every suppression of this lawyer's that mentions the subject, of
    those this matter may touch (`_suppressions_here`). The count is what the
    confirmation reports, so zero can be said plainly."""
    init()
    needle = f"%{subject.lower()}%"
    where, args = _suppressions_here(user_address, matter_id)
    with connect() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            f"SELECT * FROM memory WHERE {where} AND status = ? AND lower(statement) LIKE ?",
            (*args, Status.CONFIRMED.value, needle),
        ).fetchall()
    return _lift(user_address, [_row(r) for r in rows])


def _suppressions_here(user_address: str, matter_id: str | None) -> tuple[str, tuple]:
    """This lawyer's suppressions that a conversation on this matter may see
    or touch: their personal ones, and their walled ones under this matter
    or its client. Never another client's, not even to say it exists."""
    places = ["(scope = ? AND lower(scope_key) = lower(?))"]
    args: list[str] = [Scope.PERSONAL.value, user_address]
    walled = []
    client = clients.for_matter(matter_id)
    if client:
        walled.append((Scope.CLIENT.value, client))
    if matter_id:
        walled.append((Scope.MATTER.value, matter_id))
    for scope, key in walled:
        places.append("(scope = ? AND lower(scope_key) = lower(?) AND lower(owner) = lower(?))")
        args += [scope, key, user_address]
    return f"kind = ? AND ({' OR '.join(places)})", (Kind.SUPPRESSION.value, *args)


# Suppressions learned from what the lawyer did rather than said. Lifting one
# keeps it, as LIFTED, so the signals behind it do not learn it straight back.
_LEARNED = ("undo", "dismiss")


def _lift(user_address: str, entries: list[MemoryEntry]) -> int:
    """Stop reading these suppressions into reviews. The lawyer's own words
    are deleted; one learned from undos is kept as LIFTED (see Status)."""
    if not entries:
        return 0
    now = datetime.now(UTC).isoformat()
    with connect() as c:
        for e in entries:
            if (e.provenance or {}).get("source") in _LEARNED:
                c.execute("UPDATE memory SET status = ?, updated_at = ? WHERE id = ?",
                          (Status.LIFTED.value, now, e.id))
            else:
                c.execute("DELETE FROM memory WHERE id = ?", (e.id,))
    for scope, key in {(e.scope, e.scope_key) for e in entries}:
        project(scope, key)
    return len(entries)


def _suppression_statement(request: str) -> str:
    return f'Asked not to be told about this again: "{_one_line(request, 200)}"'


def suppression_home(request: str, user_address: str,
                     matter_id: str | None = None) -> tuple[Scope, str] | None:
    """Where "stop flagging X" is kept: with the lawyer when X is drafting
    style, under the client or matter it was said on when X names anything
    from a document, and nowhere when it names something and no matter is
    known (the caller says so rather than pretending to remember)."""
    statement = _suppression_statement(request)
    if personal_ok(Kind.SUPPRESSION, statement, {"source": "reply"}):
        return Scope.PERSONAL, user_address.lower()
    return walled_scope(matter_id)


def remember_suppression(user_address: str, request: str, source: str = "",
                         announced: bool = False, matter_id: str | None = None) -> bool:
    """Store a lawyer's own instruction. False if they have said it before,
    or if it names client content and there is no matter to keep it under
    (`suppression_home`).

    `announced` when the reply that answers this one confirms it, so the next
    review does not say it again."""
    statement = _suppression_statement(request)
    home = suppression_home(request, user_address, matter_id)
    if home is None:
        return False
    scope, key = home
    init()
    with connect() as c:
        known = c.execute(
            "SELECT 1 FROM memory WHERE scope = ? AND lower(scope_key) = lower(?) "
            "AND lower(owner) = lower(?) AND statement = ?",
            (scope.value, key, user_address, statement),
        ).fetchone()
    if known:
        return False
    remember(MemoryEntry(
        scope=scope, scope_key=key, kind=Kind.SUPPRESSION,
        statement=statement, confidence=1.0, owner=user_address.lower(),
        provenance={"source": "reply", "message": source, "matter": matter_id or "",
                    "subject": suppression_subject(request) or ""},
    ))
    if announced:
        with connect() as c:
            c.execute("UPDATE memory SET announced_at = ? WHERE scope = ? AND scope_key = ? "
                      "AND lower(owner) = lower(?) AND statement = ?",
                      (datetime.now(UTC).isoformat(), scope.value, key, user_address,
                       statement))
    return True


# --------------------------------------------------------------------------
# Saying what it learned, once
# --------------------------------------------------------------------------

# The words a lawyer uses for a kind of change, where the category is ours.
_KIND_WORDS = {
    "defined-term": "defined-term corrections",
    "cross-reference": "cross-reference corrections",
    "numbering": "numbering corrections",
    "date": "date-format corrections",
    "date-format": "date-format corrections",
    "amount": "amount corrections",
    "party-name": "party-name corrections",
    "placeholder": "placeholder fixes",
    "leftovers": "leftover removals",
    "formatting": "formatting corrections",
    "style": "style changes",
    "typo": "typo corrections",
    "spelling": "spelling corrections",
    "punctuation": "punctuation changes",
    "drafting": "drafting changes",
}

_LEARNED_LABEL = re.compile(r"“([^”]+)”")


def _announcement(entry: MemoryEntry) -> str:
    """The one line a review says about a suppression. What is said about one
    learned from undos is what it does: the change is no longer made, and is
    raised as a note instead, so "stopped flagging" would be untrue."""
    provenance = entry.provenance or {}
    if provenance.get("source") == "dismiss":
        label = provenance.get("label") or "those"
        return (f"I've stopped flagging {label} points for you, since you dismissed "
                f"{provenance.get('rejected') or entry.samples} of them. "
                'Reply "flag it again" to have them back.')
    if provenance.get("source") == "undo":
        label = provenance.get("label")
        if not label:
            m = _LEARNED_LABEL.search(entry.statement)
            label = m.group(1) if m else "that kind of change"
        what = _KIND_WORDS.get(label, f"{label} changes")
        undone = provenance.get("rejected") or round(
            float(provenance.get("rate") or 0) * (entry.samples or 0))
        return (f"I've stopped making {what} for you, since you undid {undone} of them; "
                'I point them out instead. Reply "flag it again" to have them back.')
    subject = provenance.get("subject")
    if not subject:
        m = re.search(r'"(.+)"', entry.statement)
        subject = (suppression_subject(m.group(1)) if m else None) or "that"
    return (f"I've stopped flagging {subject} for you, as you asked. "
            'Reply "flag it again" to have it back.')


def next_announcement(user_address: str, matter_id: str | None = None
                      ) -> tuple[int, str] | None:
    """(id, line) for the oldest suppression of this lawyer's they have not
    been told about, or None. One at a time: a review carries one line of
    this, and "flag it again" then means exactly one thing. One learned on a
    client's documents is announced only on that client's."""
    init()
    where, args = _suppressions_here(user_address, matter_id)
    with connect() as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            f"SELECT * FROM memory WHERE {where} "
            "AND status = ? AND announced_at IS NULL ORDER BY id LIMIT 1",
            (*args, Status.CONFIRMED.value),
        ).fetchone()
    if not row:
        return None
    entry = _row(row)
    return entry.id, _announcement(entry)


def mark_announced(entry_id: int) -> None:
    """The line went out: never say it again."""
    init()
    with connect() as c:
        c.execute("UPDATE memory SET announced_at = ? WHERE id = ?",
                  (datetime.now(UTC).isoformat(), entry_id))


def lift_last_announced(user_address: str, matter_id: str | None = None) -> str | None:
    """ "Flag it again": lift the suppression this lawyer was told about most
    recently, of those this matter may touch, and say what it was. None when
    there is nothing to lift."""
    init()
    where, args = _suppressions_here(user_address, matter_id)
    with connect() as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            f"SELECT * FROM memory WHERE {where} "
            "AND status = ? AND announced_at IS NOT NULL "
            "ORDER BY announced_at DESC, id DESC LIMIT 1",
            (*args, Status.CONFIRMED.value),
        ).fetchone()
    if not row:
        return None
    entry = _row(row)
    provenance = entry.provenance or {}
    if provenance.get("source") == "dismiss":
        what = f"{provenance.get('label') or 'those'} points"
    elif provenance.get("source") == "undo":
        label = provenance.get("label")
        if not label:
            m = _LEARNED_LABEL.search(entry.statement)
            label = m.group(1) if m else ""
        what = _KIND_WORDS.get(label, f"{label} changes" if label else "those changes")
    else:
        what = provenance.get("subject") or "that"
    _lift(user_address, [entry])
    return what


def record_rejections(job_id: str, user_address: str, matter_id: str | None,
                      changes: list) -> None:
    """An undo is the clearest signal there is: they saw the change and refused it.

    Each one is recorded, and a kind of change refused often enough becomes a
    suppression without anyone having to say "stop doing that". Only with
    LEARN_FROM_OUTCOMES on: by default nothing is learned unless asked.
    """
    if not learning():
        return
    user = user_address.lower()
    for change in changes:
        record_outcome(job_id, user, matter_id, change.category, "", change.title,
                       "", None, "rejected")
    for category in {c.category for c in changes if c.category}:
        label = _label(category)
        home = _learned_home(label, user, matter_id)
        if not home:
            continue
        scope, key = home
        rate, total = rejection_rate(user, category, None if scope is Scope.PERSONAL else scope,
                                     key)
        if total < _PROMOTE_AFTER or rate < _PROMOTE_RATE:
            continue
        statement = (f"Usually undoes changes of the kind “{label}”. Raise them as "
                     "a note rather than editing.")
        if _known(scope, key, user, statement):
            continue
        remember(MemoryEntry(
            scope=scope, scope_key=key, kind=Kind.SUPPRESSION, owner=user,
            statement=statement, confidence=min(0.95, rate), samples=total,
            provenance={"source": "undo", "rate": rate, "label": label,
                        "rejected": round(rate * total), "matter": matter_id or ""},
        ))


def _label(category: str) -> str:
    return re.sub(r"[^a-z0-9 -]+", " ", (category or "").lower()).strip()[:40]


def _learned_home(label: str, user: str, matter_id: str | None) -> tuple[Scope, str] | None:
    """Where a habit learned from undos or dismissals is kept. A style
    category is the lawyer's own, counted across everything they do; any
    other was named after what a document said, so it is counted and kept
    within the client (or matter) it was learned on, and not at all when
    neither is known."""
    if not label:
        return None
    if label in STYLE_LABELS:
        return Scope.PERSONAL, user
    return walled_scope(matter_id)


def _known(scope: Scope, key: str, user: str, statement: str) -> bool:
    with connect() as c:
        return c.execute(
            "SELECT 1 FROM memory WHERE scope = ? AND lower(scope_key) = lower(?) "
            "AND lower(COALESCE(owner, scope_key)) = lower(?) AND statement = ?",
            (scope.value, key, user, statement)).fetchone() is not None


def record_dismissals(job_id: str, user_address: str, matter_id: str | None,
                      findings: list) -> None:
    """ "B is fine": the lawyer saw a point raised and said it was not one.

    Counted apart from undos, as outcome "dismissed": an undo refuses an edit,
    a dismissal refuses being told, and they learn different things. A flag
    shown and not dismissed leaves no record, so there is no rate to compute;
    a kind of point dismissed often enough is the signal on its own, and it
    becomes a suppression the next review announces once, like any other.
    Only with LEARN_FROM_OUTCOMES on, as `record_rejections`.
    """
    if not learning():
        return
    user = user_address.lower()
    for f in findings:
        record_outcome(job_id, user, matter_id, f.category, f.severity, f.title,
                       f.anchor, None, "dismissed")
    for category in {f.category for f in findings if f.category}:
        label = _label(category)
        home = _learned_home(label, user, matter_id)
        if not home:
            continue
        scope, key = home
        where, extra = _within(None if scope is Scope.PERSONAL else scope, key)
        with connect() as c:
            row = c.execute(
                "SELECT COUNT(*) FROM suggestion_outcomes WHERE user_address = ? "
                f"AND category = ? AND outcome = 'dismissed'{where}",
                (user, category, *extra)).fetchone()
        count = row[0] if row else 0
        if count < _PROMOTE_AFTER:
            continue
        statement = (f"Usually dismisses points of the kind \u201c{label}\u201d. Do not "
                     "raise them unless they would stop the document being sent.")
        if _known(scope, key, user, statement):
            continue
        remember(MemoryEntry(
            scope=scope, scope_key=key, kind=Kind.SUPPRESSION, owner=user,
            statement=statement, confidence=0.9, samples=count,
            provenance={"source": "dismiss", "label": label, "rejected": count,
                        "matter": matter_id or ""},
        ))
