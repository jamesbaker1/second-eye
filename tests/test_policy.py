"""Firm policy (policy.py): rules no composer and no model decision can change.

REPLY_POLICY=sender_only is a rule a firm's general counsel signs off on
(DECISIONS 31), so the tests hold it where mail leaves, not where replies are
written: a composer that addresses the counterparty is overruled, and no path
out of the application reaches a provider without passing the policy.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from secondeye import flow, handler, policy
from secondeye.config import settings
from secondeye.models import OutboundEmail
from secondeye.pipeline import reply
from tests.test_cloudflare import cloud  # noqa: F401  (the fixture)
from tests.test_end_to_end import JUDGMENT, TYPO, Captured, inbound, stub_agent

SRC = Path(__file__).resolve().parents[1] / "src" / "secondeye"


@pytest.fixture
def captured(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/policy.sqlite3")
    monkeypatch.setenv("MAIL_AGENT_ADDRESS", "review@legal.firm.com")
    monkeypatch.setenv("FIRM_DOMAINS", "firm.com")
    settings.cache_clear()
    provider = Captured()
    monkeypatch.setattr(handler, "get_provider", lambda: provider)
    yield provider
    settings.cache_clear()


def _readdressed(monkeypatch):
    """A composer (or, under DECISIONS 30, a model) that puts the other side
    on the reply."""
    real = reply.compose

    def compose(*args, **kwargs):
        out = real(*args, **kwargs)
        out.to = ["opp@counterparty.com"]
        out.cc = ["client@acme.com"]
        return out

    monkeypatch.setattr(reply, "compose", compose)


def test_sender_only_is_the_default():
    assert settings().reply_policy == "sender_only"


def test_a_review_goes_to_the_sender_alone_whoever_the_composer_chose(captured, monkeypatch):
    _readdressed(monkeypatch)
    monkeypatch.setattr(handler.review, "review", stub_agent(TYPO, JUDGMENT))
    handler.handle(inbound())
    assert captured.sent, "nothing was sent"
    for out in captured.sent:
        assert out.to == ["jim@firm.com"]
        assert out.cc == []


def test_under_the_model_policy_the_recipients_stand(captured, monkeypatch):
    monkeypatch.setenv("REPLY_POLICY", "model")
    settings.cache_clear()
    _readdressed(monkeypatch)
    monkeypatch.setattr(handler.review, "review", stub_agent(TYPO, JUDGMENT))
    handler.handle(inbound())
    assert captured.sent[-1].to == ["opp@counterparty.com"]
    assert captured.sent[-1].cc == ["client@acme.com"]


def test_a_bad_policy_value_refuses_to_boot(monkeypatch):
    monkeypatch.setenv("REPLY_POLICY", "anyone")
    settings.cache_clear()
    with pytest.raises(ValueError):
        settings()
    settings.cache_clear()


def test_the_workflow_stores_replies_as_policy_lets_them_leave():
    out = OutboundEmail(to=["opp@counterparty.com"], cc=["x@acme.com"], subject="Re: NDA",
                        text_body="The review.")
    body = flow._payload(out, inbound())
    assert body["to"] == ["jim@firm.com"]
    assert body["cc"] == []


def test_no_sender_is_refused_rather_than_sent_anywhere():
    out = OutboundEmail(to=["opp@counterparty.com"], subject="x", text_body="x")
    with pytest.raises(policy.PolicyViolation):
        policy.apply(out, "")


def test_every_way_out_passes_the_policy():
    """Structural, because the next send path will be written by someone who
    has not read this file. A provider is resolved in four places: two wrap
    it in policy.Policed at once, main.py only verifies and parses with it,
    and the factory itself. The Workflow's stored replies are built in one
    function, which applies the policy."""
    resolved = {}
    for path in SRC.rglob("*.py"):
        text = path.read_text()
        if "get_provider()" in text:
            resolved[path.relative_to(SRC).as_posix()] = text
    assert set(resolved) == {"handler.py", "flow.py", "main.py", "mail/__init__.py"}
    # audit.Watch sits inside the policy, so the audit trail records what
    # actually left (audit.py); the policy is still the outermost layer.
    assert ("policy.Policed(audit.Watch(provider or get_provider(), job_id), email)"
            in resolved["handler.py"])
    assert "Outboxed(policy.Policed(audit.Watch(provider, job_id), email)" in resolved["flow.py"]
    assert ".send(" not in resolved["main.py"]
    calls = re.findall(r"(?<![_\w])payload\(", resolved["flow.py"])
    assert len(calls) == 1, "flow.py builds a stored reply without _payload"
    assert "return payload(policy.apply(out, email.from_address))" in resolved["flow.py"]


# --- the privilege notice ----------------------------------------------------

NOTICE = "Privileged & Confidential — Attorney Work Product"


def test_every_reply_ends_with_the_notice_and_leads_with_the_verdict(captured, monkeypatch):
    monkeypatch.setattr(handler.review, "review", stub_agent(TYPO, JUDGMENT))
    handler.handle(inbound())
    [out] = captured.sent
    # The first line is still what a phone previews: the verdict.
    assert out.text_body.startswith("Don't send yet")
    assert out.text_body.rstrip("\n").endswith("\n\n" + NOTICE)
    assert out.html_body.endswith(
        '<p style="margin:1.5em 0 0;font-size:11px;color:#888">'
        "Privileged &amp; Confidential — Attorney Work Product</p>")
    # ASCII, so every provider carries it without encoding it.
    assert out.headers["X-Privileged"] == "Privileged & Confidential - Attorney Work Product"


def test_the_notice_is_applied_once_however_often_policy_runs():
    out = OutboundEmail(to=["jim@firm.com"], subject="x", text_body="Done.\n",
                        html_body="<div>Done.</div>")
    twice = policy.apply(policy.apply(out, "jim@firm.com"), "jim@firm.com")
    assert twice.text_body.count(NOTICE) == 1
    assert twice.html_body.count("Attorney Work Product") == 1
    assert out.text_body == "Done.\n", "the composer's message was changed in place"


def test_an_empty_notice_sends_none(monkeypatch):
    monkeypatch.setenv("PRIVILEGE_NOTICE", "")
    settings.cache_clear()
    out = policy.apply(OutboundEmail(to=["jim@firm.com"], subject="x", text_body="Done.\n"),
                       "jim@firm.com")
    assert out.text_body == "Done.\n" and out.headers == {}
    settings.cache_clear()


def test_every_provider_sends_the_header(monkeypatch):
    import json

    import httpx
    import respx

    from secondeye.mail.agentmail import AgentMailProvider
    from secondeye.mail.cloudflare import payload
    from secondeye.mail.postmark import PostmarkProvider

    monkeypatch.setenv("AGENTMAIL_API_KEY", "k")
    monkeypatch.setenv("AGENTMAIL_INBOX_ID", "inbox1")
    monkeypatch.setenv("POSTMARK_SERVER_TOKEN", "t")
    settings.cache_clear()
    out = policy.apply(OutboundEmail(to=["jim@firm.com"], subject="Re: NDA", text_body="x",
                                     in_reply_to="<m1@firm.com>"), "jim@firm.com")
    headers = payload(out)["headers"]
    assert headers["X-Privileged"] and headers["In-Reply-To"] == "<m1@firm.com>"

    with respx.mock:
        route = respx.post("https://api.postmarkapp.com/email").mock(
            return_value=httpx.Response(200, json={"MessageID": "out"}))
        PostmarkProvider().send(out)
        names = [h["Name"] for h in json.loads(route.calls[0].request.content)["Headers"]]
        assert names == ["X-Privileged", "In-Reply-To", "References"]

        route = respx.post(re.compile(r"https://api\.agentmail\.to/.*")).mock(
            return_value=httpx.Response(200, json={"message_id": "out"}))
        AgentMailProvider().send(out)
        assert "X-Privileged" in json.loads(route.calls[0].request.content)["headers"]
    settings.cache_clear()


# --- the kill switch -----------------------------------------------------------


def _pause(monkeypatch, on=True):
    monkeypatch.setenv("SERVICE_PAUSED", "true" if on else "false")
    settings.cache_clear()


def test_paused_nothing_is_reviewed_or_sent_and_the_message_is_not_used_up(
        captured, monkeypatch):
    monkeypatch.setattr(handler.review, "review", stub_agent(TYPO, JUDGMENT))
    _pause(monkeypatch)
    handler.handle(inbound())
    assert captured.sent == []
    # Not claimed: the same message, delivered again once the switch is off,
    # is reviewed.
    _pause(monkeypatch, on=False)
    handler.handle(inbound())
    assert len(captured.sent) == 1


def test_paused_mid_message_the_send_is_dropped(monkeypatch):
    sent: list = []

    class Recorder:
        def send(self, out, **kwargs):
            sent.append(out)
            return "id"

    _pause(monkeypatch)
    out = OutboundEmail(to=["jim@firm.com"], subject="x", text_body="x")
    assert policy.Policed(Recorder(), inbound()).send(out) == ""
    assert sent == []
    settings.cache_clear()


def test_paused_the_application_asks_for_redelivery(cloud, monkeypatch):  # noqa: F811
    from fastapi.testclient import TestClient

    from secondeye import crypto, main
    from tests.test_workflow_steps import AUTH, raw_review

    _pause(monkeypatch)
    client = TestClient(main.app, raise_server_exceptions=False)
    step = client.post("/prepare", content=crypto.seal(raw_review()),
                       headers={**AUTH, "x-job-id": "rf-paused"})
    assert step.status_code == 503 and step.json()["detail"] == "paused"
    # A stranger learns nothing: authentication is checked first.
    assert client.post("/prepare", content=b"x",
                       headers={"x-job-id": "rf-paused"}).status_code == 401
    assert cloud.sent == []
