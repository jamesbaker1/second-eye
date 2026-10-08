"""A stand-in for the Managed Agents client, scripted per test.

The real session is a stream of events we react to: a custom tool call we
answer, a message, an idle. The fake plays a script of those, records what we
sent back, and serves whatever files the test says the agent wrote to the
outputs directory. Nothing here talks to the network, and nothing in it knows
what a review is; it is the platform, as seen from our side of the stream.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS

from anthropic.types.beta.beta_file_metadata import BetaFileMetadata
from anthropic.types.beta.beta_managed_agents_session import BetaManagedAgentsSession
from anthropic.types.beta.sessions.beta_managed_agents_session_event import (
    BetaManagedAgentsSessionEvent,
)

from tests import api_contract as contract

_ids = itertools.count(1)
_clock = itertools.count(1)
_EPOCH = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def _id(prefix: str = "sevt") -> str:
    return f"{prefix}_{next(_ids):05d}"


def _now() -> str:
    """A processed_at that moves forward with every event, as the platform's does."""
    return (_EPOCH + timedelta(milliseconds=next(_clock))).isoformat().replace("+00:00", "Z")


def event(type: str, **fields):
    """A session event as the API sends it, held to the SDK's event union:
    an event type or a field the real API does not have fails here."""
    fields.setdefault("id", _id())
    fields.setdefault("processed_at", _now())
    return contract.model(BetaManagedAgentsSessionEvent, type=type, **fields)


