"""The audit trail: one content-free row per job, for the firm's risk committee.

A GC supervising a generative-AI tool (ABA Formal Opinion 512) needs to be
able to answer, for any month: who sent what to it, what it did, which model
and which session did it, where the document went and for how long, and who
got the answer. Job rows cannot answer that: they are purged after
RETENTION_HOURS so that nothing privileged lingers. This table keeps what the
committee needs and nothing a client would mind being kept: addresses,
document *names*, what ran, ids, times, outcome. Never a document's
contents, never an email body, never a finding.

It is written as a side effect of paths that already exist, so no feature has
to remember it: `store.record` (every status change), `handle` and the
Workflow's steps (received, and every send through `Watch`), and
`managed` (every session created). A failure here is logged and never fails
the job it describes.

Read with `lra audit --since 2026-09-01 [--lawyer x] [--matter y] --format
csv|json`, or by a playbook admin emailing "audit report for September".
"""

from __future__ import annotations

import contextvars
import csv
import io
import json
import logging
import re
import sqlite3
from calendar import month_name, monthrange
from datetime import UTC, date, datetime, timedelta

from lra.store import connect

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    job_id TEXT PRIMARY KEY,
    message_id TEXT,
    received_at TEXT,
    sender TEXT,
    matter_id TEXT,
    documents TEXT,           -- json: the names of the files received, never contents
    ran TEXT,                 -- json: what ran, in order, once each
    model TEXT,
    session_ids TEXT,         -- json
    inference_geo TEXT,
    stored_in TEXT,
    retention_until TEXT,
    reply_to TEXT,            -- json: everyone our reply was addressed to
    returned TEXT,            -- json: the names of the files we sent back
    reply_sent_at TEXT,
    outcome TEXT,
    triage TEXT,              -- json: the triage plan, the rules' plan, where they differ
    updated_at TEXT,
    -- What a purge by client or matter finds a job by (purge.py). The
    -- client is known on the message (its matter number or its parties),
    -- the conversation once the job reaches one, and the files are every
    -- upload a session of this job was given, deleted at once but kept here
    -- so a purge can make sure.
    client_id TEXT,
    thread_id TEXT,
    file_ids TEXT,            -- json
    -- The purge that blanked this row's names and ids, when one has.
    purged TEXT,
    -- When the retention sweep deleted this job's sessions and uploads at
    -- Anthropic (retention.py). The ids stay: they say what ran.
    expired TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_received ON audit_log(received_at);
