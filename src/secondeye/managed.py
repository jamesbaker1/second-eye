"""Anthropic Managed Agents: the one place this codebase drives a session.

DECISIONS 29. The review used to be a tool-runner loop we hosted, with the
document pasted into the prompt as text and base64, our own retry logic, our
own time budget, our own quality checks and our own memory block. Each of
those has a platform feature that does the same job, so each is gone:

| Ours                                  | Platform                              |
| ------------------------------------- | ------------------------------------- |
| tool-runner loop, capability menu     | a persisted Agent, one Session per job|
| document in the prompt                | Files API, mounted at /workspace      |
| report tool answered in-process       | findings.json, validated in the sandbox|
| wall-clock loop and iteration cap     | session budget, plus user.interrupt   |
| memory spliced into the system prompt | memory stores mounted in the container|
| our own report checks                 | an outcome with a rubric, graded      |
| log lines                             | the Console session trace             |

What stays ours is what Anthropic does not do: receiving and sending email,
thread state and undo, the deterministic checks (run locally too, so the agent
is told what was found), the consent flow for the document system, and re-verifying
every file that comes back before it is attached. The agents themselves are
defined in `agents/*.yaml` and applied once by `second-eye agents apply`; nothing in
the request path creates an agent.

Two ways to run a session live here. The review, the comments, the blackline
and every clientless agent run detached (docs/migration.md, phases 1 and 5):
an agent with no custom tools, started by `start_session`, polled by
`wait_until_stopped` (or woken by the Workflow's webhook), and read back from
its event history and its output files. That path has no stream to drop and
nothing to answer.

The associate (make_changes, note_for_next_time), the playbook reader
(record_playbook), the closing agent and the one-shot jobs (convert, repair)
still run through `run_session`, which holds the event stream open because a
custom tool needs a connected client. Two facts about that stream shape it:
it has no replay, so on every (re)connect the event history is read as well
and the two are deduped by event id; and an interrupt does not carry a
message, so the time budget is an interrupt followed by one message asking
for the report. Moving those agents to files is later work, not phase 5.
"""

from __future__ import annotations

import logging
import mimetypes
import re
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from secondeye import audit
from secondeye.config import anthropic_client, settings

log = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parents[2]
AGENTS = REPO / "agents"
PROMPTS = Path(__file__).parent / "prompts"

# The Files API knows its own beta; listing a session's outputs takes a
# Managed Agents parameter as well, so that call needs this header explicitly.
BETAS = ["managed-agents-2026-04-01"]

# Where the agent finds the document and where it must leave what it makes.
WORKSPACE = "/workspace"
OUTPUTS = "/mnt/session/outputs"

# Reconnects allowed per session before the job is failed. Each one re-reads
# the history, so a flapping connection costs bandwidth, not correctness.
MAX_RECONNECTS = 5

# Seconds the stream may go without a byte before we stop waiting on it and
# look at the clock. A quiet stream is not a dropped one (a long tool run
# emits nothing), so a read timeout does not count as a reconnect.
STREAM_READ_TIMEOUT = 60.0

# Past the time budget and the report grace, this much more and the session is
# abandoned whatever the stream is doing. The budget and grace are enforced on
# events; this is enforced by a timer that closes the stream, so a session
# that emits nothing, or a stream kept open by keep-alives, still ends.
HARD_DEADLINE_MARGIN = 60.0

# What the agents may consult beyond the sandbox's own file tools. Web search
# stays off unless the operator turns it on: a query written from a privileged
# document discloses part of it.
ANTHROPIC_SKILLS = ("docx", "xlsx", "pdf", "pptx")


class NotConfigured(RuntimeError):
    """The ids that point at the agents are missing. Nothing model-backed can run."""


class StreamDropped(RuntimeError):
    """The SSE stream ended without the session stopping. Reconnect."""


class SessionTimedOut(RuntimeError):
    """The session outran its hard deadline and was abandoned."""


def _required() -> list[tuple[str, str]]:
    cfg = settings()
    return [
        ("ANTHROPIC_API_KEY", cfg.anthropic_api_key),
        ("MANAGED_REVIEW_AGENT_ID", cfg.managed_review_agent_id),
        ("MANAGED_ASSOCIATE_AGENT_ID", cfg.managed_associate_agent_id),
        ("MANAGED_ENVIRONMENT_ID", cfg.managed_environment_id),
    ]


def configured() -> bool:
    from secondeye import policy

    # Not for a no-AI client's work (policy.py): every path that offers a
    # session asks this first, and takes its no-model branch.
    if policy.ai_forbidden():
        return False
    return all(value.strip() for _, value in _required())


def require_configured() -> None:
    from secondeye import policy

    # Every session starts here, a test's injected client included.
    policy.require_ai_allowed()
    missing = [name for name, value in _required() if not value.strip()]
    if missing:
        raise NotConfigured(
            "Managed Agents is not set up: " + ", ".join(missing) + " missing. "
            "Run `second-eye agents apply` and put the ids it prints in .env."
        )


