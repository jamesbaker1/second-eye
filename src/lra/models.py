"""Core domain types. Everything the pipeline passes around lives here."""

from __future__ import annotations

import re
from datetime import datetime
from email.utils import parseaddr
from enum import Enum
from html import unescape
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Attachment(BaseModel):
    filename: str
    content_type: str
    size_bytes: int
    content: bytes = Field(repr=False)


def html_to_text(html: str) -> str:
    """A readable plain-text rendering of an HTML mail body.

    Many clients send HTML only. Reading text_body alone meant the covering
    message vanished: instructions were ignored, memo-only was never detected,
    "revoke" stopped working, and the checks that compare the email against the
    document went silently dead.
    """
    if not html:
        return ""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?is)<head\b.*?</head\s*>", " ", text)
    # Rendered the way the same client writes its plain part, so a reply sent
    # as HTML only is separated from its quoted history like any other:
    # quoted text as "> " lines, Outlook's rule as its underscores, and a
    # signature under "-- ".
    text = _quoted_blocks(text)
    text = re.sub(r"(?i)<hr\b[^>]*>", "<br>" + "_" * 32 + "<br>", text)
    text = re.sub(r"(?i)(<div\b[^>]*(?:\bid=\"?Signature\b|class=\"[^\"]*\b(?:gmail_signature|"
                  r"moz-signature)\b)[^>]*>)", r"<br>-- <br>\1", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|tr|li|h[1-6]|blockquote)\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


_INNERMOST_QUOTE = re.compile(r"(?is)<blockquote\b[^>]*>((?:(?!<blockquote\b).)*?)</blockquote\s*>")


def _quoted_blocks(html: str) -> str:
    """Each <blockquote>, innermost first, as its text with "> " before
    every line, the way a plain-text reply quotes."""
    from html import escape

    def quoted(m: re.Match) -> str:
        lines = html_to_text(m.group(1)).splitlines()
        return "<br>" + "<br>".join(escape(f"> {ln}" if ln else ">") for ln in lines) + "<br>"

    for _ in range(20):
        html, n = _INNERMOST_QUOTE.subn(quoted, html)
        if not n:
            break
    return html


def bare_address(value: str) -> str:
    """The address alone, with any display name removed.

    Mail arrives as `Jane Partner <jane@firm.com>`, and that whole string used
    to become `from_address`. Everything downstream keys off that field:
    identity resolution, the ALLOWED_SENDERS allowlist, is_internal, and the
    thread key. So a lawyer using any normal mail client was rejected by the
    allowlist, counted as outside the firm, and given a different conversation
    depending on whether their display name happened to be present.

    Normalised once, here, so no adapter can forget.
    """
    if not value:
        return ""
    parsed = parseaddr(value)[1] or value
    return parsed.strip().strip("<>").strip()


class InboundEmail(BaseModel):
    """Provider-agnostic shape. Every mail adapter normalizes into this."""

    message_id: str
    thread_id: str | None = None
    in_reply_to: str | None = None
    # Every ancestor in the conversation, oldest first, from the References
    # header. A reply is matched to its thread by any of them.
    references: list[str] = []
    from_address: str
    from_name: str | None = None
    to: list[str] = []
    cc: list[str] = []
    subject: str = ""
    text_body: str = ""
    html_body: str = ""
    attachments: list[Attachment] = []
    received_at: datetime
    # Raw headers, lower-cased keys. Needed to spot auto-replies, bounces and
    # mailing lists, which must never be answered. Without these an
    # auto-responder and this agent will talk to each other indefinitely.
    headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("from_address", mode="before")
    @classmethod
    def _bare_sender(cls, value: str) -> str:
        return bare_address(value or "")

    @field_validator("to", "cc", mode="before")
    @classmethod
    def _bare_recipients(cls, value):
        if not value:
            return []
        return [bare_address(v) for v in value if bare_address(v)]

    @property
    def body(self) -> str:
        """What the sender wrote, whichever format they sent it in."""
        if self.text_body and self.text_body.strip():
            return self.text_body
        return html_to_text(self.html_body)


