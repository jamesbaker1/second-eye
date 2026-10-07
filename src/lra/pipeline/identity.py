"""Who sent this, on what matter, and how did it get here.

With one address for the whole firm, none of this is typed by the user. It is
all inferred, and when inference fails the agent says what it assumed rather
than asking.
"""

from __future__ import annotations

import logging
import re
from enum import Enum

from lra.config import settings
from lra.models import InboundEmail

log = logging.getLogger(__name__)


class EntryMode(str, Enum):
    FORWARD = "forward"    # sent to the agent directly, reply comes back to sender
    CC_DRAFT = "cc_draft"  # agent CC'd on a draft that has not gone to the client yet
    BCC_SENT = "bcc_sent"  # agent BCC'd on a message already delivered to the client
    REPLY = "reply"        # a follow-up in an existing review thread


def is_agent(address: str) -> bool:
    """Whether an address is ours. A plus tag does not make it someone else's:
    `review+M123@` is how resolve_matter() lets a lawyer name the matter, and
    comparing whole strings put such a message on no visible header at all,
    which reads as a BCC."""
    cfg = settings()
    ours = {cfg.mail_agent_address.lower()}
    ours.update(a.strip().lower() for a in cfg.mail_agent_aliases.split(",") if a.strip())
    local, _, domain = address.lower().partition("@")
    return f"{local.split('+', 1)[0]}@{domain}" in ours


# A one-tap reply link's plus tag: "t" and sixteen hex digits, so it cannot be
# read as a matter tag (`review+M123@`) and a matter tag is never read as one.
_TAP_TAG = re.compile(r"^t([0-9a-f]{16})$")


def tap_address(token: str) -> str:
    """The agent's address carrying a conversation's one-tap token.

    A mailto: link cannot set In-Reply-To, so a reply started from one arrives
    looking like a new message. The address is the only thing it can carry.
    """
    local, _, domain = settings().mail_agent_address.partition("@")
    return f"{local.split('+', 1)[0]}+t{token}@{domain}"


def tap_token(email: InboundEmail) -> str | None:
    """The one-tap token this message was addressed to, if any. It names a
    conversation, and resolves only for that conversation's owner
    (thread.resolve), so knowing it lets nobody else act on the thread."""
    for addr in email.to + email.cc:
        if not is_agent(addr):
            continue
        local = addr.lower().partition("@")[0]
        if "+" in local:
            m = _TAP_TAG.match(local.split("+", 1)[1])
            if m:
                return m.group(1)
    return None


def bcc_only(email: InboundEmail) -> bool:
    """We received this without being on any visible header.

    Someone else is being written to and we are looking over their shoulder,
    which is the whole point of the BCC habit: the lawyer's mail rule copies us
    on everything, and most of everything is not a document. A message with no
    visible recipients at all does not count; we cannot tell how it reached us,
    so the caller treats it as addressed.
    """
    visible = email.to + email.cc
    return bool(visible) and not any(is_agent(a) for a in visible)


def entry_mode(email: InboundEmail) -> EntryMode:
    """Which of the three ways in was used.

    The distinction that matters is BCC_SENT: the document has already reached
    the client, so the reply is an incident report, not a redline.
    """
    outside = external_recipients(email)

    # Order matters. Replying on the client's own thread with the revised
    # document attached is how client mail is actually sent, and treating it as
    # an ordinary follow-up meant the "this has already gone out" warning never
    # fired in the one case it exists for.
    if outside and email.attachments:
        return EntryMode.BCC_SENT
    if email.in_reply_to:
        return EntryMode.REPLY

    if not any(is_agent(a) for a in email.to + email.cc):
        # We received it but are on no visible header, so we were BCC'd. That
        # only means "already gone" if someone outside the firm is on it.
        return EntryMode.BCC_SENT if outside else EntryMode.CC_DRAFT

    if any(is_agent(a) for a in email.cc):
        # CC'd alongside other people. Circulating to colleagues is still a
        # draft; the document has only left the building if an outside party
        # is on the message.
        return EntryMode.BCC_SENT if outside else EntryMode.CC_DRAFT

    # Addressed to us directly. Still a forward even if colleagues are copied,
    # but if a client is on the To line the document has already gone.
    return EntryMode.BCC_SENT if outside else EntryMode.FORWARD


def external_recipients(email: InboundEmail) -> list[str]:
    """Anyone on this message who is not the agent and not inside the firm.

    Used to warn when the agent is on a message that is heading out of the firm.
    """
    cfg = settings()
    out = []
    for addr in email.to + email.cc:
        if is_agent(addr) or cfg.is_internal(addr):
            continue
        out.append(addr)
    return out


def resolve_user(email: InboundEmail) -> str:
    """The lawyer this review belongs to. Their address is their identity."""
    return email.from_address.lower()


_ADDRESS = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def resolve_client(email: InboundEmail, matter_id: str | None, doc_text: str = "") -> str | None:
    """The client whose document this is, which memory is walled by
    (clients.py). From the matter number first; failing that, from the
    registered clients whose domains are on the message (or in a forwarded
    header in its body) or whose names are in the document, and only when
    exactly one is. None means no memory crosses matters on this review."""
    from lra import clients

    cfg = settings()
    seen = [*email.to, *email.cc, *_ADDRESS.findall(email.body or "")]
    outside = [a for a in seen if not is_agent(a) and not cfg.is_internal(a)]
    try:
        return clients.resolve(matter_id, outside, doc_text)
    except Exception:
        # Resolution failing must never fail a review; it walls memory to
        # the matter instead, which is the conservative answer anyway.
        log.exception("could not resolve the client")
        return None


def resolve_matter(email: InboundEmail, doc_text: str = "") -> str | None:
    """Best guess at the matter.

    Order of confidence:
      1. an explicit matter number in the subject or body
      2. a plus-address tag, if anyone ever uses one
      3. TODO(dms): a DMS lookup by document name or content fingerprint
    Returns None rather than guessing badly. A wrong matter means memory bleeds
    between deals, which is worse than no matter at all.
    """
    text = f"{email.subject}\n{email.body}"
    m = re.search(r"\b(?:matter|file|client)\s*(?:no\.?|number|#)?\s*[:#]?\s*([A-Z0-9][\w.-]{3,})",
                  text, re.IGNORECASE)
    if m:
        return m.group(1)
    for addr in email.to + email.cc:
        if "+" in addr.split("@")[0]:
            tag = addr.split("@")[0].split("+", 1)[1]
            if _TAP_TAG.match(tag.lower()):
                # A one-tap reply's conversation, not a matter.
                continue
            return tag
    return None