def console_url(session_id: str) -> str:
    """The live trace in the Anthropic Console. Logged for every session, so
    a review can be read turn by turn while it runs; the session is deleted
    when it ends (`_clean_up`), and the trace with it."""
    workspace = settings().anthropic_workspace_id.strip() or "default"
    return f"https://platform.claude.com/workspaces/{workspace}/sessions/{session_id}"


def skill_refs() -> list[dict]:
    """Anthropic's document skills plus this product's own, by id, for an agent
    definition. Ours are absent until `second-eye skills sync` has uploaded them."""
    out = [{"type": "anthropic", "skill_id": name, "version": "latest"}
           for name in ANTHROPIC_SKILLS]
    cfg = settings()
    for custom in (cfg.sandbox_skill_id, cfg.sandbox_playbook_skill_id,
                   cfg.sandbox_key_terms_skill_id):
        if custom.strip():
            out.append({"type": "custom", "skill_id": custom.strip(), "version": "latest"})
    return out


def mount_name(filename: str) -> str:
    """A filename safe to use as a path component under /workspace."""
    cleaned = re.sub(r"[^A-Za-z0-9._ ()\[\]-]+", "_", filename)
    cleaned = re.sub(r"\.{2,}", ".", cleaned).strip(" ._") or "document"
    return cleaned[:120]


# --------------------------------------------------------------------------
# Driving one session
# --------------------------------------------------------------------------


@dataclass
class SessionRun:
    """What one session produced, beyond whatever the tools recorded."""

    session_id: str
    messages: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # The wall-clock budget ran out and the model was told to report now.
    cut_short: bool = False
    # Why the session stopped: end_turn, budget_reached, retries_exhausted,
    # terminated, or deadline when we interrupted it for good.
    stop: str = ""
    outcome: str = ""
    # (filename, bytes) written to /mnt/session/outputs, in listing order.
    outputs: list[tuple[str, bytes]] = field(default_factory=list)


ToolHandler = Callable[[dict], str]


def upload(client, filename: str, content: bytes) -> str:
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return client.beta.files.upload(file=(filename, content, mime)).id


def file_resources(client, files: Iterable[tuple[str, bytes]]) -> tuple[list[dict], list[str]]:
    """Upload each file and describe it as a session resource under /workspace.
    Returns the resources and the ids to delete once the session is done: the
    session takes its own copy, so the upload has no further use."""
    files = list(files)
    if len(files) <= 1:
        ids = [upload(client, name, content) for name, content in files]
    else:
        # At the same time: a PDF goes up with the Word copy built from it, and
        # the session cannot be created until both are in. One after the other
        # was a second upload's worth of waiting on every PDF review.
        with ThreadPoolExecutor(max_workers=len(files)) as pool:
            futures = [pool.submit(upload, client, name, content) for name, content in files]
        ids, failure = [], None
        for future in futures:
            try:
                ids.append(future.result())
            except Exception as e:  # noqa: BLE001 - re-raised below, after cleanup
                failure = failure or e
        if failure is not None:
            _delete_files(client, ids, "uploaded")
            raise failure
    # Deleted as soon as the session has its copy, but a delete can fail and
    # is then only a log line: the job's audit row keeps the ids, so a purge
    # of its client, matter or lawyer can make sure (purge.py).
    audit.uploaded(ids)
    resources = [{"type": "file", "file_id": file_id,
                  "mount_path": f"{WORKSPACE}/{mount_name(filename)}"}
                 for (filename, _), file_id in zip(files, ids, strict=True)]
    return resources, ids


def memory_resources(store_ids: dict[str, str]) -> list[dict]:
    """Memory stores as session resources. Every store is mounted read-only:
    the agent reads the firm's conventions, the lawyer's preferences and the
    matter's history with its file tools, but writes go through our own gate
    (memory.py), because text in a forwarded counterparty draft must not be
    able to plant a note that every later review reads back."""
    labels = {
        "firm": "House conventions, firm-wide. Personal preferences override them.",
        "personal": "This lawyer's preferences and what they have asked not to be told "
                    "about again. When you flag something because of one of these rather "
                    "than because it is objectively wrong, say so in the explanation.",
        "client": "What has been learned on this client's other matters: positions "
                  "observed, how its counterparties behave. It is about this client "
                  "only, and is never mounted on another client's review.",
        "matter": "What has happened on this matter: prior drafts, positions taken, "
                  "what was conceded. The most specific memory; use it.",
        "playbook": "The firm's own playbook, approved by the firm. Its positions/ files "
                    "replace the lra-playbook skill's starter positions entirely: read "
                    "positions from here, report against them as the firm's, and do not "
                    "use a starter file for any clause.",
    }
    return [
        {"type": "memory_store", "memory_store_id": store_id, "access": "read_only",
         "instructions": labels.get(scope, "")}
        for scope, store_id in store_ids.items() if store_id
    ]


def _budget() -> dict | None:
    cents = settings().managed_session_budget_cents
    if cents <= 0:
        return None
    return {"type": "limit", "max_list_cost": {"amount": str(int(cents)), "currency": "USD"}}


