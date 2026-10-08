"""The model, and the Claude Fable 5.1 rules every call to it has to follow.

Jim chose Claude Fable 5.1 on 2026-09-28. Three things about it break code
written for earlier models: thinking is always on and any other thinking
setting is a 400, a forced tool_choice is a 400, and a safety classifier can
decline a request with an HTTP 200 whose stop_reason is "refusal" and whose
content is empty or partial. The two places this codebase calls the Messages
API directly (transcribing a scan, assessing a comparison) are held to all
three here, against a stub: the account has no credit to try them for real.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

import secondeye.config
from secondeye.config import FALLBACK_BETA, FALLBACK_MODEL, refusal_fallback, settings
from secondeye.pipeline import compare, extract
from secondeye.pipeline.extract import Block, ExtractedDoc
from tests import api_contract as contract

ROOT = Path(__file__).resolve().parents[1]
MODEL = "claude-fable-5-1"


@pytest.fixture(autouse=True)
def fable(monkeypatch):
    monkeypatch.setenv("REVIEW_MODEL", MODEL)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    settings.cache_clear()
    yield
    settings.cache_clear()


# --- one model, named the same everywhere --------------------------------------

def test_every_place_that_names_the_model_names_fable():
    import yaml

    assert secondeye.config.Settings.model_fields["review_model"].default == MODEL
    for manifest in sorted((ROOT / "agents").glob("*.agent.yaml")):
        assert yaml.safe_load(manifest.read_text())["model"] == MODEL, manifest.name
    raw = (ROOT / "cloudflare" / "wrangler.jsonc").read_text()
    config = json.loads(re.sub(r"^\s*//.*$", "", raw, flags=re.MULTILINE))
    assert config["vars"]["REVIEW_MODEL"] == MODEL


def test_nothing_sends_a_thinking_setting_or_forces_a_tool():
    """Both are a 400 on Fable 5.1. Thinking is adaptive when unset."""
    for path in (ROOT / "src").rglob("*.py"):
        text = path.read_text()
        assert not re.search(r"\bthinking\s*=|[\"']thinking[\"']\s*:", text), path
        assert "budget_tokens" not in text, path
        assert not re.search(r"tool_choice", text), path


def test_the_fallback_is_opted_into_for_fable_and_nothing_else():
    assert refusal_fallback(MODEL) == {"betas": [FALLBACK_BETA],
                                       "fallbacks": [{"model": FALLBACK_MODEL}]}
    assert FALLBACK_MODEL in ("claude-opus-4-8", "claude-opus-5"), \
        "the only targets the API accepts from Fable 5.1"
    assert refusal_fallback("claude-haiku-4-5") == {}


# --- assessing a comparison -----------------------------------------------------

def _comparison() -> tuple[compare.Comparison, ExtractedDoc]:
    later = ExtractedDoc(blocks=[Block(0, "The term is sixty days.", "Normal", "paragraph")],
                         docx=None, filename="v2.docx")
    return compare.Comparison(content=None, changes=[
        compare.Change(kind="replaced", where="clause 1", before="thirty", after="sixty")
    ]), later


def _parsing_client(calls: list, stop_reason: str = "end_turn"):
    def parse(**kwargs):
        calls.append(kwargs)
        parsed = compare._Assessment(summary="One change.", changes=[
            compare._Assessed(index=0, risk="medium", impact="Longer term.")])
        return NS(stop_reason=stop_reason,
                  parsed_output=None if stop_reason == "refusal" else parsed)

    return contract.strict(NS(beta=NS(messages=NS(parse=parse))))


def test_the_assessment_asks_for_the_fallback_and_sends_no_thinking(monkeypatch):
    calls: list = []
    monkeypatch.setattr(secondeye.config, "anthropic_client", lambda: _parsing_client(calls))
    comparison, later = _comparison()
    compare._assess(comparison, later, "", "")
    [call] = calls
    assert call["model"] == MODEL
    assert call["fallbacks"] == [{"model": FALLBACK_MODEL}]
    assert call["betas"] == [FALLBACK_BETA]
    assert "thinking" not in call and "tool_choice" not in call
    assert comparison.changes[0].risk == "medium"


def test_a_refused_assessment_leaves_the_changes_unassessed(monkeypatch):
    calls: list = []
    monkeypatch.setattr(secondeye.config, "anthropic_client",
                        lambda: _parsing_client(calls, "refusal"))
    comparison, later = _comparison()
    compare.explain(comparison, later)
    assert comparison.changes[0].risk == "" and comparison.summary == ""


# --- transcribing a scan ----------------------------------------------------------

def _streaming_client(calls: list, message):
    class Stream:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            return message

    def stream(**kwargs):
        calls.append(kwargs)
        return Stream()

    return contract.strict(NS(beta=NS(messages=NS(stream=stream))))


def _text(text: str) -> NS:
    return NS(type="text", text=text)


def test_the_transcription_asks_for_the_fallback_and_sends_no_thinking(monkeypatch):
    calls: list = []
    message = NS(stop_reason="end_turn", content=[
        NS(type="fallback", **{"from": NS(model=MODEL)}, to=NS(model=FALLBACK_MODEL)),
        _text("=== PAGE 1 ===\nThe term is thirty days."),
    ])
    monkeypatch.setattr(secondeye.config, "anthropic_client",
                        lambda: _streaming_client(calls, message))
    pages = extract._transcribe(b"%PDF-", 1)
    assert pages == ["The term is thirty days."], "a fallback block is not text"
    [call] = calls
    assert call["fallbacks"] == [{"model": FALLBACK_MODEL}]
    assert call["output_config"] == {"effort": "low"}
    assert "thinking" not in call


def test_a_transcription_refused_part_way_is_not_used_as_the_whole_scan(monkeypatch):
    """A classifier can stop the stream after some pages. That text is not
    the document, and a review built on it would miss the rest."""
    message = NS(stop_reason="refusal", content=[_text("=== PAGE 1 ===\nThe term")])
    monkeypatch.setattr(secondeye.config, "anthropic_client",
                        lambda: _streaming_client([], message))
    with pytest.raises(RuntimeError, match="declined"):
        extract._transcribe(b"%PDF-", 2)


# --- ZERO_RETENTION ----------------------------------------------------------------

def test_zero_retention_is_off_and_names_the_model_available_under_zdr():
    """Fable 5.1 requires 30-day retention (a 400 for a ZDR organisation);
    Claude Opus 5 is the most capable model documented as available under ZDR."""
    assert settings().zero_retention is False
    assert settings().effective_model == MODEL
    assert secondeye.config.ZDR_MODEL == "claude-opus-5"


def test_zero_retention_puts_every_agent_and_every_call_on_the_zdr_model(monkeypatch):
    from secondeye import managed

    monkeypatch.setenv("ZERO_RETENTION", "true")
    settings.cache_clear()
    for manifest in sorted((ROOT / "agents").glob("*.agent.yaml")):
        body = managed.agent_body(managed.load_manifest(manifest), [])
        assert body["model"]["id"] == "claude-opus-5", manifest.name

    # `second-eye agents apply` sends exactly that body.
    created: list = []
    client = contract.strict(NS(beta=NS(agents=NS(
        create=lambda **body: created.append(body) or NS(id="a")))))
    managed.apply_agent(client, ROOT / "agents" / "review.agent.yaml", "", [])
    assert created[0]["model"]["id"] == "claude-opus-5"

    calls: list = []
    monkeypatch.setattr(secondeye.config, "anthropic_client", lambda: _parsing_client(calls))
    comparison, later = _comparison()
    compare._assess(comparison, later, "", "")
    [call] = calls
    assert call["model"] == "claude-opus-5"
    # Opus 5 has no server-side refusal fallback to opt into.
    assert "fallbacks" not in call and "betas" not in call
