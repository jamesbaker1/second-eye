"""Firm policy: rules a firm sets that no model output can change.

DECISIONS 30 lets the model decide almost everything, recipients included. A
firm's general counsel does not sign off on "the model decides who receives
privileged work product"; they sign off on a rule they can read. The rules
here are applied at the one place every outbound message passes (`Policed`,
wrapping the mail provider for the length of one inbound message, and
`apply` for the replies the Workflow stores for the Worker to send), after
every composer and every model decision, so nothing upstream can route around
them.

REPLY_POLICY (DECISIONS 31)
    sender_only (the default): every message goes to the lawyer who sent the
    inbound one, and to nobody else, whatever the composer put on To or CC.
    model: the recipients stand as composed (DECISIONS 30).

NO_AI_MATTERS
    Clients whose guidelines exclude AI. Their work gets no model call at
    all; see the section below.
"""

from __future__ import annotations

import contextlib
import contextvars
import html as html_lib
import logging
import re
import unicodedata
from datetime import UTC, datetime

from secondeye.config import settings
from secondeye.models import InboundEmail, OutboundEmail

log = logging.getLogger(__name__)


def apply(out: OutboundEmail, sender: str) -> OutboundEmail:
    """The message as policy allows it to leave. `sender` is the inbound
    message's From address, which is the only recipient sender_only allows."""
    if settings().reply_policy == "sender_only":
        if not sender:
            # Nobody to send it to is not "send it to whoever the composer
            # chose". Refuse, loudly: this is a bug upstream, not a mail.
            raise PolicyViolation("REPLY_POLICY=sender_only and the inbound "
                                  "message has no sender to reply to")
        if [a.lower() for a in out.to] != [sender.lower()] or out.cc:
            log.warning("REPLY_POLICY=sender_only: readdressing a reply from %s (cc %s) "
                        "to the sender alone", out.to, out.cc)
        out = out.model_copy(update={"to": [sender], "cc": []})
    return privileged(out)


def privileged(out: OutboundEmail) -> OutboundEmail:
    """PRIVILEGE_NOTICE on the message: the last line of both bodies, small
    and grey in HTML, and the X-Privileged header.

    At the end, not the top. The first line is the verdict ("Don't send yet:
    2 things to fix"), and it is what a phone's inbox preview shows; a
    legend there would take the preview on every message and say nothing a
    lawyer did not know. Firms' own confidentiality legends sit at the foot of
    the email for the same reason. The header is for machines: journaling,
    DLP and retention rules match it without reading the body."""
    notice = settings().privilege_notice.strip()
    if not notice:
        return out
    text = out.text_body.rstrip("\n")
    html_body = out.html_body
    if not text.endswith(notice):
        text = f"{text}\n\n{notice}"
        if html_body:
            html_body += ('<p style="margin:1.5em 0 0;font-size:11px;color:#888">'
                          f"{html_lib.escape(notice)}</p>")
    headers = {**out.headers, "X-Privileged": _ascii(notice)}
    return out.model_copy(update={"text_body": text + "\n", "html_body": html_body,
                                  "headers": headers})


def _ascii(value: str) -> str:
    """A header value every provider carries as it stands: no encoded words
    for a dash, and no line breaks."""
    value = value.replace("—", "-").replace("–", "-").replace("\r", " ").replace("\n", " ")
    return unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().strip()


class PolicyViolation(RuntimeError):
    """A message policy will not let leave."""


class Policed:
    """The mail provider as one inbound message's handling sees it: every
    send goes through `apply` first, with that message's sender."""

    def __init__(self, provider, inbound: InboundEmail) -> None:
        self._provider = provider
        self._sender = inbound.from_address

    def __getattr__(self, name):
        return getattr(self._provider, name)

    def send(self, out: OutboundEmail, **kwargs):
        out = apply(out, self._sender)
        if paused():
            # Switched off mid-message: whatever this was, it does not leave.
            log.warning("SERVICE_PAUSED: not sending %r", out.subject)
            return ""
        return self._provider.send(out, **kwargs)


def paused() -> bool:
    """SERVICE_PAUSED, the kill switch: nothing is reviewed and nothing sent.
    The edge holds incoming mail rather than bouncing it; here, a message
    that reaches the application anyway is refused before it is claimed, so
    it is redelivered once the switch is off (main.py, handler.handle)."""
    return settings().service_paused


# --------------------------------------------------------------------------
# No-AI clients and matters
# --------------------------------------------------------------------------
#
# Some clients' outside-counsel guidelines forbid putting their documents
# through a model. NO_AI_MATTERS lists them (matter ids, client names, or the
# client's mail domains), and a playbook admin can add or lift one by email:
# "no AI for Acme", "AI ok for Acme". A message that matches gets no model
# call at all: the deterministic checks, and compare, clean copy, renumber
# and signature pages, which are code. The reply says so in one line.
#
# Enforced in two layers. The handler takes the mechanical path when a match
# is found; and while a match is in force, `managed.configured()` is False
# and `config.anthropic_client()` raises AIForbidden, so a model call nobody
# thought to route around fails closed instead of reaching the API.

NO_AI_SCHEMA = """
CREATE TABLE IF NOT EXISTS no_ai_clients (
    entry TEXT PRIMARY KEY,       -- lower-cased: a matter id, a client name, or a domain
    label TEXT NOT NULL,          -- as the admin wrote it, for the reply
    added_by TEXT,
    added_at TEXT
);
"""


class AIForbidden(RuntimeError):
    """A model call while a no-AI client's work is being handled."""


_forbidden: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "secondeye_no_ai", default=None)