def run_session(
    *,
    agent_id: str,
    title: str,
    initial_events: list[dict],
    tools: dict[str, ToolHandler],
    files: Iterable[tuple[str, bytes]] = (),
    memory_stores: dict[str, str] | None = None,
    heartbeat: Callable[[], None] | None = None,
    time_budget: float | None = None,
    report_now: str = "",
    reported: Callable[[], bool] | None = None,
    client=None,
    collect_outputs: bool | Callable[[], bool] = True,
    access=None,
) -> SessionRun:
    """Create a session, drive it to a stop, and bring back what it wrote.

    `tools` maps custom tool names to handlers; each `agent.custom_tool_use`
    is answered with the handler's return value, or its exception as an error
    result the model can read. `heartbeat` is called on every event.

    `time_budget` is the wall-clock allowance in seconds. Past it the session
    is interrupted and sent `report_now` as one message; `reported()` says
    whether the tools have recorded a report, so a session that reported on
    the turn that crossed the line is complete, not cut short. Past a further
    grace period the session is interrupted for good.

    `collect_outputs` may be a callable, asked once the session has stopped:
    whether anything it could have written is worth waiting for. The listing
    is retried through an indexing lag when it comes back empty, which on a
    review that found nothing to change was seconds spent before the lawyer's
    "Looks good" could go out, waiting for a file that was never going to be
    written.

    `access` is the document system's part of the session
    (dms_mcp.SessionAccess): a matter-bound MCP URL and
    the vaults to attach.
    """
    require_configured()
    cfg = settings()
    client = client or anthropic_client()

    resources, uploaded = file_resources(client, files)
    resources += memory_resources(memory_stores or {})
    create: dict = {
        "agent": agent_id,
        "environment_id": cfg.managed_environment_id,
        "title": title[:200],
        "initial_events": initial_events,
    }
    if resources:
        create["resources"] = resources
    budget = _budget()
    if budget:
        create["budget"] = budget
    _with_access(create, agent_id, access)

    try:
        session = client.beta.sessions.create(**create)
        audit.session(session.id)
    except BaseException:
        _delete_files(client, uploaded, "uploaded")
        _release_access(client, access)
        raise
    run = SessionRun(session_id=session.id)
    log.info("session %s for %r: %s", session.id, title, console_url(session.id))

    try:
        _drive(client, session.id, run, tools, _safe(heartbeat), time_budget, report_now,
               reported)
        if collect_outputs() if callable(collect_outputs) else collect_outputs:
            run.outputs = outputs(client, session.id)
    finally:
        _clean_up(client, session.id, run, uploaded)
        _release_access(client, access)
    return run


def _with_access(create: dict, agent_id: str, access) -> None:
    """The document system's part of `sessions.create`: the agent's MCP
    server list replaced for this session only (`agent_with_overrides`, so
    the URL can carry this session's matter), and the vaults whose
    credentials the MCP connection uses. `vault_ids` is create-only."""
    if access is None:
        return
    if access.overrides:
        create["agent"] = {"type": "agent_with_overrides", "id": agent_id, **access.overrides}
    if access.vault_ids:
        create["vault_ids"] = list(access.vault_ids)


def _release_access(client, access) -> None:
    """Delete any vault made for this session alone (the capability fallback)."""
    if access is None or not access.ephemeral:
        return
    from secondeye import dms_mcp

    dms_mcp.release(client, vault_ids=access.ephemeral)


def _safe(heartbeat: Callable[[], None] | None) -> Callable[[], None] | None:
    """The heartbeat, unable to end the review.

    It is called on every event and, on Cloudflare, writes the job row through
    the edge. One refused write (an EdgeError, a timeout) used to propagate out
    of the event loop and abort a review that was going fine; the lease it
    extends only guards against a second delivery, and losing one beat of it
    costs nothing. A failure is logged once, not once per event.
    """
    if heartbeat is None:
        return None
    failed = False

    def beat() -> None:
        nonlocal failed
        try:
            heartbeat()
        except Exception:
            if not failed:
                log.warning("heartbeat failed; carrying on with the session", exc_info=True)
            failed = True

    return beat


def _delete_files(client, file_ids: Iterable[str], what: str) -> None:
    for file_id in file_ids:
        try:
            client.beta.files.delete(file_id)
        except Exception:  # noqa: BLE001 - cleanup must not mask the result
            log.warning("could not delete %s file %s", what, file_id)