"""

COLUMNS = ("job_id", "received_at", "sender", "matter_id", "documents", "ran", "model",
           "session_ids", "inference_geo", "stored_in", "retention_until", "reply_to",
           "returned", "reply_sent_at", "outcome", "purged")
_LISTS = ("documents", "ran", "session_ids", "reply_to", "returned", "file_ids")

# The job the current code is working for, so a session created deep inside a
# review is recorded against it without every caller passing the id down.
_current: contextvars.ContextVar[str | None] = contextvars.ContextVar("audit_job", default=None)

# Status and payload kinds, as the committee reads them.
_STATUS_RAN = {
    # "Model review" is recorded when a session is created (`session`), so a
    # no-AI client's checks-only review never claims one.
    "reviewing": ["checks"],
    "redlining": ["tracked changes"],
}
_KIND_RAN = {
    "compare": "compare", "comparison": "compare", "compared": "compare",
    "clean": "clean", "clean_copy": "clean", "cleaned": "clean",
    "sigpack": "sig pack", "signature_pack": "sig pack", "sig_pack": "sig pack",
    "signatures": "sig pack", "closing": "closing", "closing_set": "closing",
    "comments": "comments", "comment": "comments", "renumber": "renumber",
    "renumbered": "renumber", "repair": "repair", "repaired": "repair",
    "undo": "undo", "answer": "answer", "instruction": "instruction",
    "combined": "instruction", "suppression": "memory", "unsuppression": "memory",
    "remember": "memory", "forget": "memory", "audit": "audit report",
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def init() -> None:
    with connect() as c:
        c.executescript(SCHEMA)
        # Databases created before triage was recorded (triage.py).
        # And before a purge could find a job by client or conversation.
        columns = {r[1] for r in c.execute("PRAGMA table_info(audit_log)")}
        for column in ("triage", "client_id", "thread_id", "file_ids", "purged", "expired"):
            if column not in columns:
                c.execute(f"ALTER TABLE audit_log ADD COLUMN {column} TEXT")


def begin(job_id: str) -> contextvars.Token:
    """Work from here on is for this job (see `_current`)."""
    return _current.set(job_id)


def end(token: contextvars.Token) -> None:
    _current.reset(token)


def current() -> str | None:
    return _current.get()


def _safe(fn):
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            log.exception("audit: %s failed", fn.__name__)
            return None
    wrapped.__name__ = fn.__name__
    wrapped.__doc__ = fn.__doc__
    return wrapped


def _where_stored() -> tuple[str, str]:
    """Where this deployment keeps a document, and until when one received
    now is kept. Described from settings, so it is what was true then."""
    from lra.config import settings
    from lra.store import is_d1

    cfg = settings()
    place = ("Cloudflare D1 and R2" if is_d1() else
             f"SQLite ({cfg.database_url.replace('sqlite:///', '')})")
    sealed = ("encrypted with the firm's key" if cfg.data_key.strip()
              else "not encrypted at rest by us")
    days = cfg.thread_retention_days
    until = ((datetime.now(UTC) + timedelta(days=days)).isoformat() if days > 0
             else "until purged")
    where = (f"{place}, {sealed}; a copy is mounted in the Anthropic session, whose "
             "files are deleted when it ends"
             + (f" and the session itself after {days} days" if days > 0 else ""))
    return where, until


def _row(job_id: str) -> dict | None:
    with connect() as c:
        c.row_factory = sqlite3.Row
        r = c.execute("SELECT * FROM audit_log WHERE job_id = ?", (job_id,)).fetchone()
    if not r:
        return None
    out = dict(zip(r.keys(), tuple(r), strict=True))
    for k in _LISTS:
        out[k] = json.loads(out[k] or "[]")
    return out


def _ensure(job_id: str, message_id: str = "", sender: str = "") -> dict:
    init()
    existing = _row(job_id)
    if existing:
        return existing
    from lra.config import settings

    stored, until = _where_stored()
    with connect() as c:
        c.execute(
            "INSERT OR IGNORE INTO audit_log (job_id, message_id, received_at, sender, "
            "documents, ran, session_ids, reply_to, returned, inference_geo, stored_in, "
            "retention_until, updated_at) VALUES (?, ?, ?, ?, '[]', '[]', '[]', '[]', '[]', "
            "?, ?, ?, ?)",
            (job_id, message_id, _now(), sender.lower(),
             settings().inference_geo or "API default (not pinned)", stored, until, _now()))
    return _row(job_id) or {}


def _update(job_id: str, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = _now()
    for k in _LISTS:
        if k in fields:
            fields[k] = json.dumps(fields[k])
    sets = ", ".join(f"{k} = ?" for k in fields)
    with connect() as c:
        c.execute(f"UPDATE audit_log SET {sets} WHERE job_id = ?", (*fields.values(), job_id))


def _add(existing: list, items) -> list:
    return existing + [i for i in items if i and i not in existing]


@_safe
def received(job_id: str, email) -> None:
    """A message arrived and is this job's. Names of its files, not their bytes."""
    row = _ensure(job_id, email.message_id, email.from_address)
    fields = {"documents": _add(row["documents"], [a.filename for a in email.attachments])}
    if email.received_at:
        fields["received_at"] = email.received_at.astimezone(UTC).isoformat()
    from lra.pipeline import identity

    matter = identity.resolve_matter(email)
    if matter:
        fields["matter_id"] = matter
    # Whose it is, as a review decides it (the matter number, else the
    # parties' domains), so a purge of the client finds the job.
    client = identity.resolve_client(email, matter)
    if client:
        fields["client_id"] = client
    _update(job_id, **fields)


@_safe
def status(job_id: str, message_id: str, state: str, sender: str, payload: dict) -> None:
    """Every job status change (store.record). The outcome is the last one."""
    row = _ensure(job_id, message_id, sender)
    ran = _add(row["ran"], _STATUS_RAN.get(state, []))
    kind = str((payload or {}).get("kind") or "")
    if kind:
        ran = _add(ran, [_KIND_RAN.get(kind, kind.replace("_", " "))])
    outcome = state if not kind else f"{state} ({kind.replace('_', ' ')})"
    if state in ("rejected", "ignored") and (payload or {}).get("reason"):
        outcome = f"{state}: {str(payload['reason'])[:120]}"
    _update(job_id, ran=ran, outcome=outcome)