@contextlib.contextmanager
def ai_scope():
    """One inbound message's handling. A no-AI match made inside it ends
    with it, so the next message on the same worker thread starts clean."""
    token = _forbidden.set(None)
    try:
        yield
    finally:
        _forbidden.reset(token)


def forbid_ai(label: str) -> None:
    """From here to the end of the scope, no model call is made."""
    if _forbidden.get() is None:
        log.info("no model calls for this message: %s is on the no-AI list", label)
        _forbidden.set(label)


def ai_forbidden() -> str | None:
    """The client whose guidelines exclude AI for the message in hand, or None."""
    return _forbidden.get()


def require_ai_allowed() -> None:
    label = _forbidden.get()
    if label is not None:
        raise AIForbidden(f"{label}'s guidelines exclude AI; no model call is made")


def no_ai_line(label: str) -> str:
    return f"Mechanical checks only: {label}'s guidelines exclude AI."


def _init() -> None:
    from secondeye.store import connect

    with connect() as c:
        c.executescript(NO_AI_SCHEMA)


def _configured() -> dict[str, str]:
    return {e.strip().lower(): e.strip()
            for e in settings().no_ai_matters.split(",") if e.strip()}


def _entries() -> dict[str, str]:
    """Every entry, lower-cased, to the label the reply names it by."""
    out = _configured()
    try:
        from secondeye.store import connect

        _init()
        with connect() as c:
            for entry, label in c.execute("SELECT entry, label FROM no_ai_clients").fetchall():
                out.setdefault(entry, label)
    except Exception:
        # Failing closed would stop every review on a database blip; the list
        # in configuration still holds. Logged in full: it is a policy lapse.
        log.exception("could not read the no-AI list from the database")
    return out


def _is_domain(entry: str) -> bool:
    return "." in entry and " " not in entry and "@" not in entry


def excluded(email: InboundEmail | None = None, matter: str | None = None,
             parties: list[str] | tuple[str, ...] = ()) -> str | None:
    """The label of the no-AI entry this work matches, or None.

    A match is any of: the matter (identity.resolve_matter, or the one a
    conversation was recorded under) equal to an entry; a To or CC address on
    an entry's domain, or a subdomain of it; an entry's name as a whole word
    in one of the document's parties (the parties clause, as the checks read
    it) or, for a domain entry, its first label there ("acme" for acme.com).
    """
    entries = _entries()
    if not entries:
        return None
    if matter and matter.strip().lower() in entries:
        return entries[matter.strip().lower()]
    if email is not None:
        for address in email.to + email.cc:
            domain = address.lower().rpartition("@")[2]
            for entry, label in entries.items():
                if _is_domain(entry) and (domain == entry or domain.endswith("." + entry)):
                    return label
    for party in parties:
        for entry, label in entries.items():
            name = entry.split(".")[0] if _is_domain(entry) else entry
            if len(name) >= 2 and re.search(rf"(?<!\w){re.escape(name)}(?!\w)", party,
                                             re.IGNORECASE):
                return label
    return None


_ADD = re.compile(r"^\s*no\s+ai\s+(?:for|on)\s+(.+?)\s*[.!]?\s*$", re.IGNORECASE)
_LIFT = re.compile(
    r"^\s*ai\s+(?:is\s+)?(?:ok|okay|allowed|fine)\s+(?:for|on)\s+(.+?)\s*[.!]?\s*$",
    re.IGNORECASE)


def command(email: InboundEmail) -> tuple[bool, str] | None:
    """"no AI for Acme" -> (True, "Acme"); "AI ok for Acme" -> (False,
    "Acme"); None for anything else. The whole message must be the command,
    as with every other command, so a forwarded email that happens to say
    "no AI for..." in passing changes nothing."""
    from secondeye.pipeline.router import strip_reply

    if email.attachments:
        return None
    lines = [line for line in strip_reply(email.body).splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    for pattern, forbid in ((_ADD, True), (_LIFT, False)):
        m = pattern.match(lines[0])
        if m and len(m.group(1)) <= 100:
            return forbid, m.group(1).strip().strip("\"'“”")
    return None


def set_no_ai(label: str, forbid: bool, by: str) -> bool:
    """Add or lift an entry kept in the database. False when there was
    nothing to change."""
    from secondeye.store import connect

    _init()
    entry = label.strip().lower()
    with connect() as c:
        if forbid:
            present = c.execute("SELECT 1 FROM no_ai_clients WHERE entry = ?",
                                (entry,)).fetchone()
            c.execute("INSERT OR REPLACE INTO no_ai_clients (entry, label, added_by, added_at) "
                      "VALUES (?, ?, ?, ?)",
                      (entry, label.strip(), by, datetime.now(UTC).isoformat()))
            return present is None
        return bool(c.execute("DELETE FROM no_ai_clients WHERE entry = ?", (entry,)).rowcount)


def answer_command(email: InboundEmail, forbid: bool, label: str) -> str:
    """Carry out "no AI for X" / "AI ok for X" from a playbook admin, and the
    sentence to say back. An entry in NO_AI_MATTERS cannot be lifted by email:
    configuration is the firm's signed-off list, and a reply cannot overrule
    it."""
    if not forbid and label.strip().lower() in _configured():
        return (f"{label} is on the firm's configured no-AI list (NO_AI_MATTERS), so it "
                "stays excluded. Whoever runs the deployment can change that.")
    changed = set_no_ai(label, forbid, email.from_address)
    if forbid:
        return (f"Done. Nothing for {label} goes to a model from now on: mechanical checks, "
                "comparisons, clean copies, renumbering and signature pages only."
                if changed else f"{label} was already excluded from AI. Nothing changed.")
    return (f"Done. {label} is no longer excluded from AI." if changed
            else f"{label} was not on the list I keep, so nothing changed.")
