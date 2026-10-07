"""The fakes are held to the real API's contract, and so is everything we send.

tests/api_contract.py checks every call through a fake client against the
installed SDK's typed params and response models, plus the documented rules
the types cannot express. These tests prove the checker refuses what the
real API would refuse (so a green suite means something), and run every
request this codebase builds for Anthropic through it directly: the agent
definitions, the environment, the review session, skills sync, the memory
stores, and the webhook payloads the Worker reads.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from anthropic.types.beta.beta_webhook_event import BetaWebhookEvent
from anthropic.types.beta.sessions.beta_managed_agents_session_event import (
    BetaManagedAgentsSessionEvent,
)

from lra import managed
from tests import api_contract as contract
from tests import fake_sessions as fs

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def configured(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/contract.sqlite3")
    fs.configure(monkeypatch)
    yield
    from lra.config import settings

    settings.cache_clear()


def _refused(call, match: str):
    with pytest.raises(contract.ContractError, match=match):
        call()


# --- the checker refuses what the API refuses ----------------------------------

def test_an_unknown_field_in_a_sent_event_is_refused():
    fake = fs.FakeSessions([])
    _refused(lambda: fake.client.beta.sessions.events.send(
        session_id="sesn_1", events=[{"type": "user.interrupt", "reason": "late"}]),
        r"events\[0\]\.reason: not a field")


def test_a_wrong_enum_is_refused():
    fake = fs.FakeSessions([])
    _refused(lambda: fake.client.beta.sessions.create(
        agent="agent_1", environment_id="env_1",
        budget={"type": "limit", "max_list_cost": {"amount": "500", "currency": "usd"}}),
        "currency")


def test_a_missing_required_field_is_refused():
    fake = fs.FakeSessions([])
    _refused(lambda: fake.client.beta.sessions.create(
        agent="agent_1", environment_id="env_1",
        initial_events=[{"type": "user.define_outcome", "description": "Review it."}]),
        "rubric: required")


def test_an_argument_the_sdk_does_not_take_is_refused():
    fake = fs.FakeSessions([])
    _refused(lambda: fake.client.beta.files.list(scope_id="sesn_1", purpose="outputs"),
             "unexpected keyword")


def test_a_method_the_sdk_does_not_have_is_refused():
    fake = fs.FakeSessions([])
    _refused(lambda: fake.client.beta.sessions.events.replay, "no such resource or method")


def test_listing_outputs_without_the_managed_agents_header_is_refused():
    fake = fs.FakeSessions([])
    _refused(lambda: list(fake.client.beta.files.list(scope_id="sesn_1")), "scope_id needs")


def test_a_budget_in_dollars_is_refused():
    fake = fs.FakeSessions([])
    _refused(lambda: fake.client.beta.sessions.create(
        agent="agent_1", environment_id="env_1",
        budget={"type": "limit", "max_list_cost": {"amount": "25.00", "currency": "USD"}}),
        "integer string of cents")


def test_the_managed_agents_header_on_a_memory_store_call_is_refused():
    client = contract.strict(NS(beta=NS(memory_stores=NS(create=lambda **kw: {"id": "m"}))))
    _refused(lambda: client.beta.memory_stores.create(
        name="x", betas=[contract.MANAGED_AGENTS_BETA]), "is a 400")


def test_an_event_the_api_never_sends_cannot_reach_our_code():
    with pytest.raises(contract.ContractError, match="api_error"):
        fs.event("session.error", error={"type": "api_error", "message": "x"})
    with pytest.raises(contract.ContractError, match="not one of"):
        fs.event("session.status_idle", stop_reason={"type": "interrupted"})
    with pytest.raises(contract.ContractError, match="never returns"):
        fs.outcome_end("pass")


def test_what_the_fake_answers_is_the_sdks_own_object():
    fake = fs.DetachedSessions([fs.Turn(fs.idle())])
    session = fake.client.beta.sessions.create(agent="agent_1", environment_id="env_1")
    assert type(session).__name__ == "BetaManagedAgentsSession"
    with pytest.raises(AttributeError):
        session.stop_reason      # noqa: B018 - the real session has no stop_reason


def test_archiving_a_running_session_is_a_400_as_live():
    import anthropic

    fake = fs.DetachedSessions([fs.Turn(fs.idle(), polls=5)])
    fake.client.beta.sessions.create(agent="agent_1", environment_id="env_1",
                                     initial_events=[{"type": "user.message", "content": [
                                         {"type": "text", "text": "go"}]}])
    with pytest.raises(anthropic.BadRequestError):
        fake.client.beta.sessions.archive("sesn_fake")


def test_a_session_at_its_budget_refuses_a_message_as_live():
    import anthropic

    fake = fs.DetachedSessions([fs.Turn(fs.idle("budget_reached"), polls=0)])
    fake.client.beta.sessions.create(agent="agent_1", environment_id="env_1")
    fake.client.beta.sessions.retrieve("sesn_fake")
    with pytest.raises(anthropic.BadRequestError):
        managed.send_message(fake.client, "sesn_fake", "one more thing")


# --- everything we build passes ------------------------------------------------

def _agents_client(created: list, updated: list):
    def create(**body):
        created.append(body)
        return {"id": f"agent_{len(created)}", "name": body["name"], "version": 1}

    def update(agent_id, **body):
        updated.append((agent_id, body))
        return {"id": agent_id, "name": body.get("name", "x"), "version": 2}

    return contract.strict(NS(beta=NS(agents=NS(create=create, update=update))))


@pytest.mark.parametrize("dms", ["none", "imanage"])
def test_every_agent_definition_is_a_valid_create_and_update(configured, monkeypatch, dms):
    from lra import closing, playbook
    from lra.config import settings
    from lra.pipeline import instruct

    monkeypatch.setenv("DMS_PROVIDER", dms)
    monkeypatch.setenv("DMS_MCP_URL", "https://dms.example.com/mcp")
    monkeypatch.setenv("SANDBOX_SKILL_ID", "skill_tools")
    monkeypatch.setenv("SANDBOX_CLOSING_SKILL_ID", "skill_closing")
    settings.cache_clear()
    tools = {"associate.agent.yaml": instruct.CUSTOM_TOOLS,
             "playbook.agent.yaml": playbook.CUSTOM_TOOLS,
             "closing.agent.yaml": closing.CUSTOM_TOOLS}
    created: list = []
    updated: list = []
    client = _agents_client(created, updated)
    for manifest in sorted(managed.AGENTS.glob("*.agent.yaml")):
        custom = tools.get(manifest.name, [])
        managed.apply_agent(client, manifest, "", custom)
        managed.apply_agent(client, manifest, "agent_existing", custom)
    assert len(created) == len(updated) == len(list(managed.AGENTS.glob("*.agent.yaml")))


def test_the_environment_is_a_valid_create_and_update(configured):
    calls: list = []
    client = contract.strict(NS(beta=NS(environments=NS(
        create=lambda **kw: calls.append(("create", kw)) or {"id": "env_1", "name": kw["name"]},
        retrieve=lambda env_id, **kw: {"id": env_id, "name": "lra-review",
                                       "config": {"type": "cloud",
                                                  "networking": {"type": "unrestricted"}}},
        update=lambda env_id, **kw: calls.append(("update", kw)) or {"id": env_id}))))
    managed.apply_environment(client, "")
    managed.apply_environment(client, "env_1")
    assert [c[0] for c in calls] == ["create", "update"]


def test_a_review_session_is_a_valid_create(configured, monkeypatch):
    from lra.models import Mode
    from lra.pipeline import review

    monkeypatch.setenv("MANAGED_SESSION_BUDGET_CENTS", "4000")
    from lra.config import settings

    settings.cache_clear()
    from tests.test_detached import a_document

    doc, raw = a_document("NDA.docx")
    fake = fs.DetachedSessions([fs.Turn(fs.idle())])
    review.start(doc, Mode.REDLINE, "look at clause 4", original=raw,
                 memory_stores={"firm": "memstore_firm", "matter": "memstore_m"},
                 client=fake.client)
    [create] = fake.created
    assert create["budget"]["max_list_cost"] == {"amount": "4000", "currency": "USD"}
    [outcome] = create["initial_events"]
    assert outcome["type"] == "user.define_outcome"


def test_skills_sync_uploads_a_valid_skill(configured, monkeypatch):
    import lra.config
    from lra import skillsync

    calls: list = []
    client = contract.strict(NS(skills=NS(
        create=lambda **kw: calls.append(kw) or {"id": "skill_1", "latest_version_id": "v1",
                                                 "display_name": kw["display_name"]},
        versions=NS(create=lambda skill_id, **kw: calls.append(kw) or {"id": "v2",
                                                                       "skill_id": skill_id}))))
    monkeypatch.setattr(lra.config, "anthropic_client", lambda: client)
    for name in skillsync.CATALOGUE:
        assert skillsync.sync("", name) == ("skill_1", "v1")
    assert skillsync.sync("skill_1", "lra-playbook") == ("skill_1", "v2")
    assert len(calls) == len(skillsync.CATALOGUE) + 1


def test_the_webhook_payloads_the_worker_reads_are_ones_the_api_sends():
    """cloudflare/src/webhook.ts reads `id`, `data.type`, `data.id` and, for a
    lapsed credential, `data.vault_id`. Each payload its tests send must parse
    as the SDK's webhook event, so the Worker is tested on the real shape."""
    from anthropic._models import construct_type

    source = (ROOT / "cloudflare" / "test" / "webhook.test.ts").read_text()
    assert '"session.status_idled"' in source and '"vault_credential.refresh_failed"' in source
    for data in (
        {"type": "session.status_idled", "id": "sesn_1", "organization_id": "org",
         "workspace_id": "ws"},
        {"type": "session.status_terminated", "id": "sesn_1", "organization_id": "org",
         "workspace_id": "ws"},
        {"type": "vault_credential.refresh_failed", "id": "vcrd_1", "vault_id": "vlt_1",
         "organization_id": "org", "workspace_id": "ws"},
    ):
        payload = {"type": "event", "id": "whe_1", "created_at": "2026-10-01T09:00:00Z",
                   "data": data}
        contract.require(payload, BetaWebhookEvent, "a webhook", response=True)
        event = construct_type(type_=BetaWebhookEvent, value=json.loads(json.dumps(payload)))
        assert event.data.type == data["type"] and event.data.id == data["id"]
    # And the types the Worker wakes on exist as webhook types at all.
    webhook = (ROOT / "cloudflare" / "src" / "webhook.ts").read_text()
    for kind in ("session.status_idled", "session.status_terminated",
                 "vault_credential.refresh_failed"):
        assert kind in webhook


def test_every_event_kind_managed_reads_exists_in_the_sdk():
    """The event types managed.py switches on, each a member of the union."""
    import re

    source = (ROOT / "src" / "lra" / "managed.py").read_text()
    kinds = set(re.findall(r'"((?:agent|session|span|user)\.[a-z_]+)"', source))
    known = {t for arm in BetaManagedAgentsSessionEvent.__args__[0].__args__
             for t in arm.model_fields["type"].annotation.__args__}
    assert kinds and kinds <= known, kinds - known
