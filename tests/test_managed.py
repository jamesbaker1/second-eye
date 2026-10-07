"""Driving a Managed Agents session: the behaviour we own on our side of the stream.

The platform runs the loop. What is ours is answering custom tools, keeping
the time budget, surviving a dropped stream without deadlocking the session,
and bringing the outputs back. All of it against the scripted fake.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from lra import managed
from tests import fake_sessions as fs


@pytest.fixture
def configured(monkeypatch):
    fs.configure(monkeypatch)
    # No real waiting: the reconnect backoff and the outputs indexing lag.
    monkeypatch.setattr(managed.time, "sleep", lambda s: None)
    yield
    from lra.config import settings

    settings.cache_clear()


def drive(fake: fs.FakeSessions, tools=None, **kw) -> managed.SessionRun:
    kw.setdefault("agent_id", "agent_review")
    kw.setdefault("title", "t")
    kw.setdefault("initial_events", [{"type": "user.message",
                                      "content": [{"type": "text", "text": "go"}]}])
    return managed.run_session(tools=tools or {}, client=fake.client, **kw)


# --- it refuses to run half-configured --------------------------------------

def test_nothing_runs_without_the_agent_ids(monkeypatch):
    fs.configure(monkeypatch, MANAGED_REVIEW_AGENT_ID="")
    try:
        assert managed.configured() is False
        with pytest.raises(managed.NotConfigured, match="MANAGED_REVIEW_AGENT_ID"):
            managed.run_session(agent_id="x", title="t", initial_events=[], tools={})
    finally:
        from lra.config import settings

        settings.cache_clear()


# --- custom tools are answered over the stream ------------------------------

def test_a_custom_tool_call_is_answered_and_the_session_continues(configured):
    fake = fs.FakeSessions([
        fs.running(),
        fs.tool_use("lookup", query="cap"),
        fs.idle("requires_action"),
        fs.after_tool_result(fs.message("The cap is $1m."), fs.idle()),
    ])
    run = drive(fake, tools={"lookup": lambda inp: f"found {inp['query']}"})
    result = fake.sent[0][0]
    assert result["type"] == "user.custom_tool_result"
    assert result["content"][0]["text"] == "found cap"
    assert "is_error" not in result
    assert run.messages == ["The cap is $1m."]
    assert run.stop == "end_turn"


def test_a_tool_that_raises_becomes_an_error_result_the_model_can_read(configured):
    def broken(inp):
        raise ValueError("the DMS is down")

    fake = fs.FakeSessions([fs.tool_use("lookup"), fs.after_tool_result(fs.idle())])
    drive(fake, tools={"lookup": broken})
    result = fake.sent[0][0]
    assert result["is_error"] is True
    assert "the DMS is down" in result["content"][0]["text"]


def test_an_unknown_tool_is_refused_not_crashed(configured):
    fake = fs.FakeSessions([fs.tool_use("nonsense"), fs.after_tool_result(fs.idle())])
    drive(fake)
    assert fake.sent[0][0]["is_error"] is True


def test_a_requires_action_idle_does_not_end_the_session(configured):
    """The session goes idle while it waits for our tool result. Breaking on
    that idle is the deadlock the docs warn about."""
    fake = fs.FakeSessions([
        fs.tool_use("a"), fs.idle("requires_action"),
        fs.after_tool_result(fs.tool_use("b"), fs.idle("requires_action"),
                             fs.after_tool_result(fs.idle())),
    ])
    calls = []
    drive(fake, tools={"a": lambda i: calls.append("a") or "ok",
                       "b": lambda i: calls.append("b") or "ok"})
    assert calls == ["a", "b"]


# --- the heartbeat -----------------------------------------------------------

def test_the_heartbeat_is_called_on_every_event(configured):
    fake = fs.FakeSessions([fs.running(), fs.message("x"), fs.idle()])
    beats = []
    drive(fake, heartbeat=lambda: beats.append(1))
    assert len(beats) == 3


# --- a dropped stream is reconnected without losing or repeating anything ----

def test_a_dropped_stream_is_reconnected_and_history_is_deduped(configured):
    fake = fs.FakeSessions([
        fs.running(), fs.message("first"), fs.message("second"), fs.idle(),
    ], drop_after=2)
    with patch.object(managed.time, "sleep", lambda s: None):
        run = drive(fake)
    assert fake.stream_opens == 2
    # "first" was in the history on reconnect; it must appear once, not twice.
    assert run.messages == ["first", "second"]


def test_a_tool_call_pending_across_a_drop_is_still_answered_once(configured):
    fake = fs.FakeSessions([
        fs.tool_use("lookup"), fs.after_tool_result(fs.idle()),
    ], drop_after=1)
    with patch.object(managed.time, "sleep", lambda s: None):
        drive(fake, tools={"lookup": lambda i: "ok"})
    results = [e for batch in fake.sent for e in batch if e["type"] == "user.custom_tool_result"]
    assert len(results) == 1


def test_a_stream_that_keeps_dropping_fails_the_job(configured):
    class Always(fs.FakeSessions):
        def _stream(self, session_id, **kw):
            self.stream_opens += 1
            import anthropic
            import httpx2

            class S:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def __iter__(self):
                    raise anthropic.APIConnectionError(
                        request=httpx2.Request("GET", "http://x"), message="down")
                    yield

            return S()

    fake = Always([])
    with patch.object(managed.time, "sleep", lambda s: None), \
         pytest.raises(RuntimeError, match="giving up"):
        drive(fake)
    assert fake.stream_opens == managed.MAX_RECONNECTS + 1


# --- the time budget ----------------------------------------------------------

def out_of_time():
    """The deadline is set normally; every check after that lands past it."""
    ticks = iter([0.0] + [10**9] * 200)
    return patch.object(managed.time, "monotonic", lambda: next(ticks))


def test_past_the_budget_the_session_is_interrupted_and_asked_to_report(configured):
    fake = fs.FakeSessions([
        fs.running(),
        fs.Step(lambda sent: [fs.idle(), fs.running(), fs.model_turn_end(),
                              fs.message("what I have"), fs.idle()]),
    ])
    with out_of_time():
        run = drive(fake, time_budget=300, report_now="REPORT NOW", reported=lambda: False)
    assert [e["type"] for e in fake.sent[0]] == ["user.interrupt", "user.message"]
    assert fake.sent[0][1]["content"][0]["text"] == "REPORT NOW"
    assert run.cut_short is True
    assert run.messages == ["what I have"]


def test_the_idle_after_the_interrupt_does_not_end_the_session(configured):
    """An interrupt forces an idle. Our "report now" message is queued behind
    it; breaking there would read the interrupt as the answer."""
    fake = fs.FakeSessions([
        fs.running(),
        fs.Step(lambda sent: [fs.idle("end_turn"), fs.running(), fs.model_turn_end(),
                              fs.message("late report"), fs.idle()]),
    ])
    with out_of_time():
        run = drive(fake, time_budget=300, report_now="now", reported=lambda: False)
    assert run.messages == ["late report"]
    assert run.stop == "end_turn"


def test_a_report_in_hand_at_the_deadline_is_kept_and_no_more_turns_run(configured):
    fake = fs.FakeSessions([fs.running(), fs.Step(lambda sent: [fs.idle()])])
    with out_of_time():
        run = drive(fake, time_budget=300, report_now="now", reported=lambda: True)
    assert [e["type"] for e in fake.sent[0]] == ["user.interrupt"]
    assert run.cut_short is False


def test_a_budget_pause_stops_the_session(configured):
    fake = fs.FakeSessions([fs.running(), fs.idle("budget_reached")])
    run = drive(fake)
    assert run.stop == "budget_reached"


def test_the_session_is_created_with_a_dollar_cap(configured):
    fake = fs.FakeSessions([fs.idle()])
    drive(fake)
    budget = fake.created[0]["budget"]
    assert budget["type"] == "limit"
    assert budget["max_list_cost"]["currency"] == "USD"
    assert budget["max_list_cost"]["amount"].isdigit()


# --- files in, files out -------------------------------------------------------

def test_files_are_uploaded_mounted_under_workspace_and_deleted_afterwards(configured):
    fake = fs.FakeSessions([fs.idle()])
    drive(fake, files=[("Acme SPA.docx", b"PK..")])
    resource = fake.created[0]["resources"][0]
    assert resource == {"type": "file", "file_id": "file_1",
                        "mount_path": "/workspace/Acme SPA.docx"}
    assert fake.deleted == ["file_1"]


def test_two_files_go_up_together_and_mount_in_order(configured):
    """A PDF goes up with its Word copy; neither waits for the other."""
    import threading

    fake = fs.FakeSessions([fs.idle()])
    both_in_flight = threading.Barrier(2, timeout=5)
    real_upload = fake.upload

    def upload(file, **kw):
        both_in_flight.wait()      # deadlocks (and times out) if sequential
        return real_upload(file, **kw)

    fake.upload = upload
    drive(fake, files=[("scan.pdf", b"%PDF"), ("scan.docx", b"PK")])
    paths = [r["mount_path"] for r in fake.created[0]["resources"]]
    assert paths == ["/workspace/scan.pdf", "/workspace/scan.docx"]
    ids = [r["file_id"] for r in fake.created[0]["resources"]]
    assert sorted(fake.deleted) == sorted(ids)


def test_a_failed_upload_leaves_the_other_file_deleted_and_no_session(configured):
    fake = fs.FakeSessions([fs.idle()])
    real_upload = fake.upload

    def upload(file, **kw):
        if file[0].endswith(".pdf"):
            raise RuntimeError("upload refused")
        return real_upload(file, **kw)

    fake.upload = upload
    with pytest.raises(RuntimeError, match="upload refused"):
        drive(fake, files=[("scan.pdf", b"%PDF"), ("scan.docx", b"PK")])
    assert fake.created == []
    assert fake.deleted == ["file_1"]


def test_a_hostile_filename_cannot_escape_the_workspace(configured):
    fake = fs.FakeSessions([fs.idle()])
    drive(fake, files=[("../../etc/passwd", b"x")])
    path = fake.created[0]["resources"][0]["mount_path"]
    assert path.startswith("/workspace/") and ".." not in path and path.count("/") == 2


def test_outputs_are_listed_by_session_and_downloaded(configured):
    fake = fs.FakeSessions([fs.idle()], outputs=[("a (redline).docx", b"PK-out")])
    run = drive(fake)
    assert run.outputs == [("a (redline).docx", b"PK-out")]


def test_an_empty_outputs_listing_is_retried_for_the_indexing_lag(configured):
    fake = fs.FakeSessions([])
    calls = {"n": 0}

    def listing(**kw):
        calls["n"] += 1
        return []

    fake.list = listing
    waited = []
    assert managed.outputs(fake.client, "sesn_1", attempts=3, sleep=waited.append) == []
    assert calls["n"] == 3 and len(waited) == 2


def test_memory_stores_are_mounted_read_only(configured):
    fake = fs.FakeSessions([fs.idle()])
    drive(fake, memory_stores={"firm": "memstore_f", "personal": "memstore_p", "matter": ""})
    stores = [r for r in fake.created[0]["resources"] if r["type"] == "memory_store"]
    assert [s["memory_store_id"] for s in stores] == ["memstore_f", "memstore_p"]
    assert all(s["access"] == "read_only" for s in stores)


# --- the trace URL ---------------------------------------------------------------

def test_the_console_url_names_the_workspace(monkeypatch):
    fs.configure(monkeypatch, ANTHROPIC_WORKSPACE_ID="wrkspc_01X")
    try:
        assert managed.console_url("sesn_1") == \
            "https://platform.claude.com/workspaces/wrkspc_01X/sessions/sesn_1"
    finally:
        from lra.config import settings

        settings.cache_clear()


# --- agent definitions from the manifests -----------------------------------------

def test_the_manifests_load_and_carry_the_prompt_files():
    for name in ("review", "associate"):
        manifest = managed.load_manifest(managed.AGENTS / f"{name}.agent.yaml")
        assert manifest["name"].startswith("LRA")
        assert "system" in manifest and len(manifest["system"]) > 500
        assert "system_file" not in manifest


def test_the_agent_body_carries_effort_skills_and_the_custom_tools(monkeypatch):
    fs.configure(monkeypatch, SANDBOX_SKILL_ID="skill_tools", AGENT_EFFORT="xhigh",
                 WEB_SEARCH_ENABLED="false")
    try:
        body = managed.agent_body(
            managed.load_manifest(managed.AGENTS / "associate.agent.yaml"),
            [{"type": "custom", "name": "make_changes", "input_schema": {}}],
        )
    finally:
        from lra.config import settings

        settings.cache_clear()
    assert body["model"]["effort"] == "xhigh"
    assert body["tools"][0]["type"] == "agent_toolset_20260401"
    assert body["tools"][-1]["name"] == "make_changes"
    web = {c["name"]: c["enabled"] for c in body["tools"][0]["configs"]}
    assert web == {"web_search": False, "web_fetch": False}
    ids = [s["skill_id"] for s in body["skills"]]
    assert ids == ["docx", "xlsx", "pdf", "pptx", "skill_tools"]


def test_web_search_is_a_decision_made_in_settings(monkeypatch):
    fs.configure(monkeypatch, WEB_SEARCH_ENABLED="true")
    try:
        body = managed.agent_body(
            managed.load_manifest(managed.AGENTS / "review.agent.yaml"), [])
    finally:
        from lra.config import settings

        settings.cache_clear()
    assert all(c["enabled"] for c in body["tools"][0]["configs"]
               if c["name"] in ("web_search", "web_fetch"))


def test_the_rubric_is_a_file_people_can_edit():
    text = (managed.AGENTS / "review_rubric.md").read_text()
    criteria = [line for line in text.splitlines() if line[:2].rstrip(".").isdigit()]
    assert 6 <= len(criteria) <= 9
    assert "validate_findings.py" in text and "verbatim" in text and "safe to send" in text
    assert "report_findings" not in text


def test_the_reviewer_is_applied_with_no_custom_tools(monkeypatch):
    """A custom tool call leaves a session idle until someone answers it. A
    review session has nobody listening, so it would wait for ever."""
    fs.configure(monkeypatch, SANDBOX_SKILL_ID="skill_tools")
    try:
        manifest = managed.load_manifest(managed.AGENTS / "review.agent.yaml")
        body = managed.agent_body(manifest, [])
    finally:
        from lra.config import settings

        settings.cache_clear()
    assert not [t for t in body["tools"] if t.get("type") != "agent_toolset_20260401"]
    toolset = body["tools"][0]
    assert toolset["default_config"]["permission_policy"]["type"] == "always_allow", \
        "a tool that asks for confirmation stalls a clientless session the same way"


def test_the_reviewer_prompt_has_files_for_tools():
    """The report and the notes are files: nothing in the prompt names a
    custom tool the reviewer does not have."""
    prompt = (managed.PROMPTS / "review_system.md").read_text()
    for tool in ("report_findings", "note_for_next_time"):
        assert tool not in prompt, f"{tool} is a custom tool the reviewer does not have"
    assert "validate_findings.py" in prompt and "notes.json" in prompt
    assert "--findings /mnt/session/outputs/findings.json" in prompt


def _criteria(name: str) -> dict[str, str]:
    import re

    text = (managed.AGENTS / name).read_text()
    return {m.group(1): m.group(2) for m in re.finditer(
        r"^(\d+)\. (.*?)(?=^\d+\. |\Z)", text, re.MULTILINE | re.DOTALL)}


def test_the_rubric_judges_the_report_and_the_notes_by_the_files():
    criteria = _criteria("review_rubric.md")
    assert "validate_findings.py" in criteria["1"]
    assert "findings.json" in criteria["7"]
    assert "notes.json" in criteria["9"]


# --- the environment's egress is made to match, not just read ---------------

def _env_client(networking: dict | None):
    from types import SimpleNamespace as NS

    calls: dict = {}

    def retrieve(env_id):
        calls["retrieve"] = env_id
        return NS(id=env_id, name="lra-review",
                  config=NS(networking=NS(**networking) if networking else None))

    def update(env_id, **kw):
        calls["update"] = (env_id, kw)
        return NS(id=env_id, name="lra-review")

    def create(**kw):
        calls["create"] = kw
        return NS(id="env_new", name=kw["name"])

    from tests import api_contract as contract

    return (contract.strict(NS(beta=NS(environments=NS(retrieve=retrieve, update=update,
                                                        create=create)))), calls)


def test_an_existing_environment_with_open_egress_is_brought_into_line():
    client, calls = _env_client({"type": "unrestricted"})
    managed.apply_environment(client, "env_1")
    env_id, kw = calls["update"]
    assert env_id == "env_1"
    assert kw["config"]["networking"] == managed.NETWORKING


def test_an_environment_that_already_matches_is_left_alone():
    client, calls = _env_client(dict(managed.NETWORKING))
    managed.apply_environment(client, "env_1")
    assert "update" not in calls


def test_a_new_environment_allows_no_hosts_beyond_package_registries():
    client, calls = _env_client(None)
    managed.apply_environment(client, "")
    net = calls["create"]["config"]["networking"]
    assert net["type"] == "limited" and net["allowed_hosts"] == []
    assert net["allow_mcp_servers"] is False


# --- nothing is left behind, and nothing outlives its deadline ---------------

def at(*times):
    """time.monotonic returning these values, then the last one forever."""
    values = list(times)

    def tick():
        return values.pop(0) if len(values) > 1 else values[0]

    return patch.object(managed.time, "monotonic", tick)


def test_a_failing_heartbeat_does_not_end_the_review(configured):
    """On Cloudflare the heartbeat writes through the edge; one refused write
    used to propagate out of the event loop and abort a review going fine."""
    from lra.edge import EdgeError

    def beat():
        raise EdgeError("edge said 503")

    fake = fs.FakeSessions([fs.running(), fs.message("done"), fs.idle()])
    run = drive(fake, heartbeat=beat)
    assert run.messages == ["done"] and run.stop == "end_turn"


def test_outputs_are_deleted_once_downloaded_and_the_session_deleted(configured):
    fake = fs.FakeSessions([fs.idle()], outputs=[("Schedule.xlsx", b"PK-out")])
    run = drive(fake, files=[("a.docx", b"PK..")])
    assert run.outputs == [("Schedule.xlsx", b"PK-out")]
    assert fake.deleted == ["file_1", "out_0"]
    assert fake.deleted_sessions == ["sesn_fake"]
    # It stopped on its own, so there is nothing to interrupt.
    assert not any(e["type"] == "user.interrupt" for batch in fake.sent for e in batch)


def test_a_session_that_fails_is_interrupted_and_cleaned_up(configured):
    fake = fs.FakeSessions([fs.running()], outputs=[("half.docx", b"PK")])
    with pytest.raises(RuntimeError, match="giving up"):
        drive(fake, files=[("a.docx", b"PK..")])
    assert fake.sent[-1] == [{"type": "user.interrupt"}]
    assert "file_1" in fake.deleted and "out_0" in fake.deleted
    assert fake.deleted_sessions == ["sesn_fake"]


def test_a_replayed_idle_after_report_now_does_not_end_the_session(configured):
    """The idle our interrupt causes is skipped when it arrives. After a drop,
    the history replays it; with a later turn already counted, it used to be
    read as the end, and the report that followed was never waited for."""
    fake = fs.FakeSessions([
        fs.running(),
        fs.Step(lambda sent: [fs.idle("end_turn"), fs.running(), fs.model_turn_end(),
                              fs.message("the late report"), fs.idle()]),
    ], drop_after=4)
    # Past the 300s budget, inside the grace and the hard deadline.
    with at(0.0, 400.0):
        run = drive(fake, time_budget=300, report_now="now", reported=lambda: False)
    assert fake.stream_opens == 2
    assert run.messages == ["the late report"]
    assert run.stop == "end_turn"


class Quiet(fs.FakeSessions):
    """A stream that times out reading `quiet_for` times, then plays the script."""

    def __init__(self, script, quiet_for):
        super().__init__(script)
        self.quiet_for = quiet_for
        self.timeout = None

    def _stream(self, session_id, **kw):
        self.timeout = kw.get("timeout")
        if not self.quiet_for:
            return super()._stream(session_id, **kw)
        self.quiet_for -= 1
        self.stream_opens += 1
        import anthropic
        import httpx2

        class S:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def __iter__(self):
                raise anthropic.APITimeoutError(request=httpx2.Request("GET", "http://x"))
                yield

        return S()


def test_a_quiet_stream_is_not_a_dropped_one(configured):
    """A long tool run emits nothing. Read timeouts are waited through without
    using up the reconnects a flapping connection is allowed."""
    fake = Quiet([fs.message("done"), fs.idle()], quiet_for=managed.MAX_RECONNECTS + 2)
    run = drive(fake)
    assert run.stop == "end_turn"
    assert fake.timeout == managed.STREAM_READ_TIMEOUT


def test_a_session_past_its_hard_deadline_is_abandoned_and_cleaned_up(configured):
    fake = Quiet([fs.idle()], quiet_for=100)
    with at(0.0, 10.0**9), pytest.raises(managed.SessionTimedOut):
        drive(fake, time_budget=300, files=[("a.docx", b"PK..")])
    assert fake.sent[-1] == [{"type": "user.interrupt"}]
    assert "file_1" in fake.deleted
    assert fake.deleted_sessions == ["sesn_fake"]


def test_the_app_starts_without_loading_the_sdk():
    """A container asleep for ten minutes is woken by the next email, and the
    app's import is on that email's clock. The SDK was over half of it, and
    "clean copy" or "undo 2" never calls the model."""
    import os
    import subprocess
    import sys

    probe = "import sys, lra.handler; print('anthropic' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                         check=True, env=dict(os.environ))
    assert out.stdout.strip() == "False"
