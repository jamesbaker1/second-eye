"""The story's emails as RFC 5322 messages.

`emails.yaml` says what each email is; this turns one step of it into bytes
a mail client can open and send, or `lra replay` and the rehearsal can feed
to the handler. Everything that varies between runs in a normal message (the
Message-ID, the MIME boundaries, the date) is fixed here, so the same step
always makes the same bytes.
"""

from __future__ import annotations

import mimetypes
from email.message import EmailMessage
from email.utils import parseaddr
from functools import cache
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
EMAILS = HERE / "emails.yaml"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@cache
def story() -> dict:
    """emails.yaml, with the lawyer's and the agent's addresses and the
    lawyer's name filled in from cast (they are never in the file)."""
    from demos.falcon import cast

    text = EMAILS.read_text()
    for token, value in (("{{lawyer}}", f"{cast.LAWYER_NAME} <{cast.LAWYER}>"),
                         ("{{lawyer_first}}", cast.IDENTITY.lawyer_first),
                         ("{{agent}}", f"{cast.AGENT_NAME} <{cast.AGENT}>")):
        text = text.replace(token, value)
    return yaml.safe_load(text)


def steps() -> list[dict]:
    return story()["steps"]


def step(step_id: str) -> dict:
    for s in steps():
        if str(s["id"]) == str(step_id):
            return s
    raise KeyError(f"no step {step_id!r} in emails.yaml; the steps are "
                   + ", ".join(str(s["id"]) for s in steps()))


def person(key: str) -> str:
    return story()["people"][key]


def address(key: str) -> str:
    return parseaddr(person(key))[1]


def message_id(step_id: str) -> str:
    return f"<falcon-{step_id}@demo.example>"


def subject_of(s: dict) -> str:
    """A reply's subject is the thread's, with "Re:" once."""
    if s.get("subject"):
        return s["subject"]
    parent = step(s["reply_to"])
    base = subject_of(parent)
    return base if base.lower().startswith("re:") else f"Re: {base}"


def _content_type(name: str) -> tuple[str, str]:
    if name.lower().endswith(".docx"):
        return tuple(DOCX.split("/"))  # type: ignore[return-value]
    guessed = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return tuple(guessed.split("/"))  # type: ignore[return-value]


def build_message(s: dict, files: dict[str, tuple[str, bytes]],
                  in_reply_to: str | None = None,
                  references: list[str] | None = None,
                  with_bcc: bool = True) -> bytes:
    """One step as a .eml. `files` maps an attachment key to (filename,
    bytes). `in_reply_to` threads it under the agent's reply it answers.
    `with_bcc` keeps the Bcc header, which a mail client needs to send it and
    which a delivered copy never carries: the rehearsal leaves it out, so the
    handler sees exactly what the agent's mailbox would receive."""
    msg = EmailMessage()
    msg["From"] = person(s.get("from", "lawyer"))
    msg["To"] = ", ".join(person(k) for k in s["to"])
    if s.get("cc"):
        msg["Cc"] = ", ".join(person(k) for k in s["cc"])
    if s.get("bcc") and with_bcc:
        msg["Bcc"] = ", ".join(person(k) for k in s["bcc"])
    msg["Subject"] = subject_of(s)
    msg["Date"] = s["when"]
    msg["Message-ID"] = message_id(s["id"])
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = " ".join(references or [in_reply_to])
    msg["X-Falcon-Step"] = str(s["id"])
    msg.set_content(s["body"])
    for key in s.get("attachments") or []:
        name, data = files[key]
        maintype, subtype = _content_type(name)
        msg.add_attachment(data, maintype=maintype, subtype=subtype,
                           filename=Path(name).name)
    for n, part in enumerate(p for p in msg.walk() if p.is_multipart()):
        part.set_boundary(f"==falcon-{s['id']}-{n}==")
    return msg.as_bytes()