def _clean_up(client, session_id: str, run: SessionRun, uploaded: list[str]) -> None:
    """Leave nothing behind, however the session ended.

    A session that raised (lost stream, hard deadline) used to be left
    running, spending against its budget with the document mounted. Every
    file the agent wrote stayed in the Files API indefinitely, a copy of
    privileged material nobody would read again. So: interrupt the session if
    it may still be running, delete what we uploaded and what it wrote (after
    `outputs` has downloaded it), and delete the session itself. Every step
    is best-effort.

    Deleted, not archived. Archiving kept the session read-only for ever,
    with its turn-by-turn record, which quotes the document; deleting removes
    the record, its events and its sandbox (Managed Agents docs, "Deleting a
    session"). The Console trace goes with it: the audit row keeps the
    session id, and what Anthropic keeps under its own retention is its
    affair (docs/trust.md). A delete that fails is retried by the daily
    retention sweep once the job is past its window (retention.py).

    The session must have stopped before it can be deleted: deleting one
    that is still running is a 400. An interrupt stops it at its next safe
    boundary, not on receipt, and even a session that has gone idle reads
    as running for a moment after its idle event is out. So the status is
    polled until it settles, and only then are its files listed and the
    session deleted; one that never settles is left for its budget to stop
    and the sweep to delete.
    """
    if run.stop in ("", "deadline"):
        try:
            client.beta.sessions.events.send(session_id=session_id,
                                             events=[{"type": "user.interrupt"}])
        except Exception:  # noqa: BLE001
            log.warning("could not interrupt session %s", session_id)
    settled = _settled(client, session_id)
    _delete_files(client, uploaded, "uploaded")
    try:
        written = [meta.id for meta in client.beta.files.list(scope_id=session_id,
                                                               betas=BETAS)]
    except Exception:  # noqa: BLE001
        log.warning("could not list the outputs of session %s to delete them", session_id)
        written = []
    _delete_files(client, written, "output")
    if not settled:
        log.warning("session %s was still running after it was interrupted; not deleted "
                    "(its budget stops it, and the retention sweep deletes it)", session_id)
        return
    try:
        client.beta.sessions.delete(session_id)
    except Exception:  # noqa: BLE001
        log.warning("could not delete session %s; the retention sweep will", session_id)


# How long `_settled` waits for a session to leave `running`: the interrupt
# takes effect at the next safe boundary, and the status write trails the
# idle event (managed-agents-client-patterns.md, pattern 6).
SETTLE_POLL_SECONDS = 0.5
SETTLE_ATTEMPTS = 20


def _settled(client, session_id: str) -> bool:
    """Whether the session's status has come to rest (not running)."""
    for attempt in range(SETTLE_ATTEMPTS):
        try:
            status = str(getattr(client.beta.sessions.retrieve(session_id), "status", "") or "")
        except Exception:  # noqa: BLE001 - let the delete try, and say why it failed
            log.warning("could not read the status of session %s", session_id)
            return True
        if status not in ACTIVE:
            return True
        if attempt < SETTLE_ATTEMPTS - 1:
            time.sleep(SETTLE_POLL_SECONDS)
    return False


def _events_with_history(client, session_id: str, seen: set[str],
                         hard_deadline: float | None = None):
    """Open the stream, then read the history, then tail the stream.

    Order matters and is the documented pattern: the stream buffers from the
    moment it opens, the history covers anything before that or lost in a
    gap, and the id set stops an event being handled twice. Yields every
    event, seen or not; the caller decides what to act on, because a terminal
    event that was in the history must still end the loop.
    """
    with client.beta.sessions.events.stream(session_id=session_id,
                                            timeout=STREAM_READ_TIMEOUT) as stream:
        watchdog = None
        closer = getattr(stream, "close", None)
        if hard_deadline is not None and closer is not None:
            # Closing the stream from a timer is the only way to stop waiting
            # on a stream that is alive but silent, or kept open by pings.
            watchdog = threading.Timer(max(0.0, hard_deadline - time.monotonic()), closer)
            watchdog.daemon = True
            watchdog.start()
        try:
            for event in client.beta.sessions.events.list(session_id=session_id):
                yield event
            for event in stream:
                yield event
        finally:
            if watchdog is not None:
                watchdog.cancel()


def _text_of(event) -> str:
    return "".join(getattr(block, "text", "") or ""
                   for block in getattr(event, "content", None) or []
                   if getattr(block, "type", "") == "text")


