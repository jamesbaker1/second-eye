"""No-AI clients (policy.py): some clients' guidelines forbid putting their
documents through a model. For them, no model call at all: the deterministic
checks, and the reply says why in one line.

Every model entry point is booby-trapped here, and the agents are configured,
so a review that reaches any of them fails the test rather than quietly
passing because nothing was set up.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime

import pytest

from secondeye import config, crypto, handler, managed, policy, sessions_api, thread
from secondeye.config import settings
from secondeye.models import InboundEmail
from secondeye.pipeline import instruct
from tests.fake_sessions import configure
from tests.fake_sessions_api import FakeSessionsApi
from tests.test_cloudflare import cloud  # noqa: F401  (the fixture)
from tests.test_end_to_end import JUDGMENT, TYPO, Captured, inbound, stub_agent

LINE = "Mechanical checks only: Acme's guidelines exclude AI."


class ModelCalled(AssertionError):
    pass


# Every trap sprung, kept as well as raised: a caller that swallows the
# exception (the comparison's assessment never raises) must still fail the test.
_sprung: list[str] = []


def _trap(name):
    def called(*args, **kwargs):
        _sprung.append(name)
        raise ModelCalled(f"{name} was called for a no-AI client")
    return called


@pytest.fixture
def traps(monkeypatch):
    """Every way to a model, set to fail the test if reached."""
    monkeypatch.setattr(handler.review, "review", _trap("review.review"))
    monkeypatch.setattr(handler.review, "start", _trap("review.start"), raising=False)
    monkeypatch.setattr(instruct, "carry_out", _trap("instruct.carry_out"))
    monkeypatch.setattr(managed, "run_session", _trap("managed.run_session"))
    monkeypatch.setattr(managed, "start_session", _trap("managed.start_session"))
    monkeypatch.setattr(managed, "run_job", _trap("managed.run_job"))
    monkeypatch.setattr(managed, "anthropic_client", _trap("anthropic_client"))
    monkeypatch.setattr(config, "anthropic_client", _trap("anthropic_client"))
    fake = FakeSessionsApi()
    previous = sessions_api.use(fake)
    _sprung.clear()
    yield fake
    sessions_api.use(previous)
    assert fake.started == [], "a review session was started for a no-AI client"
    assert _sprung == [], f"model entry points reached: {_sprung}"


@pytest.fixture
def captured(monkeypatch, tmp_path):
    configure(monkeypatch)          # the agents are set up: only the policy stops them
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/no_ai.sqlite3")
    monkeypatch.setenv("MAIL_AGENT_ADDRESS", "review@legal.firm.com")
    monkeypatch.setenv("FIRM_DOMAINS", "firm.com")
    monkeypatch.setenv("PLAYBOOK_ADMINS", "gc@firm.com")
    monkeypatch.setattr(handler, "SLOW_NOTICE_AFTER", 0.0)
    settings.cache_clear()
    provider = Captured()
    monkeypatch.setattr(handler, "get_provider", lambda: provider)
    yield provider
    settings.cache_clear()


def _no_ai(monkeypatch, value):
    monkeypatch.setenv("NO_AI_MATTERS", value)
    settings.cache_clear()


def _command(text, sender="gc@firm.com", mid="cmd-1") -> InboundEmail:
    return InboundEmail(message_id=mid, from_address=sender, to=["review@legal.firm.com"],
                        subject="Clients", text_body=text, received_at=datetime.now(UTC))


def _body(out) -> str:
    return out.text_body + out.html_body


def test_a_party_named_in_the_document_means_no_model_call(captured, traps, monkeypatch):
    _no_ai(monkeypatch, "Acme")
    handler.handle(inbound())                     # the NDA's parties clause names Acme Holdings LLC
    [out] = captured.sent
    assert LINE in out.text_body and html.escape(LINE) in out.html_body
    # The deterministic checks still ran: "thirty (13)" is theirs to find.
    assert "13" in _body(out)


def test_the_clients_domain_on_the_message_is_enough(captured, traps, monkeypatch):
    _no_ai(monkeypatch, "acme.com")
    email = inbound().model_copy(update={"cc": ["legal@eu.acme.com"]})
    handler.handle(email)
    assert "Mechanical checks only: acme.com's guidelines exclude AI." in captured.sent[-1].text_body


def test_the_matter_is_enough(captured, traps, monkeypatch):
    _no_ai(monkeypatch, "M-4471")
    email = inbound().model_copy(update={"subject": "Matter no. M-4471 NDA"})
    handler.handle(email)
    assert "M-4471's guidelines exclude AI" in captured.sent[-1].text_body


def test_everyone_else_still_gets_the_model(captured, monkeypatch):
    _no_ai(monkeypatch, "Globex")
    monkeypatch.setattr(handler.review, "review", stub_agent(TYPO, JUDGMENT))
    handler.handle(inbound())
    assert "guidelines exclude AI" not in captured.sent[-1].text_body


def test_an_admin_adds_and_lifts_a_client_by_email(captured, traps, monkeypatch):
    handler.handle(_command("no AI for Acme"))
    assert captured.sent[-1].text_body.startswith("Done. Nothing for Acme goes to a model")
    handler.handle(inbound())
    assert LINE in captured.sent[-1].text_body

    handler.handle(_command("AI ok for Acme", mid="cmd-2"))
    assert captured.sent[-1].text_body.startswith("Done. Acme is no longer excluded")
    # The model again, for this review: the stand-in, and no memory stores.
    monkeypatch.setattr(handler.review, "review", stub_agent(TYPO, JUDGMENT))
    monkeypatch.setattr(handler.memory, "stores_for", lambda *a, **k: {})
    handler.handle(inbound().model_copy(update={"message_id": "e2e-2"}))
    assert "guidelines exclude AI" not in captured.sent[-1].text_body


def test_only_an_admin_can_change_the_list(captured, traps):
    handler.handle(_command("no AI for Acme", sender="jim@firm.com"))
    assert "Only the firm's playbook admins" in captured.sent[-1].text_body
    assert policy.excluded(parties=["Acme Holdings LLC"]) is None


def test_a_configured_entry_cannot_be_lifted_by_email(captured, traps, monkeypatch):
    _no_ai(monkeypatch, "Acme")
    handler.handle(_command("AI ok for Acme"))
    assert "stays excluded" in captured.sent[-1].text_body
    assert policy.excluded(parties=["Acme Holdings LLC"]) == "Acme"


def test_an_instruction_on_a_no_ai_thread_is_declined_not_attempted(captured, traps, monkeypatch):
    _no_ai(monkeypatch, "Acme")
    handler.handle(inbound())
    reply = InboundEmail(message_id="e2e-reply", in_reply_to="e2e-1", references=["e2e-1"],
                         from_address="jim@firm.com", to=["review@legal.firm.com"],
                         subject="Re: Acme / Beta NDA",
                         text_body="Tighten the confidentiality clause.",
                         received_at=datetime.now(UTC))
    assert thread.resolve("jim@firm.com", None, "e2e-reply", "e2e-1", ["e2e-1"])
    handler.handle(reply)
    assert captured.sent[-1].text_body.startswith(LINE)
    assert "can't carry out a free-form instruction" in captured.sent[-1].text_body


def test_the_guard_fails_closed_for_a_call_nobody_routed_around():
    with policy.ai_scope():
        policy.forbid_ai("Acme")
        assert not managed.configured()
        with pytest.raises(policy.AIForbidden):
            config.anthropic_client()
        with pytest.raises(policy.AIForbidden):
            managed.require_configured()
    # And the next message starts clean.
    assert policy.ai_forbidden() is None


def test_the_workflow_sends_the_checks_from_prepare_and_starts_no_session(
        cloud, traps, monkeypatch):  # noqa: F811
    from fastapi.testclient import TestClient

    from secondeye import main
    from tests.test_workflow_steps import AUTH, JOB, raw_review

    configure(monkeypatch, NO_AI_MATTERS="Acme")
    client = TestClient(main.app, raise_server_exceptions=False)
    response = client.post("/prepare", content=crypto.seal(raw_review()),
                           headers={**AUTH, "x-job-id": JOB})
    assert response.json() == {"status": "done"}
    [sent] = cloud.sent
    assert LINE in sent["text"]
    assert sent["to"] == ["jim@firm.com"]
