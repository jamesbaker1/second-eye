"""AgentMail adapter.

AgentMail gives an agent a real inbox (threads, labels, search) rather than a
bare inbound-parse webhook, which is why it is the default candidate. Fill in
the endpoint details against docs.agentmail.to once an API key exists.

Everything here is written from documentation nobody could test against a live
account, so the rule in this file is: never fail quietly. A wrong guess about a
field name or a signature format must show up as a log line naming what we
actually received, because the alternative is a permanent 401 or a silently
unthreaded reply that looks exactly like success.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from secondeye.config import settings
from secondeye.models import Attachment, InboundEmail, OutboundEmail

API_BASE = "https://api.agentmail.to/v0"

log = logging.getLogger(__name__)

# The header carrying the signature is a documentation guess, so check the
# plausible spellings rather than 401-ing forever on a name mismatch.
SIGNATURE_HEADERS = (
    "x-agentmail-signature",
    "agentmail-signature",
    "x-webhook-signature",
)

# Scheme prefixes some vendors put in front of the digest (`sha256=<hex>`,
# Svix-style `v1,<base64>`). Anything else is treated as the digest itself.
SIGNATURE_PREFIXES = ("sha256", "sha-256", "hmac", "v1", "s")


def _candidate_signatures(raw_value: str) -> list[str]:
    """Every digest that could be hiding in a signature header value.

    The same HMAC over the same body gets encoded differently by different
    vendors: a bare hex digest, `sha256=<hex>`, or a space-separated list of
    `v1,<base64>` values. Accepting any encoding of the digest we computed
    weakens nothing -- the secret and the signed bytes are unchanged -- and
    removes three ways for a correctly configured deployment to reject every
    message forever.
    """
    candidates: list[str] = []
    for token in re.split(r"[,\s]+", raw_value.strip()):
        if not token:
            continue
        candidates.append(token)
        prefix, sep, rest = token.partition("=")
        if sep and rest and prefix.lower() in SIGNATURE_PREFIXES:
            candidates.append(rest)
    return candidates


class AgentMailProvider:
    name = "agentmail"

    def __init__(self) -> None:
        self.cfg = settings()
        self.client = httpx.Client(
            base_url=API_BASE,
            headers={"Authorization": f"Bearer {self.cfg.agentmail_api_key}"},
            timeout=60,
        )

    def verify(self, headers: dict[str, str], raw_body: bytes) -> bool:
        """Authenticate the webhook. Fails closed, but never silently.

        main.py turns a False here into a bare 401. AgentMail then retries,
        backs off and disables the endpoint, and the symptom a firm sees is
        lawyers emailing documents into silence while /health still says ok.
        So every rejection logs at ERROR with enough detail -- which headers
        arrived, what shape the value had -- to tell a misconfiguration from an
        attack without a debugger.
        """
        secret = self.cfg.agentmail_webhook_secret
        if not secret:
            log.error(
                "AGENTMAIL_WEBHOOK_SECRET is not set; rejecting every inbound "
                "webhook rather than accepting unauthenticated mail"
            )
            return False

        sent = next((headers[h] for h in SIGNATURE_HEADERS if headers.get(h)), "")
        if not sent:
            log.error(
                "inbound webhook carried no signature header; looked for %s, "
                "received %s. Until these agree, every message is rejected.",
                ", ".join(SIGNATURE_HEADERS),
                ", ".join(sorted(headers)) or "no headers",
            )
            return False

        digest = hmac.new(secret.encode(), raw_body, hashlib.sha256)
        expected_hex = digest.hexdigest()
        expected_b64 = base64.b64encode(digest.digest()).decode()
        for candidate in _candidate_signatures(sent):
            if not candidate.isascii():
                continue
            if hmac.compare_digest(candidate.lower(), expected_hex) or hmac.compare_digest(
                candidate, expected_b64
            ):
                return True

        # Log the shape, not the secret. A signature that is the right length
        # but never matches means the signed payload is not the raw body (Svix
        # signs `{id}.{timestamp}.{body}`); a value like "v1,..." says the
        # scheme differs. Both are invisible from a 401 alone.
        log.error(
            "AgentMail signature mismatch: received %r (%d chars) against a "
            "%d-char hex digest of the raw body. If this is every request, the "
            "signing scheme differs from the one assumed here.",
            sent[:16],
            len(sent),
            len(expected_hex),
        )
        return False

    def parse_inbound(self, payload: dict[str, Any]) -> InboundEmail:
        msg = payload.get("message", payload)
        message_id = msg.get("message_id") or msg.get("id")
        from_address = msg.get("from") or msg.get("from_address")
        if not message_id or not from_address:
            # A bare KeyError here reaches the operator as a 500 with a
            # one-word traceback. Name the keys we did get: if AgentMail
            # renames a field, that log line is the whole diagnosis.
            raise ValueError(
                "AgentMail webhook payload has no usable message id or sender. "
                f"Keys present: {', '.join(sorted(msg)) or 'none'}"
            )

        attachments = []
        for a in msg.get("attachments", []):
            content = self._fetch_attachment(message_id, a)
            attachments.append(
                Attachment(
                    filename=a.get("filename") or a.get("name") or "attachment",
                    content_type=a.get("content_type")
                    or a.get("contentType")
                    or "application/octet-stream",
                    # Measured, never trusted. The provider may name this field
                    # size, size_bytes, length or byte_size; a missing one
                    # defaulted to zero and silently disabled MAX_ATTACHMENT_MB,
                    # so a 300 MB document would have been decoded into memory
                    # and fed to the model.
                    size_bytes=len(content),
                    content=content,
                )
            )
        return InboundEmail(
            headers={k.lower(): v for k, v in (msg.get("headers") or {}).items()},
            message_id=message_id,
            thread_id=_thread_id(msg),
            in_reply_to=msg.get("in_reply_to") or msg.get("inReplyTo"),
            from_address=from_address,
            to=msg.get("to", []),
            cc=msg.get("cc", []),
            subject=msg.get("subject", ""),
            text_body=msg.get("text", "") or msg.get("text_body", "") or "",
            html_body=msg.get("html", "") or msg.get("html_body", "") or "",
            attachments=attachments,
            received_at=datetime.now(UTC),
        )

    def _fetch_attachment(self, message_id: str, meta: dict[str, Any]) -> bytes:
        if meta.get("content"):
            return base64.b64decode(meta["content"])
        inbox = self.cfg.agentmail_inbox_id
        r = self.client.get(
            f"/inboxes/{inbox}/messages/{message_id}/attachments/{meta['attachment_id']}"
        )
        r.raise_for_status()
        return r.content

    def send(self, email: OutboundEmail) -> str:
        inbox = self.cfg.agentmail_inbox_id
        body = {
            "to": email.to,
            "cc": email.cc,
            "subject": email.subject,
            "text": email.text_body,
            "html": email.html_body or None,
            "attachments": [
                {
                    "filename": a.filename,
                    "content_type": a.content_type,
                    "content": base64.b64encode(a.content).decode(),
                }
                for a in email.attachments
            ],
        }
        # Threading. Without at least In-Reply-To, every review arrives as a
        # fresh conversation and the reply-to-revise loop the footer advertises
        # never links up.
        if email.in_reply_to:
            body["in_reply_to"] = email.in_reply_to
            body["references"] = email.in_reply_to
        if email.headers:
            body["headers"] = dict(email.headers)
        if email.thread_id:
            path = f"/inboxes/{inbox}/threads/{email.thread_id}/reply"
        else:
            path = f"/inboxes/{inbox}/messages/send"
            if email.in_reply_to:
                # The quietest failure this adapter has: if the inbound payload
                # names its thread field anything but the spellings _thread_id
                # knows, every reply takes this branch and lands as an
                # unrelated message. Nothing else would ever say so.
                log.warning(
                    "replying to %s with no AgentMail thread id; sending as a new "
                    "message with In-Reply-To/References only. If this appears on "
                    "every reply, the inbound payload's thread field is not one of "
                    "thread_id/threadId/thread and threading is broken.",
                    email.in_reply_to,
                )
        r = self.client.post(path, json=body)
        r.raise_for_status()
        return r.json().get("message_id", "")


def _thread_id(msg: dict[str, Any]) -> str | None:
    """The AgentMail thread this message belongs to, under any of its names.

    Guessed from documentation, so accept the obvious spellings and a nested
    object. When this comes back None the reply is sent as a new message, which
    looks like success and is not, so send() logs that case.
    """
    raw = msg.get("thread_id") or msg.get("threadId") or msg.get("thread")
    if isinstance(raw, dict):
        raw = raw.get("id") or raw.get("thread_id")
    return str(raw) if raw else None