def _drive(client, session_id: str, run: SessionRun, tools: dict[str, ToolHandler],
           heartbeat, time_budget, report_now: str, reported) -> None:
    # Here rather than at the top of the module: the SDK is over half the
    # application's import time, and a container woken by "clean copy" or
    # "undo 2" never calls it.
    import anthropic

    cfg = settings()
    started = time.monotonic()
    deadline = started + time_budget if time_budget else None
    hard = (started + (time_budget or cfg.agent_time_budget_seconds)
            + cfg.agent_report_grace_seconds + HARD_DEADLINE_MARGIN)
    grace: float | None = None
    seen: set[str] = set()
    handled_tools: set[str] = set()
    asked = False
    turns_since_ask = 0
    reconnects = 0

    def send(events: list[dict]) -> None:
        client.beta.sessions.events.send(session_id=session_id, events=events)

    def clock() -> bool:
        """The time budget. True when the loop should stop now."""
        nonlocal asked, grace
        now = time.monotonic()
        if deadline is not None and not asked and now >= deadline:
            if reported is not None and reported():
                log.info("time budget reached with a report in hand; stopping")
                send([{"type": "user.interrupt"}])
                run.stop = "end_turn"
                return True
            log.warning("time budget reached without a report; asking for one now")
            asked = True
            run.cut_short = True
            grace = now + cfg.agent_report_grace_seconds
            events: list[dict] = [{"type": "user.interrupt"}]
            if report_now:
                events.append({"type": "user.message",
                               "content": [{"type": "text", "text": report_now}]})
            send(events)
        elif grace is not None and now >= grace:
            log.warning("no report within the grace period; interrupting for good")
            send([{"type": "user.interrupt"}])
            run.stop = "deadline"
            return True
        return False

    def check_hard_deadline(cause: BaseException | None = None) -> None:
        if time.monotonic() >= hard:
            raise SessionTimedOut(
                f"session {session_id} ran past its hard deadline and was abandoned"
            ) from cause

    while True:
        try:
            for event in _events_with_history(client, session_id, seen, hard):
                if heartbeat is not None:
                    heartbeat()
                event_id = getattr(event, "id", "") or ""
                fresh = not event_id or event_id not in seen
                if event_id:
                    seen.add(event_id)
                kind = getattr(event, "type", "")

                if fresh:
                    if kind == "agent.custom_tool_use" and event_id not in handled_tools:
                        handled_tools.add(event_id)
                        send([_answer(tools, event)])
                    elif kind == "agent.message":
                        text = _text_of(event)
                        if text:
                            run.messages.append(text)
                    elif kind == "session.error":
                        error = getattr(event, "error", None)
                        run.errors.append(str(getattr(error, "message", None) or error))
                        log.error("session %s error: %s", session_id, run.errors[-1])
                    elif kind == "span.outcome_evaluation_end":
                        run.outcome = str(getattr(event, "result", "") or "")
                        log.info("session %s outcome iteration %s: %s (%s)", session_id,
                                 getattr(event, "iteration", "?"), run.outcome,
                                 (getattr(event, "explanation", "") or "")[:300])
                    elif kind == "span.model_request_end" and asked:
                        turns_since_ask += 1

                    # Stop decisions are made once per event. A replayed event
                    # was decided when first seen: had it been terminal the loop
                    # would have ended then. Deciding again on a reconnect is
                    # how the idle that follows our "report now" interrupt,
                    # rightly skipped when it arrived, ended the loop on replay
                    # once a later turn had been counted, before the report.
                    if kind == "session.status_terminated":
                        run.stop = "terminated"
                        return
                    if kind == "session.status_idle":
                        reason = getattr(getattr(event, "stop_reason", None), "type", "") or ""
                        if reason == "requires_action":
                            continue
                        if asked and turns_since_ask == 0 and reason == "end_turn":
                            # The idle that follows our interrupt. The message
                            # asking for the report is queued behind it and has
                            # not been answered yet.
                            continue
                        run.stop = reason or "idle"
                        return

                # The time budget, checked on every event so a long turn is
                # caught at its next tool call rather than at its end.
                if clock():
                    return
            # The stream closed without a terminal event. Treat it as a drop.
            raise StreamDropped("the event stream ended before the session stopped")
        except anthropic.APITimeoutError as e:
            # Nothing arrived for STREAM_READ_TIMEOUT seconds. Not a fault: a
            # long tool run is quiet. Look at the clock, then listen again.
            check_hard_deadline(e)
            if clock():
                return
            log.info("session %s quiet for %.0fs; still waiting", session_id,
                     STREAM_READ_TIMEOUT)
        except (anthropic.APIConnectionError, StreamDropped) as e:
            check_hard_deadline(e)
            reconnects += 1
            if reconnects > MAX_RECONNECTS:
                raise RuntimeError(
                    f"lost the session stream {reconnects} times; giving up") from e
            log.warning("session %s stream dropped (%s); reconnecting", session_id, e)
            time.sleep(min(2.0 * reconnects, 10.0))
        except Exception as e:
            # The hard-deadline timer closed the stream under the reader, which
            # surfaces as whatever the HTTP library raises for a closed body.
            check_hard_deadline(e)
            raise


def _answer(tools: dict[str, ToolHandler], event) -> dict:
    handler = tools.get(event.name)
    if handler is None:
        return {"type": "user.custom_tool_result", "custom_tool_use_id": event.id,
                "is_error": True,
                "content": [{"type": "text", "text": f"There is no tool called {event.name}."}]}
    try:
        text = handler(dict(event.input or {}))
        return {"type": "user.custom_tool_result", "custom_tool_use_id": event.id,
                "content": [{"type": "text", "text": str(text)}]}
    except Exception as e:
        log.exception("custom tool %s failed", event.name)
        return {"type": "user.custom_tool_result", "custom_tool_use_id": event.id,
                "is_error": True,
                "content": [{"type": "text", "text": f"{event.name} failed: {e}"}]}


def outputs(client, session_id: str, attempts: int = 3, wait: float = 1.5,
            sleep: Callable[[float], None] | None = None) -> list[tuple[str, bytes]]:
    """Everything the agent left in /mnt/session/outputs, as (filename, bytes).

    There is a short indexing lag after the session goes idle, so an empty
    listing is retried. Nothing returned here is trusted: every caller checks
    the file locally before it goes anywhere near a lawyer.
    """
    for attempt in range(attempts):
        found: list[tuple[str, bytes]] = []
        try:
            for meta in client.beta.files.list(scope_id=session_id, betas=BETAS):
                data = client.beta.files.download(meta.id).read()
                if data:
                    found.append((Path(meta.filename or meta.id).name, data))
        except Exception:
            log.exception("could not list the outputs of session %s", session_id)
            return []
        if found or attempt == attempts - 1:
            return found
        (sleep or time.sleep)(wait)
    return []