@_safe
def ran(job_id: str | None, what: str) -> None:
    """Something ran that no status says: a skill, a tool."""
    job_id = job_id or current()
    if not job_id:
        return
    row = _ensure(job_id)
    _update(job_id, ran=_add(row["ran"], [what]))


@_safe
def session(session_id: str, job_id: str | None = None, model: str = "") -> None:
    """A Managed Agents session was created for this job."""
    job_id = job_id or current()
    if not job_id or not session_id:
        return
    from lra.config import settings

    row = _ensure(job_id)
    _update(job_id, session_ids=_add(row["session_ids"], [session_id]),
            model=model or settings().effective_model,
            ran=_add(row["ran"], ["model review"]))


@_safe
def uploaded(file_ids, job_id: str | None = None) -> None:
    """Files were uploaded to Anthropic for a session of this job. Each is
    deleted once the session has its copy; recorded so a purge can make sure
    (a delete that failed is otherwise only a line in a log)."""
    job_id = job_id or current()
    if not job_id or not file_ids:
        return
    row = _ensure(job_id)
    _update(job_id, file_ids=_add(row.get("file_ids") or [], list(file_ids)))


@_safe
def thread(thread_id: str, job_id: str | None = None) -> None:
    """This job is on this conversation (or closing), so a purge of the
    conversation's client or matter finds the job and its sessions, though
    the reply that started the job named no matter."""
    job_id = job_id or current()
    if not job_id or not thread_id:
        return
    row = _ensure(job_id)
    if row.get("thread_id") != thread_id:
        _update(job_id, thread_id=thread_id)


@_safe
def triaged(job_id: str | None, entry: dict) -> None:
    """What triage decided for this job, beside what the rules decided
    (triage.py). Content-free: intents, file names, ids and addresses."""
    job_id = job_id or current()
    if not job_id:
        return
    _ensure(job_id)
    _update(job_id, triage=json.dumps(entry, ensure_ascii=False))


@_safe
def replying(job_id: str, out) -> None:
    """A reply was built for this job: who it is addressed to and what it
    carries. Sent when `sent` says so (the Workflow's Worker sends it)."""
    row = _ensure(job_id)
    to = [*out.to, *getattr(out, "cc", []), *getattr(out, "bcc", [])]
    _update(job_id, reply_to=_add(row["reply_to"], [a.lower() for a in to]),
            returned=_add(row["returned"], [a.filename for a in out.attachments]))


@_safe
def sent(job_id: str, at: datetime | None = None) -> None:
    _ensure(job_id)
    _update(job_id, reply_sent_at=(at or datetime.now(UTC)).isoformat())


class Watch:
    """A provider whose every send is written to this job's audit row."""

    def __init__(self, provider, job_id: str) -> None:
        self._provider = provider
        self._job_id = job_id

    def __getattr__(self, name):
        return getattr(self._provider, name)

    def send(self, out, *args, **kwargs):
        replying(self._job_id, out)
        result = self._provider.send(out, *args, **kwargs)
        sent(self._job_id)
        return result


# --------------------------------------------------------------------------
# Reading it back
# --------------------------------------------------------------------------


def rows(since: str | date, until: str | date | None = None, lawyer: str | None = None,
         matter: str | None = None) -> list[dict]:
    """Jobs received on or after `since` (and before `until`), oldest first."""
    init()
    clauses, args = ["received_at >= ?"], [str(since)]
    if until:
        clauses.append("received_at < ?")
        args.append(str(until))
    if lawyer:
        clauses.append("lower(sender) = lower(?)")
        args.append(lawyer)
    if matter:
        clauses.append("lower(matter_id) = lower(?)")
        args.append(matter)
    with connect() as c:
        c.row_factory = sqlite3.Row
        found = c.execute(f"SELECT job_id FROM audit_log WHERE {' AND '.join(clauses)} "
                          "ORDER BY received_at, job_id", tuple(args)).fetchall()
    return [r for r in (_row(f["job_id"]) for f in found) if r]


