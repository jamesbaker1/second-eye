"""Postmark adapter: inbound parse webhook + transactional send.

Fallback option. Postmark has no thread model, so we reconstruct threading from
In-Reply-To / References headers ourselves: inbound we thread on the sender's
own RFC Message-ID rather than Postmark's internal UUID, and outbound we emit
both headers pointing at it. That is enough for Gmail and Outlook to group the
reply under the message it answers. Carrying the sender's full References chain
through as well would need a references field on the shared email models.

Inbound attachment cap is 35 MB.
"""

from __future__ import annotations

import base64
import hmac
import logging
from datetime import UTC, datetime
from email.utils import formataddr
from typing import Any

import httpx

from secondeye.config import settings
from secondeye.models import Attachment, InboundEmail, OutboundEmail

log = logging.getLogger(__name__)


class PostmarkProvider:
    name = "postmark"

    def __init__(self) -> None:
        self.cfg = settings()
        self.client = httpx.Client(
            base_url="https://api.postmarkapp.com",
            headers={"X-Postmark-Server-Token": self.cfg.postmark_server_token},
            timeout=60,
        )

    def verify(self, headers: dict[str, str], raw_body: bytes) -> bool:
        """Postmark does not sign inbound webhooks, so this checks a shared
        secret sent as a header.

        Refuses everything when no secret is configured. Comparing against an
        empty string used to make an unset secret accept every unauthenticated
        POST, and since the agent resolves identity from the forgeable From
        header, anyone who found the URL could have had it search a partner's
        document system.
        """
        expected = self.cfg.postmark_inbound_secret
        if not expected:
            log.error(
                "POSTMARK_INBOUND_SECRET is not set; rejecting inbound webhooks "
                "rather than accepting unauthenticated mail"
            )
            return False
        sent = headers.get("x-inbound-secret", "")
        # compare_digest raises TypeError on a non-ASCII str, which would turn
        # one accented character in a header into a 500 where a plain wrong
        # secret gives a 401. A header we cannot compare is a failed check.
        if sent.isascii() and hmac.compare_digest(sent, expected):
            return True
        # A rejection reaches the operator as a bare 401 and Postmark simply
        # retries, so the only place this can be diagnosed is the log. Say
        # whether the header was missing entirely (webhook URL configured
        # without the header) or present and wrong (secret rotated).
        log.error(
            "rejecting inbound webhook: x-inbound-secret %s",
            "header absent" if not sent else "does not match POSTMARK_INBOUND_SECRET",
        )
        return False

    def parse_inbound(self, payload: dict[str, Any]) -> InboundEmail:
        headers = {h["Name"].lower(): h["Value"] for h in payload.get("Headers", [])}
        # The sender's own Message-ID, not Postmark's internal UUID. Threading
        # on the UUID means no mail client ever groups the reply with the
        # original, which breaks the whole reply-in-thread model.
        rfc_message_id = headers.get("message-id") or payload["MessageID"]
        attachments = []
        for a in payload.get("Attachments", []):
            content = base64.b64decode(a["Content"])
            attachments.append(
                Attachment(
                    filename=a["Name"],
                    content_type=a.get("ContentType") or "application/octet-stream",
                    # Measured from the bytes we actually decoded rather than
                    # taken from ContentLength. intake enforces
                    # MAX_ATTACHMENT_MB against this one number, so a claim
                    # that disagreed with reality would disable the only size
                    # cap in the system without erroring.
                    size_bytes=len(content),
                    content=content,
                )
            )
        return InboundEmail(
            headers=headers,
            message_id=rfc_message_id,
            in_reply_to=headers.get("in-reply-to"),
            from_address=payload["From"],
            from_name=payload.get("FromName"),
            to=[r["Email"] for r in payload.get("ToFull", [])],
            cc=[r["Email"] for r in payload.get("CcFull", [])],
            subject=payload.get("Subject", ""),
            text_body=payload.get("TextBody", ""),
            html_body=payload.get("HtmlBody", ""),
            attachments=attachments,
            received_at=datetime.now(UTC),
        )

    def send(self, email: OutboundEmail) -> str:
        body = {
            # With the display name. A bare address shows in the inbox as
            # "review", which is the mailbox, not who is writing.
            "From": formataddr((self.cfg.mail_agent_name, self.cfg.mail_agent_address)),
            "To": ",".join(email.to),
            "Cc": ",".join(email.cc),
            "Subject": email.subject,
            "TextBody": email.text_body,
            "HtmlBody": email.html_body or None,
            "MessageStream": "outbound",
            "Attachments": [
                {
                    "Name": a.filename,
                    "ContentType": a.content_type,
                    "Content": base64.b64encode(a.content).decode(),
                }
                for a in email.attachments
            ],
        }
        headers = [{"Name": k, "Value": v} for k, v in email.headers.items()]
        if email.in_reply_to:
            # References as well as In-Reply-To: Gmail and Outlook both use it,
            # and without it a reply starts a new conversation.
            headers += [
                {"Name": "In-Reply-To", "Value": email.in_reply_to},
                {"Name": "References", "Value": email.in_reply_to},
            ]
        if headers:
            body["Headers"] = headers
        r = self.client.post("/email", json=body)
        r.raise_for_status()
        return r.json()["MessageID"]