# --------------------------------------------------------------------------
# A session with no client attached
# --------------------------------------------------------------------------
#
# docs/migration.md, phase 1. Everything above holds the event stream open
# for the whole session, because a custom tool needs someone to answer it.
# A session whose agent has no custom tools needs nobody until it stops: it
# is started, left alone, and read afterwards, by whoever is around to read
# it (this process polling, or a Cloudflare Workflow woken by a webhook).
# Nothing here keeps state between the two halves beyond the session id.

# Statuses in which the agent may still be working. `rescheduling` is the
# platform retrying after a transient error; it comes back to running.
ACTIVE = ("running", "rescheduling")

# Seconds between polls of a session nobody is streaming.
POLL_SECONDS = 5.0


class StillRunning(RuntimeError):
    """The session had not stopped by the time we stopped waiting for it."""


@dataclass
class Stopped:
    """A session at rest, and why, as read back from its event history."""

    session_id: str
    # The session's status when read: idle or terminated.
    status: str
    # The last idle's stop_reason: end_turn, budget_reached, retries_exhausted
    # or requires_action; "terminated" when the session was.
    stop: str
    # The id of the idle event that was read, so a later wait can tell a new
    # stop from this one.
    idle_id: str = ""
    errors: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    outcome: str = ""


def start_session(
    *,
    agent_id: str,
    title: str,
    initial_events: list[dict],
    files: Iterable[tuple[str, bytes]] = (),
    memory_stores: dict[str, str] | None = None,
    metadata: dict[str, str] | None = None,
    client=None,
    access=None,
) -> str:
    """Create a session that will run to a stop with nobody attached, and
    return its id. `initial_events` start it (the session is created running).

    The uploads are deleted as soon as the session exists, not when it ends:
    the session mounts its own copy, and whoever finishes it may be another
    process that never knew their ids. `metadata` goes on the session (at
    most 8 keys, values at most 512 characters) so the Console and whoever
    finishes it can see what it was for.
    """
    require_configured()
    cfg = settings()
    client = client or anthropic_client()

    resources, uploaded = file_resources(client, files)
    resources += memory_resources(memory_stores or {})
    create: dict = {
        "agent": agent_id,
        "environment_id": cfg.managed_environment_id,
        "title": title[:200],
        "initial_events": initial_events,
    }
    if resources:
        create["resources"] = resources
    budget = _budget()
    if budget:
        create["budget"] = budget
    if metadata:
        create["metadata"] = {str(k)[:64]: str(v)[:512]
                              for k, v in list(metadata.items())[:8]}
    _with_access(create, agent_id, access)
    try:
        session = client.beta.sessions.create(**create)
        audit.session(session.id)
    except BaseException:
        _release_access(client, access)
        raise
    finally:
        _delete_files(client, uploaded, "uploaded")
    if access is not None and access.ephemeral:
        # Whoever finishes the session may be another process; it finds the
        # vault by session id (close_session).
        from secondeye import dms_mcp

        dms_mcp.attach_session(access, session.id)
    log.info("detached session %s for %r: %s", session.id, title, console_url(session.id))
    return session.id


def _latest(client, session_id: str, kind: str):
    """The most recent event of one type, or None."""
    for event in client.beta.sessions.events.list(session_id=session_id, types=[kind],
                                                  order="desc", limit=1):
        return event
    return None


def _all(client, session_id: str, kind: str) -> list:
    return list(client.beta.sessions.events.list(session_id=session_id, types=[kind]))


def read_stop(client, session_id: str, status: str) -> Stopped:
    """Why a session that is no longer running stopped, and what it said.

    The session object has no stop_reason; the last `session.status_idle`
    event carries it. Errors and messages are read from the history too, so
    a refusal or a failure can be named the way the streaming path names it.
    """
    stopped = Stopped(session_id=session_id, status=status, stop="")
    idle = _latest(client, session_id, "session.status_idle")
    if idle is not None:
        stopped.idle_id = getattr(idle, "id", "") or ""
        stopped.stop = getattr(getattr(idle, "stop_reason", None), "type", "") or ""
    if status == "terminated":
        stopped.stop = "terminated"
    for event in _all(client, session_id, "session.error"):
        error = getattr(event, "error", None)
        stopped.errors.append(str(getattr(error, "message", None) or error))
    stopped.messages = [text for text in (_text_of(e) for e in
                                          _all(client, session_id, "agent.message")) if text]
    outcome = _latest(client, session_id, "span.outcome_evaluation_end")
    if outcome is not None:
        stopped.outcome = str(getattr(outcome, "result", "") or "")
    return stopped