class Mode(str, Enum):
    """What the user asked for. Inferred from the email body, overridable."""

    REDLINE = "redline"        # return an edited doc with tracked changes
    MEMO_ONLY = "memo_only"    # return findings, do not touch the doc
    PROOFREAD = "proofread"    # formatting/typos only, no substantive edits
    QUESTION = "question"      # a question about the doc, no edits


class Severity(str, Enum):
    BLOCKER = "blocker"        # do not send until resolved
    SUBSTANTIVE = "substantive"
    STYLE = "style"
    FORMATTING = "formatting"


class Finding(BaseModel):
    severity: Severity
    category: str                       # e.g. "indemnity", "defined-term", "cross-ref"
    title: str                          # one line, plain English
    explanation: str                    # why it matters, 1-3 sentences
    anchor: str                         # exact source text to locate the span
    suggested_text: str | None = None   # replacement; None for memo-only findings
    confidence: float = 1.0
    auto_apply: bool = False            # safe to write as a tracked change

    # When the defect is certain but the correct answer is not, offer the
    # choices rather than guessing or staying silent. The email renders these
    # as something the lawyer can answer in one word, and the reply applies it.
    # "thirty (13) days" is definitely wrong; only the lawyer knows which.
    options: list[str] = Field(default_factory=list)
    question: str | None = None         # the one-line question to put to them

    # How the writer should apply this. "replace" swaps the anchor for
    # suggested_text; "insert_after" adds suggested_text as a new paragraph
    # following the anchor's, which is what drafting a new clause needs.
    edit_kind: Literal["replace", "insert_after"] = "replace"

    # Where the finding is, as the reader would say it: "clause 3.2",
    # "Schedule 4, paragraph 2". Empty when it is before the first clause or
    # could not be placed. Filled by the deterministic checks.
    where: str = ""

    # Only on the other side's paper (their_paper.py): the firm's position on
    # what their draft asks for, and what to say back. They are the issues
    # list's last two columns; empty elsewhere, and the list falls back to the
    # explanation and the suggested wording.
    our_position: str = ""
    response: str = ""


class ReviewResult(BaseModel):
    mode: Mode
    summary: str
    findings: list[Finding] = []
    applied: list[str] = []             # titles of findings written into the doc
    skipped: list[str] = []             # titles raised in the memo only
    # The agent hit its time budget and was told to report with what it had.
    # The reply says so: a partial review labelled as complete is worse than
    # no review.
    cut_short: bool = False
    # Files the review session wrote to its outputs, as (filename, bytes).
    # Untrusted until handler.py has verified them locally (DECISIONS 29).
    outputs: list[tuple[str, bytes]] = []
    # The Managed Agents session, for the Console trace.
    session_id: str = ""
    # The document is the counterparty's draft, not the firm's (their_paper.py):
    # nothing cosmetic is written into their text, and the substantive points
    # become an issues list.
    their_paper: bool = False
    # negotiation.json as the session wrote it, when it was given negotiation
    # evidence (negotiation.py). Untrusted until negotiation.settle has
    # checked it against that evidence.
    negotiation: dict | None = None


class JobStatus(str, Enum):
    RECEIVED = "received"
    REJECTED = "rejected"
    EXTRACTING = "extracting"
    REVIEWING = "reviewing"
    REDLINING = "redlining"
    REPLIED = "replied"
    FAILED = "failed"


class Job(BaseModel):
    id: str
    status: JobStatus = JobStatus.RECEIVED
    email: InboundEmail
    mode: Mode = Mode.REDLINE
    result: ReviewResult | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class OutboundEmail(BaseModel):
    to: list[str]
    cc: list[str] = []
    subject: str
    text_body: str
    html_body: str = ""
    in_reply_to: str | None = None
    thread_id: str | None = None
    attachments: list[Attachment] = []
    # Extra headers every provider sends as given (X-Privileged, policy.py).
    # Threading headers are built from in_reply_to, not put here.
    headers: dict[str, str] = {}
