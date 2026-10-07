"""Cloudflare Email Service, in and out, through the edge Worker.

Inbound is not a webhook here. Cloudflare hands the Worker the raw message, the
Worker seals it into R2 and starts a Workflow for it, and the Workflow's first
step POSTs the raw bytes to `/prepare` on this service (main.py, flow.py). So
what arrives is a real RFC 5322 message,
parsed by the same code `lra replay` uses, which is the parser with the most
tests behind it. There is no vendor JSON to drift out of step with.

Outbound goes back through the Worker, because the send binding only exists
there. Two limits shape it:

- 5 MiB per message, attachments included, unless the recipient is an address
  verified in the Cloudflare account, when it is 25 MiB. We cannot see which
  applies, so the limit is configurable and a message over it is refused here,
  before anything is sent, with `MessageTooLarge`. That is the one exception a
  caller should catch to resend without the attachments; it says whether doing
  so would fit (`fits_without_attachments`), so a body that is itself too big
  is not retried into the same refusal.
- The Message-ID of what we send is chosen by Cloudflare, not by us. A lawyer's
  reply quotes it in In-Reply-To. The id the send call returns is registered
  as an alias, and because that may not be the header value, replies are also
  matched on the References chain (thread.resolve), which always contains the
  lawyer's own first message.
"""

from __future__ import annotations

import base64
import hmac
import logging
from typing import Any

from lra import edge
from lra.config import settings
from lra.mail.console import ConsoleProvider
from lra.models import InboundEmail, OutboundEmail

log = logging.getLogger(__name__)


class MessageTooLarge(RuntimeError):
    """The reply is bigger than Cloudflare will carry. Raised before sending,
    so nothing went out; catch it and resend with `attachments=[]`.

    `size` and `limit` are in bytes as they travel (attachments base64);
    `attachment_bytes` is how much of `size` the attachments are, and
    `fits_without_attachments` whether the same message without them is under
    the limit.
    """

    def __init__(self, size: int, limit: int, attachment_bytes: int) -> None:
        super().__init__(f"reply is {size} bytes and Cloudflare carries {limit}")
        self.size = size
        self.limit = limit
        self.attachment_bytes = attachment_bytes

    @property
    def fits_without_attachments(self) -> bool:
        return self.size - self.attachment_bytes <= self.limit


class CloudflareProvider:
    name = "cloudflare"

    def verify(self, headers: dict[str, str], raw_body: bytes) -> bool:
        """The caller must be our own Worker. There is no vendor signature:
        the secret is one we generated and gave to both sides."""
        expected = f"Bearer {settings().edge_secret}"
        presented = headers.get("authorization", "")
        return bool(settings().edge_secret) and hmac.compare_digest(
            presented.encode(), expected.encode())

    def parse_inbound(self, payload: dict[str, Any]) -> InboundEmail:
        raise NotImplementedError("Cloudflare delivers raw messages; use parse_raw")

    def parse_raw(self, raw: bytes) -> InboundEmail:
        return ConsoleProvider().parse_eml(raw)

    def send(self, email: OutboundEmail, outbox: str | None = None) -> str:
        """Send through the Worker. `outbox` names this send ("<job>:<kind>")
        on the Workflow path, where a retried step must not send twice: the
        Worker sends once per name and answers a repeat with the first id."""
        body = payload(email)
        if outbox:
            body["outbox"] = outbox
        response = edge.call("/internal/send", json=body)
        return str(response.json().get("messageId") or "")


def payload(email: OutboundEmail) -> dict:
    """The reply in the shape `/internal/send` takes, and the Worker's send step
    reads from object storage. Raises MessageTooLarge before anything is sent."""
    cfg = settings()
    attachments = [
        {
            "filename": a.filename,
            "type": a.content_type or "application/octet-stream",
            "content": base64.b64encode(a.content).decode(),
        }
        for a in email.attachments
    ]
    # Base64 is what travels, so that is what is measured, and the body in
    # bytes rather than characters.
    attached = sum(len(a["content"]) for a in attachments)
    size = (len(email.text_body.encode("utf-8"))
            + len((email.html_body or "").encode("utf-8")) + attached)
    limit = cfg.cloudflare_max_message_mb * 1024 * 1024
    if size > limit:
        raise MessageTooLarge(size, limit, attached)

    headers = dict(email.headers)
    if email.in_reply_to:
        headers.update({"In-Reply-To": email.in_reply_to, "References": email.in_reply_to})

    return {
        "from": {"email": cfg.mail_agent_address, "name": cfg.mail_agent_name},
        "to": email.to,
        "cc": email.cc,
        "subject": email.subject,
        "text": email.text_body,
        "html": email.html_body or None,
        "headers": headers,
        "attachments": attachments,
    }
