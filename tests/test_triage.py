"""Triage (triage.py, docs/migration.md phase 3): the model reads the email
and returns the plan; TRIAGE=shadow logs it beside the rules' plan, and
TRIAGE=model carries it out through the rules' own handlers.

No test reaches the API. `triage.call_model` is replaced by a function that
builds the plan from the evidence it is given, as the model would, and the
one test of the request itself replaces the client.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from secondeye import config, handler, triage
from secondeye.config import settings
from secondeye.models import Attachment, InboundEmail
from secondeye.store import connect
from tests.test_followup import (
    DOCX,
    TYPO,
    Captured,
    contract,
    first_email,
    reply_email,
    stub,
)


@pytest.fixture
def captured(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.sqlite3")
    monkeypatch.setenv("MAIL_AGENT_ADDRESS", "review@legal.firm.com")
    monkeypatch.setenv("FIRM_DOMAINS", "firm.com")
    settings.cache_clear()
    provider = Captured()
    monkeypatch.setattr(handler, "get_provider", lambda: provider)
    monkeypatch.setattr(handler.review, "review", stub(TYPO))
    yield provider
    settings.cache_clear()


def use(monkeypatch, mode: str, planner, **env):
    """Triage on, in `mode`, with `planner(evidence) -> Plan` as the model."""
    monkeypatch.setenv("TRIAGE", mode)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    settings.cache_clear()
    calls: list[dict] = []

    def model(facts):
        calls.append(facts)
        return planner(facts)

    monkeypatch.setattr(triage, "available", lambda: True)
    monkeypatch.setattr(triage, "call_model", model)
    return calls


def plan(*intents: str, **fields) -> triage.Plan:
    return triage.Plan(intents=[triage.Decision(intent=i, reason="test") for i in intents],
                       **fields)


def triage_rows() -> list[dict]:
    with connect() as c:
        rows = c.execute("SELECT triage FROM audit_log WHERE triage IS NOT NULL").fetchall()
    return [json.loads(r[0]) for r in rows]


def first_review(captured):
    """The first review runs with triage off, so the conversation exists."""
    handler.handle(first_email())
    assert len(captured.sent) == 1


# --- modes ---------------------------------------------------------------


def test_rules_mode_makes_no_call(captured, monkeypatch):
    calls = use(monkeypatch, "rules", lambda f: plan("help"))
    handler.handle(first_email())
    assert calls == []
    assert "Reciever" in captured.sent[0].text_body


def test_shadow_runs_triage_but_the_rules_decide_and_both_are_logged(captured, monkeypatch):
    first_review(captured)
    calls = use(monkeypatch, "shadow", lambda f: plan("help"))
    handler.handle(reply_email("30", "r2"))
    assert len(calls) == 1
    assert captured.sent[1].text_body.startswith("Noted: 30")     # the rules' answer
    [entry] = triage_rows()
    assert entry["mode"] == "shadow"
    assert entry["model"]["intents"] == ["help"]
    assert entry["rules"]["intents"] == ["answer"]
    assert entry["agree"] is False and entry["disagreements"]


def test_shadow_agreement_is_logged_as_such(captured, monkeypatch):
    first_review(captured)

    def answering(facts):
        [q] = [q for q in facts["thread"]["open_questions"] if "30" in q["options"]]
        return plan("answer", answers=[triage.Answer(question_id=q["id"], value="30")],
                    recipients=["jim@firm.com"])

    use(monkeypatch, "shadow", answering)
    handler.handle(reply_email("30", "r2"))
    [entry] = triage_rows()
    assert entry["agree"] is True, entry["disagreements"]


def test_the_audit_log_keeps_no_content(captured, monkeypatch):
    first_review(captured)
    use(monkeypatch, "shadow", lambda f: plan(
        "instruction", instruction="make the cap five million pounds", question="why?"))
    handler.handle(reply_email("make the cap five million pounds", "r2"))
    stored = json.dumps(triage_rows())
    assert "five million" not in stored and "why?" not in stored


def test_a_failed_triage_leaves_the_rules_to_decide(captured, monkeypatch):
    first_review(captured)

    def broken(facts):
        raise triage.Unavailable("declined")

    use(monkeypatch, "model", broken)
    handler.handle(reply_email("30", "r2"))
    assert captured.sent[1].text_body.startswith("Noted: 30")
    assert triage_rows()[0]["note"].startswith("triage failed")


# --- model mode: the plan is carried out by the rules' handlers ----------


def test_model_answers_by_question_id(captured, monkeypatch):
    first_review(captured)

    def answering(facts):
        [q] = [q for q in facts["thread"]["open_questions"] if "30" in q["options"]]
        return plan("answer", answers=[triage.Answer(question_id=q["id"], value="30")])

    use(monkeypatch, "model", answering)
    # Words no rule reads as an answer: the plan decides.
    handler.handle(reply_email("go with the higher of the two numbers", "r2"))
    out = captured.sent[1]
    assert out.text_body.startswith("Noted: 30")
    assert out.attachments


def test_model_undoes_by_change_number(captured, monkeypatch):
    first_review(captured)
    use(monkeypatch, "model", lambda f: plan("undo", undo=[1]))
    handler.handle(reply_email("accept the rest but not 1", "r2"))
    assert "I reversed change 1" in captured.sent[1].text_body


def test_an_undo_the_model_could_not_place_asks(captured, monkeypatch):
    first_review(captured)
    use(monkeypatch, "model", lambda f: plan("undo", undo=[]))
    handler.handle(reply_email("undo that", "r2"))
    assert captured.sent[1].text_body.startswith("Which one?")


def test_a_change_number_that_does_not_exist_asks(captured, monkeypatch):
    first_review(captured)
    use(monkeypatch, "model", lambda f: plan("undo", undo=[9]))
    handler.handle(reply_email("undo 9", "r2"))
    assert "I have no change 9" in captured.sent[1].text_body


def test_model_dismisses_by_letter(captured, monkeypatch):
    first_review(captured)

    def dismissing(facts):
        letter = facts["thread"]["findings"][0]["letter"]
        return plan("dismiss", dismiss=[letter.lower()])

    use(monkeypatch, "model", dismissing)
    handler.handle(reply_email("that first point is fine", "r2"))
    assert captured.sent[1].text_body.startswith("Noted:")


def test_model_acknowledgement_sends_nothing(captured, monkeypatch):
    first_review(captured)
    use(monkeypatch, "model", lambda f: plan("ack"))
    handler.handle(reply_email("cheers, all good", "r2"))
    assert len(captured.sent) == 1


def test_model_runs_a_whole_message_command(captured, monkeypatch):
    use(monkeypatch, "model", lambda f: plan("help"))
    handler.handle(InboundEmail(message_id="h1", from_address="jim@firm.com",
                                to=["review@legal.firm.com"], subject="?",
                                text_body="what else can you do for me?",
                                received_at=datetime.now(UTC)))
    assert "help" in captured.sent[0].subject.lower() or "reply" in captured.sent[0].text_body


def two_drafts() -> InboundEmail:
    raw = contract()
    return InboundEmail(
        message_id="two-1", from_address="jim@firm.com", to=["review@legal.firm.com"],
        subject="SPA", text_body="Their latest attached, please look at it",
        attachments=[Attachment(filename=n, content_type=DOCX, size_bytes=len(raw),
                                content=raw) for n in ("SPA v3.docx", "SPA v4.docx")],
        received_at=datetime.now(UTC))


def test_model_chooses_the_document_and_whose_paper_it_is(captured, monkeypatch):
    seen = {}
    monkeypatch.setattr(handler.review, "review",
                        lambda doc, mode, instructions, **kw: (
                            seen.setdefault("file", doc.filename), stub(TYPO)(
                                doc, mode, instructions, **kw))[1])
    use(monkeypatch, "model", lambda f: plan(
        "review", document="SPA v4.docx", their_paper=True,
        their_paper_reason="you said it is their latest"))
    handler.handle(two_drafts())
    assert seen["file"] == "SPA v4.docx"      # the rules refuse two drafts outright
    assert "I read this as their draft, because you said it is their latest" in \
        captured.sent[0].text_body


def test_a_document_triage_made_up_falls_back_to_intake(captured, monkeypatch):
    use(monkeypatch, "model", lambda f: plan("review", document="nothing.docx"))
    handler.handle(first_email())
    assert "Reciever" in captured.sent[0].text_body


def test_model_compares_against_the_baseline_it_named(captured, monkeypatch):
    got = {}

    def comparison(email, provider, pair=None, previous=None):
        got["pair"], got["previous"] = pair, previous
        return "compared"

    monkeypatch.setattr(handler.versions, "handle_comparison", comparison)
    use(monkeypatch, "model", lambda f: plan("blackline", baseline="SPA v4.docx"))
    handler.handle(two_drafts())
    earlier, later, how = got["pair"]
    assert (earlier.filename, later.filename) == ("SPA v4.docx", "SPA v3.docx")
    assert "as you said" in how


# --- what no plan changes ------------------------------------------------


def help_email(**kw) -> InboundEmail:
    return InboundEmail(message_id="h1", from_address="jim@firm.com",
                        to=["review@legal.firm.com"], cc=["ann@firm.com"], subject="?",
                        text_body="help", received_at=datetime.now(UTC), **kw)


def test_sender_only_readdresses_the_models_recipients(captured, monkeypatch):
    use(monkeypatch, "model", lambda f: plan("help", recipients=["ann@firm.com"]))
    handler.handle(help_email())
    assert captured.sent[0].to == ["jim@firm.com"] and captured.sent[0].cc == []


def test_reply_policy_model_honours_the_models_recipients(captured, monkeypatch):
    use(monkeypatch, "model", lambda f: plan("help", recipients=["ann@firm.com",
                                                                 "jim@firm.com"]),
        REPLY_POLICY="model")
    handler.handle(help_email())
    assert captured.sent[0].to == ["ann@firm.com"] and captured.sent[0].cc == ["jim@firm.com"]


def test_a_recipient_not_on_the_email_is_dropped(captured, monkeypatch):
    use(monkeypatch, "model", lambda f: plan("help", recipients=["mallory@evil.com"]),
        REPLY_POLICY="model")
    handler.handle(help_email())
    assert captured.sent[0].to == ["jim@firm.com"]


def test_no_ai_client_is_never_triaged(captured, monkeypatch):
    def trap(*a, **k):
        raise AssertionError("triage called the model for a no-AI client")

    calls = use(monkeypatch, "model", trap, NO_AI_MATTERS="acme.com")
    email = first_email().model_copy(update={"cc": ["legal@acme.com"]})
    handler.handle(email)
    assert calls == []
    assert "guidelines exclude AI" in captured.sent[0].text_body


def test_no_ai_party_in_the_attached_document_is_never_triaged(captured, monkeypatch):
    from tests.test_end_to_end import inbound

    calls = use(monkeypatch, "model", lambda f: plan("review"), NO_AI_MATTERS="Acme")
    monkeypatch.setattr(config, "anthropic_client",
                        lambda: (_ for _ in ()).throw(AssertionError("model client built")))
    handler.handle(inbound())       # its parties clause names Acme Holdings LLC
    assert calls == []
    [entry] = triage_rows()
    assert entry["note"] == "not triaged: no-AI client"


# --- the request and the plan --------------------------------------------


class FakeClient:
    def __init__(self, text: str, stop_reason: str = "end_turn"):
        self.kwargs: dict = {}
        self._response = SimpleNamespace(
            stop_reason=stop_reason,
            content=[SimpleNamespace(type="text", text=text)])
        from tests import api_contract as contract

        self.beta = contract.strict(SimpleNamespace(beta=SimpleNamespace(
            messages=SimpleNamespace(create=self._create)))).beta

    def _create(self, **kwargs):
        self.kwargs = kwargs
        return self._response


def model_json(**fields) -> str:
    base = {k: None for k in triage.SCHEMA["required"]}
    base.update(intents=[], document_reason="", review_mode="redline",
                their_paper_reason="", recipients=[], recipients_reason="", answers=[],
                undo=[], undo_all=False, dismiss=[], instruction="", question="",
                no_ai_client="", audit_since="", audit_until="")
    base.update(fields)
    return json.dumps(base)


def test_the_request_is_structured_output_on_fable_with_the_fallback(monkeypatch):
    monkeypatch.setenv("REVIEW_MODEL", "claude-fable-5-1")
    monkeypatch.setenv("ZERO_RETENTION", "false")
    settings.cache_clear()
    fake = FakeClient(model_json(intents=[{"intent": "help", "reason": "asked"}]))
    monkeypatch.setattr(config, "anthropic_client", lambda: fake)
    got = triage.call_model({"subject": "x"})
    assert got.names == ["help"]
    k = fake.kwargs
    assert k["model"] == "claude-fable-5-1"
    assert k["output_config"]["format"] == {"type": "json_schema", "schema": triage.SCHEMA}
    assert k["betas"] == [config.FALLBACK_BETA] and k["fallbacks"]
    assert "thinking" not in k and "tool_choice" not in k
    settings.cache_clear()


def test_a_refusal_is_not_a_plan(monkeypatch):
    monkeypatch.setattr(config, "anthropic_client",
                        lambda: FakeClient("", stop_reason="refusal"))
    with pytest.raises(triage.Unavailable):
        triage.call_model({})


def test_an_intent_outside_the_command_set_is_dropped():
    got = triage.parse(model_json(intents=[{"intent": "wire the money", "reason": ""},
                                           {"intent": "review", "reason": ""}]))
    assert got.names == ["review"]


def test_the_schema_names_every_field_the_plan_reads():
    fields = set(triage.Plan.model_fields) - {"source"}
    assert set(triage.SCHEMA["required"]) == fields
    assert set(triage.SCHEMA["properties"]) == fields


def test_a_blackline_goes_to_the_blackline_agent_when_it_is_set_up(captured, monkeypatch):
    called = []
    monkeypatch.setattr(handler.blackline, "configured", lambda: True)
    monkeypatch.setattr(handler.blackline, "handle",
                        lambda job_id, email, provider: called.append(email.message_id))
    first_review(captured)
    use(monkeypatch, "model", lambda f: plan("blackline", baseline="previous"))
    handler.handle(reply_email("can I have that against what we sent on Tuesday", "r2"))
    assert called == ["r2"]
