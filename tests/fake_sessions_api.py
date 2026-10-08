"""A scripted `sessions_api.Sessions`, for tests of the Workflow path.

Each started session gets the next scripted outcome: a ReviewResult to return,
or an exception for collect() to raise. It stays RUNNING for `polls` status
calls, then DONE. Nothing runs; what the tests are about is the orchestration
around the session, not the session.
"""

from __future__ import annotations

from secondeye import sessions_api
from secondeye.models import ReviewResult


class FakeSessionsApi:
    def __init__(self, *outcomes: ReviewResult | Exception, polls: int = 0) -> None:
        self.outcomes = list(outcomes)
        self.polls = polls
        self.started: list[sessions_api.ReviewJob] = []
        self.collected: list[str] = []
        self._sessions: dict[str, dict] = {}

    def start(self, job: sessions_api.ReviewJob) -> str:
        self.started.append(job)
        session_id = f"sesn_fake_{len(self.started)}"
        self._sessions[session_id] = {"outcome": self.outcomes.pop(0), "polls": self.polls}
        return session_id

    def status(self, session_id: str) -> str:
        session = self._sessions.get(session_id)
        if session is None:
            return sessions_api.LOST
        if session["polls"] > 0:
            session["polls"] -= 1
            return sessions_api.RUNNING
        return sessions_api.DONE

    def collect(self, session_id: str, job: sessions_api.ReviewJob) -> ReviewResult:
        self.collected.append(session_id)
        session = self._sessions.get(session_id)
        if session is None:
            raise sessions_api.ReviewFailed("unknown session")
        outcome = session["outcome"]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome.model_copy(deep=True)