def _usage() -> dict:
    return {"input_tokens": 1200, "output_tokens": 300, "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0}


# --- events the fake agent can emit -----------------------------------------

def tool_use(name: str, **input):
    return event("agent.custom_tool_use", name=name, input=input)


def message(text: str):
    return event("agent.message", content=[{"type": "text", "text": text}])


def idle(reason: str = "end_turn", event_ids: list[str] | None = None):
    stop: dict = {"type": reason}
    if reason == "requires_action":
        stop["event_ids"] = list(event_ids or [])
    return event("session.status_idle", stop_reason=stop)


def running():
    return event("session.status_running")


def model_turn_end():
    return event("span.model_request_end", model_request_start_id=_id(),
                 model_usage=_usage(), is_error=False)


def terminated():
    return event("session.status_terminated")


def error(text: str, kind: str = "unknown_error", retry: str = "exhausted"):
    """A `session.error`. The real error is one of a closed set of types, each
    with a retry status (beta_managed_agents_session_error_event.py); there
    is no free-form error type."""
    return event("session.error", error={"type": kind, "message": text,
                                         "retry_status": {"type": retry}})


# The verdicts the grader can return (beta_managed_agents_span_outcome_evaluation_end_event.py).
OUTCOME_RESULTS = ("satisfied", "needs_revision", "max_iterations_reached", "failed",
                   "interrupted")


def outcome_end(result: str, explanation: str = "", iteration: int = 0):
    if result not in OUTCOME_RESULTS:
        raise contract.ContractError(f"the grader never returns {result!r}; "
                                     f"it returns one of {OUTCOME_RESULTS}")
    return event("span.outcome_evaluation_end", result=result, explanation=explanation,
                 iteration=iteration, outcome_evaluation_start_id=_id(),
                 outcome_id=_id("outc"), usage=_usage())


def session_object(session_id: str, status: str, *, created: dict | None = None):
    """A `BetaManagedAgentsSession`, as `sessions.create` and `retrieve` return
    it, reflecting what it was created with."""
    created = created or {}
    agent = created.get("agent", "agent_review")
    agent_id = agent if isinstance(agent, str) else agent["id"]
    stamp = _now()
    return contract.model(
        BetaManagedAgentsSession,
        id=session_id, type="session", status=status, created_at=stamp, updated_at=stamp,
        environment_id=created.get("environment_id", "env_test"),
        title=created.get("title"),
        metadata=dict(created.get("metadata") or {}),
        vault_ids=list(created.get("vault_ids") or []),
        agent={"id": agent_id, "type": "agent", "name": "LRA agent", "version": 1,
               "model": {"id": "claude-fable-5-1"}, "mcp_servers": [], "skills": [],
               "tools": []},
        outcome_evaluations=[], resources=[], stats={}, usage={},
    )


def bad_request(message: str):
    """The 400 the real API answers with, as the SDK raises it."""
    import anthropic
    import httpx2

    request = httpx2.Request("POST", "https://api.anthropic.com/v1/sessions")
    response = httpx2.Response(400, request=request)
    return anthropic.BadRequestError(message, response=response,
                                     body={"type": "error", "error": {
                                         "type": "invalid_request_error", "message": message}})


def file_metadata(file_id: str, filename: str, size: int, session_id: str | None = None):
    """A `BetaFileMetadata`: what `files.upload` and `files.list` return."""
    import mimetypes

    fields: dict = {"id": file_id, "type": "file", "filename": filename, "size_bytes": size,
                    "created_at": _now(),
                    "mime_type": mimetypes.guess_type(filename)[0] or "application/octet-stream"}
    if session_id:
        fields["scope"] = {"id": session_id, "type": "session"}
        fields["downloadable"] = True
    return contract.model(BetaFileMetadata, **fields)


class Step:
    """A point in the script where the agent is waiting on us.

    When the stream reaches a Step, our loop must already have sent something
    (a tool result, an interrupt) since the previous Step; `then` is given
    that batch and returns the events the agent produces in answer. If nothing
    was sent, the stream simply ends, which is what a deadlocked session looks
    like from the client.
    """

    def __init__(self, then: Callable[[list[dict]], list] | None = None):
        self.then = then or (lambda sent: [])


def after_tool_result(*events) -> Step:
    """Continue with these events once our custom tool result has been sent."""
    return Step(lambda sent: list(events))


class _SessionsView:
    """`client.beta.sessions` on a fake that is also `client.beta.files`:
    everything is the fake's own, except `delete`, which on sessions deletes
    a session and on files a file."""

    def __init__(self, fake) -> None:
        self._fake = fake

    def __getattr__(self, name):
        return getattr(self._fake, "delete_session" if name == "delete" else name)


class FakeSessions:
    """`client.beta.sessions` and `client.beta.files`, scripted.

    `script` is a flat list of events and Steps, consumed in order by the
    stream. The stream ends when the script does.
    """

    def __init__(self, script: list, outputs: list[tuple[str, bytes]] | None = None,
                 drop_after: int | None = None, status_lag: int = 1):
        self.script = list(script)
        # The session's status as the server holds it. The stream emits
        # `session.status_idle` slightly before the queryable status says so
        # (managed-agents-client-patterns.md, pattern 6): `status_lag` is how
        # many retrieves still read "running" after an idle, and archiving
        # in that window is a 400, as it is live.
        self.status = "idle"
        self.status_lag = status_lag
        self._lag_left = 0
        self.sent: list[list[dict]] = []
        self.created: list[dict] = []
        self.uploaded: list[tuple[str, bytes]] = []
        self.deleted: list[str] = []
        self.archived: list[str] = []
        # Sessions deleted (`client.beta.sessions.delete`): the record, its
        # events and its sandbox, gone. Archiving would keep them.
        self.deleted_sessions: list[str] = []
        self.outputs = outputs or []
        self.history: list = []
        self.stream_opens = 0
        # Drop the stream after this many events, once, to exercise reconnect.
        self.drop_after = drop_after
        self._dropped = False
        self._consumed_sends = 0

        beta = NS(sessions=_SessionsView(self), files=self)
        # Every call our code makes goes through the SDK's own signatures and
        # types (tests/api_contract.py), and every answer comes back as the
        # SDK's own objects.
        self.client = contract.strict(NS(beta=beta))
        self.events = NS(stream=self._stream, list=self._list, send=self._send)

    # -- sessions ----------------------------------------------------------
    def create(self, **kwargs):
        self.created.append(kwargs)
        # With initial_events the session is created directly in `running`;
        # without, in `idle` (managed-agents-core.md, initial_events).
        self.status = "running" if kwargs.get("initial_events") else "idle"
        return session_object("sesn_fake", self.status, created=kwargs)

    def _stopped(self, status: str = "idle") -> None:
        """The session came to rest; the queryable status catches up later."""
        self.status = status
        self._lag_left = self.status_lag

    def _visible_status(self) -> str:
        return "running" if self._lag_left > 0 else self.status

    def retrieve(self, session_id, **kw):
        status = self._visible_status()
        if self._lag_left > 0:
            self._lag_left -= 1
        return session_object(session_id, status,
                              created=self.created[-1] if self.created else None)

    def archive(self, session_id, **kw):
        if self._visible_status() in ("running", "rescheduling"):
            raise bad_request("cannot archive a session while it is running")
        self.archived.append(session_id)
        return session_object(session_id, self.status,
                              created=self.created[-1] if self.created else None)

    def delete_session(self, session_id, **kw):
        if self._visible_status() in ("running", "rescheduling"):
            raise bad_request("cannot delete a session while it is running")
        self.deleted_sessions.append(session_id)
        return {"id": session_id, "type": "session_deleted"}

    # -- events ------------------------------------------------------------
    def _send(self, session_id, events):
        events = [dict(e) for e in events]
        self.sent.append(events)
        for sent in events:
            if sent["type"] == "user.interrupt":
                # It stops the turn at the next safe boundary, then idles.
                if self.status == "running":
                    self._stopped()
            elif self.status != "terminated":
                self.status = "running"
        return {"data": self._echo(events)}

    def _echo(self, events: list[dict]) -> list:
        """What the platform records for events we sent: each gets an id and
        a processed_at, and lands in the history (and on the stream) like any
        other event."""
        # "Interrupt events may have empty IDs in the current implementation"
        # (managed-agents-events.md, Interrupt), so ours do: nothing may key on one.
        echoed = [event(e["type"], **({"id": ""} if e["type"] == "user.interrupt" else {}),
                        **{k: v for k, v in e.items() if k != "type"})
                  for e in events]
        self.history.extend(echoed)
        return echoed

    def _list(self, session_id, types=None, order=None, limit=None, **kw):
        events = [e for e in self.history if not types or e.type in types]
        if order == "desc":
            events.reverse()
        return events[:limit] if limit else events

    def _stream(self, session_id, **kw):
        self.stream_opens += 1
        fake = self

        class Stream:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def __iter__(self):
                delivered = 0
                while fake.script:
                    item = fake.script.pop(0)
                    if isinstance(item, Step):
                        if len(fake.sent) <= fake._consumed_sends:
                            return          # nothing sent: the session waits forever
                        fake._consumed_sends = len(fake.sent)
                        fake.script[:0] = item.then(fake.sent[-1])
                        continue
                    fake.history.append(item)
                    delivered += 1
                    if item.type == "session.status_terminated":
                        fake._stopped("terminated")
                    elif (item.type == "session.status_idle"
                          and item.stop_reason.type != "requires_action"):
                        fake._stopped()
                    yield item
                    if (fake.drop_after is not None and not fake._dropped
                            and delivered >= fake.drop_after):
                        fake._dropped = True
                        import anthropic
                        import httpx2
                        raise anthropic.APIConnectionError(
                            request=httpx2.Request("GET", "http://fake"), message="dropped")

        return Stream()

    # -- files -------------------------------------------------------------
    def upload(self, file, **kw):
        name, data = file[0], file[1]
        self.uploaded.append((name, data))
        return file_metadata(f"file_{len(self.uploaded)}", name, len(data))

    def delete(self, file_id, **kw):
        self.deleted.append(file_id)
        return {"id": file_id, "type": "file_deleted"}

    def list(self, **kw):
        if "scope_id" in kw:
            return [file_metadata(f"out_{i}", name, len(data), kw["scope_id"])
                    for i, (name, data) in enumerate(self.outputs)]
        return []

    def download(self, file_id, **kw):
        index = int(file_id.split("_")[1])
        data = self.outputs[index][1]
        return NS(read=lambda: data)


# --- a session nobody is attached to ------------------------------------------

class Turn:
    """What the agent does between two of our messages, in a detached session.

    `events` land in the history when the turn ends (the last is usually an
    idle), `outputs` are files it writes to /mnt/session/outputs by then (a
    name mapped to None deletes one), and `polls` is how many times the
    session reads as running before it does. `on_interrupt` is what is left
    in the history if we interrupt it first; None means the interrupt is
    ignored and the session keeps running, as a wedged one would.
    """

    _STOP = object()

    def __init__(self, *events, outputs: dict[str, bytes | None] | None = None,
                 polls: int = 1, on_interrupt: list | None | object = _STOP):
        self.events = list(events)
        self.outputs = dict(outputs or {})
        self.polls = polls
        self.on_interrupt = [idle("end_turn")] if on_interrupt is Turn._STOP else on_interrupt


class DetachedSessions(FakeSessions):
    """The platform as a client that polls sees it: a session is created,
    runs its first Turn with nobody attached, and is read back afterwards.
    A user.message runs the next Turn; with none left, the session ignores it
    (which is what a lost message looks like from outside).

    `lag` is how many output listings come back empty before the files
    appear, the indexing lag the docs describe.

    As live: an interrupt stops the turn at its next safe boundary, not on
    receipt, so it shows at the next retrieve; archiving a session that is
    still running is a 400; and a session paused at its budget refuses a
    `user.message` with a 400 (managed-agents-events.md).
    """

    def __init__(self, turns: list[Turn], lag: int = 0, status_after_create: str = "running"):
        super().__init__([])
        self.turns = list(turns)
        self.files: dict[str, bytes] = {}
        self.status = "idle"
        self.retrieves = 0
        self.lag = lag
        self.listings = 0
        self._turn: Turn | None = None
        self._polls_left = 0
        self._status_after_create = status_after_create
        self._interrupted = False

    def _begin(self) -> None:
        if not self.turns:
            return
        self._turn = self.turns.pop(0)
        self._polls_left = self._turn.polls
        self._interrupted = False
        self.status = "running"

    def _end(self, events: list, outputs: dict) -> None:
        self.history.extend(events)
        for name, data in outputs.items():
            if data is None:
                self.files.pop(name, None)
            else:
                self.files[name] = data
        self.status = ("terminated" if any(e.type == "session.status_terminated"
                                           for e in events) else "idle")
        self._turn = None

    # -- sessions ----------------------------------------------------------
    def create(self, **kwargs):
        self.created.append(kwargs)
        self._begin()
        return session_object("sesn_fake", self._status_after_create, created=kwargs)

    def retrieve(self, session_id, **kw):
        self.retrieves += 1
        if self._turn is not None:
            if self._interrupted and self._turn.on_interrupt is not None:
                self._end(self._turn.on_interrupt, {})
            elif self._polls_left > 0:
                self._polls_left -= 1
            else:
                self._end(self._turn.events, self._turn.outputs)
        return session_object(session_id, self.status,
                              created=self.created[-1] if self.created else None)

    def _paused_at_budget(self) -> bool:
        idles = [e for e in self.history if e.type == "session.status_idle"]
        return bool(idles) and idles[-1].stop_reason.type == "budget_reached"

    def _send(self, session_id, events):
        events = [dict(e) for e in events]
        if self._paused_at_budget() and any(
                e["type"] in ("user.message", "user.define_outcome") for e in events):
            raise bad_request("the session is paused at its budget; only user.tool_confirmation, "
                              "user.tool_result, user.custom_tool_result and user.interrupt "
                              "are accepted")
        self.sent.append(events)
        echoed = self._echo(events)
        for sent in events:
            if sent["type"] == "user.interrupt" and self._turn is not None:
                self._interrupted = True
            elif sent["type"] == "user.message" and self._turn is None:
                self._begin()
        return {"data": echoed}

    def archive(self, session_id, **kw):
        if self.status in ("running", "rescheduling"):
            raise bad_request("cannot archive a session while it is running")
        self.archived.append(session_id)
        return session_object(session_id, self.status,
                              created=self.created[-1] if self.created else None)

    def delete_session(self, session_id, **kw):
        if self.status in ("running", "rescheduling"):
            raise bad_request("cannot delete a session while it is running")
        self.deleted_sessions.append(session_id)
        return {"id": session_id, "type": "session_deleted"}

    # -- files -------------------------------------------------------------
    def list(self, **kw):
        if "scope_id" not in kw:
            return []
        self.listings += 1
        if self.listings <= self.lag:
            return []
        return [file_metadata(f"out_{i}", name, len(data), kw["scope_id"])
                for i, (name, data) in enumerate(self.files.items())]

    def download(self, file_id, **kw):
        data = list(self.files.values())[int(file_id.split("_")[1])]
        return NS(read=lambda: data)


def configure(monkeypatch, **env) -> None:
    """Point settings at fake agent ids so `managed.configured()` is true."""
    defaults = {
        "ANTHROPIC_API_KEY": "sk-test",
        "MANAGED_REVIEW_AGENT_ID": "agent_review",
        "MANAGED_ASSOCIATE_AGENT_ID": "agent_associate",
        "MANAGED_ENVIRONMENT_ID": "env_test",
        # The pre-phase-5 setting, empty so a developer's .env cannot make
        # `second-eye agents apply` adopt a real agent in a test.
        "MANAGED_REVIEW_DETACHED_AGENT_ID": "",
        # A developer's .env may name a real workspace; the Console URL the
        # tests assert on must not depend on it.
        "ANTHROPIC_WORKSPACE_ID": "",
    }
    defaults.update(env)
    for key, value in defaults.items():
        monkeypatch.setenv(key, value)
    from secondeye import managed

    # The fakes settle on the next retrieve; nothing to wait for between polls.
    monkeypatch.setattr(managed, "SETTLE_POLL_SECONDS", 0.0)
    from secondeye.config import settings

    settings.cache_clear()