def wait_until_stopped(client, session_id: str, *, timeout: float, after: str = "",
                       poll: float = POLL_SECONDS,
                       heartbeat: Callable[[], None] | None = None) -> Stopped:
    """Poll the session until it is no longer running, then read why it stopped.

    `after` is the id of an idle already seen, when we have just sent the
    session something: until a newer idle exists the session has not yet
    picked the message up, however its status reads. Raises StillRunning past
    `timeout` seconds. `heartbeat` is called on every poll.
    """
    beat = _safe(heartbeat)
    deadline = time.monotonic() + timeout
    while True:
        if beat is not None:
            beat()
        status = str(getattr(client.beta.sessions.retrieve(session_id), "status", "") or "")
        if status not in ACTIVE:
            stopped = read_stop(client, session_id, status)
            if not after or stopped.idle_id != after or status == "terminated":
                return stopped
        if time.monotonic() >= deadline:
            raise StillRunning(f"session {session_id} was still {status or 'running'} "
                               f"after {timeout:.0f}s")
        time.sleep(poll)


def send_message(client, session_id: str, text: str) -> None:
    client.beta.sessions.events.send(session_id=session_id, events=[
        {"type": "user.message", "content": [{"type": "text", "text": text}]}])


def interrupt(client, session_id: str) -> None:
    try:
        client.beta.sessions.events.send(session_id=session_id,
                                         events=[{"type": "user.interrupt"}])
    except Exception:  # noqa: BLE001 - the caller carries on to read what is there
        log.warning("could not interrupt session %s", session_id)


def close_session(client, session_id: str, still_running: bool = False) -> None:
    """Leave nothing behind: interrupt if it may still be running, delete what
    it wrote (after it has been downloaded) and delete it. As `_clean_up`,
    with no uploads to delete: `start_session` deleted those already."""
    _clean_up(client, session_id,
              SessionRun(session_id=session_id, stop="" if still_running else "closed"), [])
    from secondeye import dms_mcp

    if dms_mcp.any_dms_session_state():
        dms_mcp.release(client, session_id=session_id)


# --------------------------------------------------------------------------
# One-shot jobs for the associate: convert, repair, derive
# --------------------------------------------------------------------------


class JobFailed(RuntimeError):
    """The session did not produce what was asked. Always recoverable."""


def run_job(files: list[tuple[str, bytes]], instruction: str, title: str,
            heartbeat: Callable[[], None] | None = None, client=None) -> SessionRun:
    """Hand the associate agent some files and one instruction, and bring back
    what it wrote to the outputs directory. Used for the work that needs code
    against a file and nothing else: converting a legacy format, repairing
    formatting, building a derived spreadsheet."""
    if not configured():
        raise JobFailed("Managed Agents is not configured")
    cfg = settings()
    try:
        return run_session(
            agent_id=cfg.managed_associate_agent_id,
            title=title,
            initial_events=[{"type": "user.message",
                             "content": [{"type": "text", "text": instruction}]}],
            tools={},
            files=files,
            heartbeat=heartbeat,
            time_budget=cfg.agent_time_budget_seconds,
            client=client,
        )
    except JobFailed:
        raise
    except Exception as e:
        raise JobFailed(f"the session failed: {e}") from e


def convert_to_docx(content: bytes, filename: str) -> bytes:
    """Convert a format we cannot read into one we can, in the associate's
    sandbox. The point of this is the legacy `.doc` where LibreOffice is not
    installed: a lawyer whose template produced one should not be told to go
    and re-save it. The result is identified locally before it is used."""
    mounted = mount_name(filename)
    stem = mounted.rsplit(".", 1)[0]
    instruction = (
        f"The file is mounted at {WORKSPACE}/{mounted}. Convert it to .docx, preserving "
        "all text, headings, numbering, tables and formatting as faithfully as you "
        "can. Do not change any wording. Do not summarise. Write the result to "
        f"{OUTPUTS}/{stem}.docx and reply with its name. Nothing else."
    )
    run = run_job([(filename, content)], instruction, title=f"Convert: {filename}")
    produced = [data for name, data in run.outputs if name.lower().endswith(".docx")]
    if not produced:
        raise JobFailed("the session produced no .docx")
    log.info("converted %s to .docx in a session (%d bytes)", filename, len(produced[-1]))
    return produced[-1]


# --------------------------------------------------------------------------
# Agent definitions: agents/*.yaml applied once
# --------------------------------------------------------------------------


def load_manifest(path: Path) -> dict:
    """One agent manifest. `system_file` is read relative to the manifest and
    becomes `system`, so the prompt stays a Markdown file people can edit."""
    import yaml

    manifest = yaml.safe_load(path.read_text()) or {}
    system_file = manifest.pop("system_file", None)
    if system_file:
        manifest["system"] = (path.parent / system_file).read_text()
    return manifest


