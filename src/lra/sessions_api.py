"""The review session as three calls: start it, ask how it is, collect it.

The Cloudflare Workflow (flow.py, cloudflare/src/review-flow.ts) never holds a
session open. It starts one, waits for Anthropic's webhook or a timeout, asks
for its status, and collects the result once it is done, each in a separate
container call that may land on a different process. This module is the seam
between that orchestration and whatever runs the session:

    start(job)                 -> session id
    status(session_id)         -> RUNNING | DONE | LOST
    collect(session_id, job)   -> ReviewResult, or ReviewFailed

`DetachedSessions` is the one implementation: the clientless session
(review.start, review.finish). Its ids are Anthropic's, so the webhook
(cloudflare/src/webhook.ts) wakes the Workflow waiting on it, and it survives
the container being replaced between steps. The streaming review that ran on
a thread in this process, with local ids no webhook matched, was removed in
docs/migration.md, phase 5.

`use()` replaces it; tests/fake_sessions_api.py is a scripted one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from lra.models import Mode, ReviewResult

log = logging.getLogger(__name__)

RUNNING = "running"
DONE = "done"
LOST = "lost"


class ReviewFailed(RuntimeError):
    """The session ended without a review we can use. Retrying the collection
    will not change that; the caller sends the mechanical-only reply."""


@dataclass
class ReviewJob:
    """Everything a review session is given: review.review's arguments."""

    job_id: str
    doc: object                      # extract.ExtractedDoc
    mode: Mode
    instructions: str
    user_address: str
    matter_id: str | None = None
    memory_stores: dict[str, str] | None = None
    checks_block: str = ""
    house_style: str = ""
    original: bytes | None = None
    native: tuple[str, bytes] | None = None
    changes_block: str = ""
    their_paper: bool = False
    # A new version from the other side of a document with a ledger
    # (negotiation.py): the fenced evidence and the file mounted beside it.
    negotiation_block: str = ""
    negotiation_evidence: bytes | None = None

    def review_kwargs(self) -> dict:
        """The keyword arguments review.review takes, from this job."""
        return {
            "doc": self.doc,
            "mode": self.mode,
            "instructions": self.instructions,
            "user_address": self.user_address,
            "matter_id": self.matter_id,
            "memory_stores": self.memory_stores,
            "checks_block": self.checks_block,
            "house_style": self.house_style,
            "original": self.original,
            "native": self.native,
            "changes_block": self.changes_block,
            "their_paper": self.their_paper,
            "negotiation_block": self.negotiation_block,
            "negotiation_evidence": self.negotiation_evidence,
        }


class Sessions(Protocol):
    def start(self, job: ReviewJob) -> str: ...

    def status(self, session_id: str) -> str: ...

    def collect(self, session_id: str, job: ReviewJob) -> ReviewResult: ...


class DetachedSessions:
    """A session nobody watches (docs/migration.md, phases 1 and 5)."""

    def start(self, job: ReviewJob) -> str:
        from lra.pipeline import review

        return review.start(
            job.doc, job.mode, job.instructions, matter_id=job.matter_id,
            memory_stores=job.memory_stores, checks_block=job.checks_block,
            house_style=job.house_style, original=job.original,
            changes_block=job.changes_block, native=job.native,
            their_paper=job.their_paper, negotiation_block=job.negotiation_block,
            negotiation_evidence=job.negotiation_evidence,
            user_address=job.user_address,
        )

    def status(self, session_id: str) -> str:
        import anthropic

        from lra import managed

        try:
            session = managed.anthropic_client().beta.sessions.retrieve(session_id)
        except anthropic.NotFoundError:
            return LOST
        return RUNNING if str(getattr(session, "status", "") or "") in managed.ACTIVE else DONE

    def collect(self, session_id: str, job: ReviewJob) -> ReviewResult:
        """review.finish: reads how the session stopped and what it left, and
        archives it. Its RuntimeErrors are the review failing (a refusal, no
        findings, the platform giving up), which no retry changes."""
        from lra.pipeline import review

        try:
            return review.finish(
                session_id, job.mode, user_address=job.user_address,
                matter_id=job.matter_id, document=getattr(job.doc, "filename", ""),
                their_paper=job.their_paper,
            )
        except RuntimeError as e:
            raise ReviewFailed(str(e)) from e


_DETACHED: Sessions = DetachedSessions()
_override: Sessions | None = None


def backend() -> Sessions:
    """The session runner the Workflow endpoints use."""
    if _override is not None:
        return _override
    return _DETACHED


def use(sessions: Sessions | None) -> Sessions | None:
    """Make `sessions` the runner (None: back to Anthropic's); returns
    the override it replaced."""
    global _override
    previous, _override = _override, sessions
    return previous
