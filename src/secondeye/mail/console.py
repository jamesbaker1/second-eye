"""Local development provider: prints instead of sending, no signature check.

Lets the whole pipeline run end to end with `second-eye replay <eml-file>` before any
vendor account exists.
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from secondeye.config import settings
from secondeye.models import Attachment, InboundEmail, OutboundEmail

log = logging.getLogger(__name__)


class UnparseableMessage(ValueError):
    """A .eml we refuse to turn into a job, with a sentence saying why."""


def _text(part) -> str:
    """The text of a MIME part, whatever charset it claims to be in.

    `get_content()` raises LookupError for a charset Python has never heard of
    ("ansi", "unicode-1-1-utf-8", and whatever an older gateway invented on the
    way through), and UnicodeDecodeError when the declared charset is simply a
    lie. Replay of a real mailbox export used to end in a traceback for those,
    while every other unreadable-input path in the system answers with a
    sentence. A slightly mis-decoded covering email is far better than no
    review at all, so decode permissively and say so in the log.
    """
    try:
        return part.get_content()
    except (LookupError, UnicodeDecodeError) as e:
        raw = part.get_payload(decode=True) or b""
        log.warning(
            "could not decode message body (%s); falling back to utf-8 with "
            "replacement characters",
            e,
        )
        return raw.decode("utf-8", errors="replace")


def _in_reply_to(msg) -> str | None:
    """The message this one is answering, if it is answering one.

    Without this, replay could never produce EntryMode.REPLY or match an
    existing thread, so the whole follow-up half of the product was untestable
    from a real .eml. Some clients send only References, so fall back to its
    last id, which is the immediate parent.
    """
    direct = (msg.get("In-Reply-To") or "").strip()
    if direct:
        return direct
    refs = (msg.get("References") or "").split()
    return refs[-1] if refs else None


def _attachments_of(msg, depth: int = 0) -> list[Attachment]:
    """Every file attached, including inside an attached email.

    Outlook's "Forward as attachment" wraps the original as message/rfc822.
    Read as a part it decoded to nothing, so the contract inside was never
    opened and the lawyer was told there was no document. Its attachments are
    lifted out, one level deep, which is as far as anyone forwards on purpose.
    """
    out: list[Attachment] = []
    for part in msg.iter_attachments():
        if part.get_content_type() == "message/rfc822" and depth < 2:
            inner = part.get_payload(0) if part.is_multipart() else None
            if inner is not None:
                out.extend(_attachments_of(inner, depth + 1))
            continue
        data = part.get_payload(decode=True) or b""
        out.append(
            Attachment(
                filename=part.get_filename() or "attachment",
                content_type=part.get_content_type(),
                size_bytes=len(data),
                content=data,
            )
        )
    return out


class ConsoleProvider:
    name = "console"

    def verify(self, headers: dict[str, str], raw_body: bytes) -> bool:
        return True

    def parse_inbound(self, payload: dict[str, Any]) -> InboundEmail:
        return InboundEmail(**payload)

    def parse_eml(self, raw: bytes) -> InboundEmail:
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        attachments = _attachments_of(msg)
        from_address = email.utils.parseaddr(msg.get("From", ""))[1]
        if not from_address:
            # An empty sender is not a degraded review, it is a wrong one:
            # identity.resolve_user returns "", the agent runs as nobody, and
            # reply.compose addresses the verdict to [""]. Refuse instead.
            raise UnparseableMessage(
                "This message has no usable From header, so there is nobody to "
                "run the review as and nobody to reply to."
            )
        plain = msg.get_body(preferencelist=("plain",))
        # HTML-only mail is normal, not exotic. Dropping it here meant a replay
        # of a real message had an empty body: no instructions, no memo-only
        # detection, and the checks that compare the email against the document
        # silently had nothing to compare.
        html = msg.get_body(preferencelist=("html",))
        return InboundEmail(
            headers=_headers(msg),
            message_id=msg.get("Message-ID") or str(uuid.uuid4()),
            in_reply_to=_in_reply_to(msg),
            references=(msg.get("References") or "").split(),
            from_address=from_address,
            to=[a for _, a in email.utils.getaddresses(msg.get_all("To", []))],
            cc=[a for _, a in email.utils.getaddresses(msg.get_all("Cc", []))],
            subject=msg.get("Subject", ""),
            text_body=_text(plain) if plain is not None else "",
            html_body=_text(html) if html is not None else "",
            attachments=attachments,
            received_at=datetime.now(UTC),
        )

    def send(self, email_out: OutboundEmail) -> str:
        print("=" * 70)
        cfg = settings()
        print(f"From: {email.utils.formataddr((cfg.mail_agent_name, cfg.mail_agent_address))}")
        print(f"To: {', '.join(email_out.to)}")
        print(f"Subject: {email_out.subject}")
        for name, value in email_out.headers.items():
            print(f"{name}: {value}")
        print("-" * 70)
        print(email_out.text_body)
        for att in email_out.attachments:
            print(f"[attachment] {att.filename} ({att.size_bytes} bytes)")
        print("=" * 70)
        return "console-" + str(uuid.uuid4())


# Headers a sender can add copies of to hide the gateway's own verdict.
_EVERY_COPY = ("authentication-results", "arc-authentication-results",
               "x-authentication-results")


def _headers(msg) -> dict[str, str]:
    """Lower-cased headers. Last copy wins, except for authentication results.

    A dict keeps one value per name, and the gateway prepends its verdict above
    whatever the sender wrote, so a forged "dmarc=pass" at the bottom used to
    replace the real "dmarc=fail" at the top. Every copy is kept for those, and
    one failure anywhere is a failure: a sender can only ever add a fail
    against themselves.
    """
    out = {k.lower(): str(v) for k, v in msg.items()}
    for name in _EVERY_COPY:
        copies = msg.get_all(name) or []
        if len(copies) > 1:
            out[name] = "\n".join(str(c) for c in copies)
    return out