def agent_body(manifest: dict, custom_tools: list[dict]) -> dict:
    """The manifest as the API wants it, with the tools only code can define
    and the settings-driven parts (effort, geo, skills, web search) filled in."""
    cfg = settings()
    body = dict(manifest)
    model = body.get("model", cfg.review_model)
    if isinstance(model, str):
        model = {"id": model}
    model = dict(model)
    model.setdefault("id", cfg.review_model)
    if cfg.zero_retention:
        # ZERO_RETENTION: the manifest's model (Fable 5.1, a Covered Model)
        # would 400 on a ZDR organisation, so every agent goes on ZDR_MODEL.
        model["id"] = cfg.effective_model
    if "haiku" not in model["id"].lower():
        model["effort"] = cfg.agent_effort
    if cfg.inference_geo.strip():
        model["inference_geo"] = cfg.inference_geo.strip()
    body["model"] = model

    tools = list(body.get("tools") or [])
    for tool in tools:
        if tool.get("type") == "agent_toolset_20260401":
            configs = [c for c in tool.get("configs", [])
                       if c.get("name") not in ("web_search", "web_fetch")]
            configs += [{"name": "web_search", "enabled": cfg.web_search_enabled},
                        {"name": "web_fetch", "enabled": cfg.web_search_enabled}]
            tool["configs"] = configs
    # `document_system: true` in a manifest: this agent reads the firm's
    # document system, which is our MCP server (dms_mcp.py); each session
    # sets its matter-bound URL, so the one here is the bare endpoint.
    from secondeye import dms_mcp

    reads_dms = bool(body.pop("document_system", False))
    # Always sent, even empty. An update keeps any field it omits but
    # replaces `tools` whole, so an agent that once had the document system
    # would keep its server with no mcp_toolset naming it, which the API
    # rejects: every server must be referenced by a toolset.
    body["mcp_servers"] = [s for s in body.get("mcp_servers") or []
                           if s.get("name") != dms_mcp.SERVER_NAME]
    if dms_mcp.enabled() and reads_dms:
        body["mcp_servers"].append(dms_mcp.server_definition())
        tools.append(dms_mcp.toolset())
    body["tools"] = tools + custom_tools
    body["skills"] = skill_refs()
    # A skill only one agent needs (the closing tools), named in its manifest
    # by the setting that holds the id: `extra_skills: [sandbox_closing_skill_id]`.
    for setting in body.pop("extra_skills", None) or []:
        skill_id = str(getattr(cfg, setting, "") or "").strip()
        if skill_id:
            body["skills"].append({"type": "custom", "skill_id": skill_id, "version": "latest"})
    return body


def apply_agent(client, manifest_path: Path, existing_id: str, custom_tools: list[dict]):
    body = agent_body(load_manifest(manifest_path), custom_tools)
    if existing_id.strip():
        return client.beta.agents.update(existing_id.strip(), **body)
    return client.beta.agents.create(**body)


# The container's egress. Deny by default, with the public package registries
# allowed and nothing else: no allowed hosts, no MCP servers. Package managers
# stay on because the document skills import python-docx and lxml, which the
# container is not guaranteed to have, and Anthropic's own docx/xlsx/pdf skills
# install what they use at run time. That is a residual channel (a registry
# lookup names a package), accepted and recorded in docs/sandbox.md; turning
# networking off entirely would break the skills the review depends on.
NETWORKING = {"type": "limited", "allow_package_managers": True,
              "allow_mcp_servers": False, "allowed_hosts": []}


def networking() -> dict:
    """NETWORKING, with the agents' MCP servers reachable when the document
    system is configured. Under `limited` egress an
    MCP server not allowed here fails silently."""
    from secondeye import dms_mcp

    net = dict(NETWORKING)
    if dms_mcp.enabled():
        net["allow_mcp_servers"] = True
    return net


def _networking_of(env) -> dict:
    config = getattr(env, "config", None)
    net = getattr(config, "networking", None)
    if net is None:
        return {}
    if isinstance(net, dict):
        return dict(net)
    return {key: getattr(net, key, None) for key in NETWORKING}


def apply_environment(client, existing_id: str, name: str = "lra-review"):
    """The container template, made to match `NETWORKING`.

    A model running code against a privileged document should not be able to
    reach any host it likes; the web tools are governed separately, on the
    agent, because they run on Anthropic's servers and not in this container.

    An existing environment is read and, when its networking differs from
    ours, updated in place. Reading it and moving on meant an environment made
    by hand with unrestricted egress, or before this policy, kept that egress
    forever while `second-eye agents apply` reported success.
    """
    wanted_net = networking()
    config = {"type": "cloud", "networking": dict(wanted_net)}
    if existing_id.strip():
        env = client.beta.environments.retrieve(existing_id.strip())
        current = _networking_of(env)
        wanted = {k: (sorted(v) if isinstance(v, list) else v) for k, v in wanted_net.items()}
        have = {k: (sorted(v) if isinstance(v, list) else v) for k, v in current.items()}
        if have != wanted:
            log.warning("environment %s networking was %s; updating to %s",
                        env.id, current, wanted_net)
            env = client.beta.environments.update(existing_id.strip(), config=config)
        return env
    return client.beta.environments.create(name=name, config=config)


__all__ = [
    "AGENTS", "BETAS", "OUTPUTS", "WORKSPACE", "JobFailed", "NotConfigured", "SessionRun",
    "SessionTimedOut", "StillRunning", "Stopped",
    "agent_body", "apply_agent", "apply_environment", "close_session", "configured",
    "console_url", "interrupt", "load_manifest", "memory_resources", "outputs", "read_stop",
    "require_configured", "run_job", "run_session", "send_message", "skill_refs",
    "start_session", "wait_until_stopped",
]