def as_json(found: list[dict]) -> str:
    return json.dumps([{k: r.get(k) for k in COLUMNS} for r in found], indent=2)


def as_csv(found: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(COLUMNS)
    for r in found:
        w.writerow(["; ".join(r[k]) if k in _LISTS else (r.get(k) or "") for k in COLUMNS])
    return buf.getvalue()


# --------------------------------------------------------------------------
# By email: "audit report for September"
# --------------------------------------------------------------------------

_MONTHS = {m.lower(): i for i, m in enumerate(month_name) if m}
_ASK = re.compile(r"\baudit\s+(?:report|log|export|trail)\b", re.IGNORECASE)
_MONTH = re.compile(r"\b(" + "|".join(_MONTHS) + r")\b(?:\s+(\d{4}))?", re.IGNORECASE)
_SINCE = re.compile(r"\bsince\s+(\d{4}-\d{2}-\d{2})\b", re.IGNORECASE)


def request(email) -> tuple[date, date] | None:
    """The period an "audit report for September" email asks for, or None
    when the message is not asking for one. A month with no year is the most
    recent one that has started; "since 2026-09-01" runs to today."""
    from lra.pipeline.router import strip_reply

    text = f"{email.subject or ''}\n{strip_reply(email.body or '')}"
    if not _ASK.search(text):
        return None
    today = datetime.now(UTC).date()
    m = _SINCE.search(text)
    if m:
        return date.fromisoformat(m.group(1)), today + timedelta(days=1)
    m = _MONTH.search(text)
    if m:
        month = _MONTHS[m.group(1).lower()]
        year = int(m.group(2)) if m.group(2) else (
            today.year if month <= today.month else today.year - 1)
    elif re.search(r"\blast month\b", text, re.IGNORECASE):
        year, month = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    else:
        year, month = today.year, today.month
    start = date(year, month, 1)
    return start, start + timedelta(days=monthrange(year, month)[1])


def may_request(address: str) -> bool:
    """Only a playbook admin named in PLAYBOOK_ADMINS, or, with none named,
    the allowlist contact. Deliberately narrower than `playbook.is_admin`,
    which with nothing configured lets any firm address in: the report lists
    every lawyer's senders and file names."""
    from lra.config import settings
    from lra.models import bare_address

    cfg = settings()
    addr = address.lower()
    listed = [s.strip().lower() for s in cfg.playbook_admins.split(",") if s.strip()]
    if listed:
        return addr in listed or addr.split("@")[-1] in listed
    contact = bare_address(cfg.allowlist_contact).lower()
    return "@" in contact and addr == contact


def handle(job_id: str, email, provider, period: tuple[date, date]) -> str:
    """Answer an audit request, to the sender alone. Returns the status."""
    from lra.models import Attachment, OutboundEmail
    from lra.pipeline import reply

    def answer(text: str, attachments=None) -> None:
        provider.send(OutboundEmail(
            to=[email.from_address], subject=reply._subject(email.subject or "Audit report"),
            text_body=text + "\n", html_body=reply.text_as_html(text),
            in_reply_to=email.message_id, thread_id=email.thread_id,
            attachments=attachments or []))

    if not may_request(email.from_address):
        answer("Only the firm's playbook admins can ask for the audit report, so none "
               "is attached.")
        return "rejected"
    start, end_ = period
    found = rows(start, end_)
    body = as_csv(found).encode()
    name = f"audit {start.isoformat()} to {(end_ - timedelta(days=1)).isoformat()}.csv"
    answer(f"The audit report for {start:%-d %B %Y} to "
           f"{end_ - timedelta(days=1):%-d %B %Y}: {len(found)} job"
           f"{'' if len(found) == 1 else 's'}. One row per email received: who sent it, "
           "the names of its files (never their contents), what ran, the model and "
           "session, where it was kept and until when, who the reply went to and when, "
           "and how it ended.",
           [Attachment(filename=name, content_type="text/csv", size_bytes=len(body),
                       content=body)])
    return "replied"


__all__ = ["COLUMNS", "Watch", "as_csv", "as_json", "begin", "current", "end", "handle",
           "init", "may_request", "ran", "received", "replying", "request", "rows", "sent",
           "session", "status", "thread", "uploaded"]
