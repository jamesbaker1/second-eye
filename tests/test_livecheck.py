"""`lra live-check`: the first live run in one command (src/lra/livecheck.py).

Nothing here reaches the API. The preflight is held to the errors the API
documents for the three ways the first run is expected to fail (no credit,
retention not enabled, a wrong model id), built as the SDK raises them; the
steps after it are replaced by fakes, except the agents step, which runs
`lra agents apply` against the same fake client tests/test_cli.py uses, so
that the ids it writes into wrangler.jsonc are the ids apply printed.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from types import SimpleNamespace as NS

import anthropic
import httpx2
import pytest

import lra.config
from lra import livecheck
from lra.config import settings
from tests import api_contract as contract
from tests import fake_sessions as fs

ROOT = Path(__file__).resolve().parents[1]
URL = "https://api.anthropic.com/v1/messages"

NO_CREDIT = ("Your credit balance is too low to access the Anthropic API. Please go to "
             "Plans & Billing to upgrade or purchase credits.")
NO_RETENTION = ("In order to access this model, your organization or workspace must have "
                "data retention enabled.")
BAD_MODEL = "model: claude-fable-5-1"


def api_error(cls, status: int, kind: str, message: str):
    response = httpx2.Response(status, request=httpx2.Request("POST", URL))
    return cls(message, response=response,
               body={"type": "error", "error": {"type": kind, "message": message}})


@pytest.fixture
def where(tmp_path, monkeypatch):
    """A report root, a copy of the real wrangler.jsonc, and an .env, all in
    tmp; the API key set and no agent ids, so nothing from a developer's
    .env leaks in."""
    wrangler = tmp_path / "wrangler.jsonc"
    shutil.copyfile(ROOT / "cloudflare" / "wrangler.jsonc", wrangler)
    env = tmp_path / ".env"
    env.write_text("# local settings\nANTHROPIC_API_KEY=sk-test\nMANAGED_REVIEW_AGENT_ID=\n")
    fs.configure(monkeypatch, MANAGED_REVIEW_AGENT_ID="", MANAGED_ASSOCIATE_AGENT_ID="",
                 MANAGED_ENVIRONMENT_ID="", MANAGED_FIRM_MEMORY_STORE_ID="",
                 SANDBOX_SKILL_ID="", REVIEW_MODEL="claude-fable-5-1", ZERO_RETENTION="false")
    yield NS(root=tmp_path / "reports", wrangler=wrangler, env=env)
    settings.cache_clear()


def main(where, *argv, steps=None) -> int:
    return livecheck.main(list(argv), root=where.root, wrangler=where.wrangler,
                          env_file=where.env, steps=steps)


def report(where) -> str:
    [out] = sorted(where.root.iterdir())
    return (out / "REPORT.md").read_text()


class Recorder:
    """Fake steps: each records that it ran and returns what it is told to."""

    def __init__(self, fail: str = "", spend: float = 0.0) -> None:
        self.ran: list[str] = []
        self.fail = fail
        self.spend = spend

    def steps(self, **real) -> dict:
        def make(name):
            def step(ctx):
                self.ran.append(name)
                print(f"{name} says hello")
                if name == self.fail:
                    return livecheck.StepResult(False, f"{name} broke", f"fix {name}",
                                                spent_usd=self.spend)
                return livecheck.StepResult(True, f"{name} fine", spent_usd=self.spend)
            return step
        return {name: real.get(name) or make(name) for name in livecheck.STEP_NAMES}


# --- the preflight: which of the expected failures it was --------------------------


@pytest.mark.parametrize("error, kind, says", [
    (api_error(anthropic.BadRequestError, 400, "invalid_request_error", NO_CREDIT),
     "credit", "no credit"),
    (api_error(anthropic.APIStatusError, 402, "billing_error", "Payment required"),
     "credit", "no credit"),
    (api_error(anthropic.BadRequestError, 400, "invalid_request_error", NO_RETENTION),
     "retention", "30-day data retention"),
    (api_error(anthropic.NotFoundError, 404, "not_found_error", BAD_MODEL),
     "model", "does not exist or this organisation cannot use it"),
    (api_error(anthropic.AuthenticationError, 401, "authentication_error",
               "invalid x-api-key"), "key", "API key was refused"),
])
def test_the_preflight_names_the_failure_from_the_api_error(error, kind, says):
    got, what, fix = livecheck.classify(error, "claude-fable-5-1")
    assert got == kind
    assert says in what
    assert fix


def test_a_retention_failure_says_how_to_turn_it_on_or_step_down():
    error = api_error(anthropic.BadRequestError, 400, "invalid_request_error", NO_RETENTION)
    _, what, fix = livecheck.classify(error, "claude-fable-5-1")
    assert "claude-fable-5-1" in what
    assert "30-day data retention" in fix and "ZERO_RETENTION=true" in fix


@pytest.mark.parametrize("message, says", [
    (NO_CREDIT, "Add credit"),
    (NO_RETENTION, "Turn on 30-day data retention"),
    (BAD_MODEL, "Check REVIEW_MODEL"),
])
def test_a_failed_preflight_stops_the_run_and_the_report_says_what_to_do(
        where, monkeypatch, capsys, message, says):
    cls, status, kind = {
        NO_CREDIT: (anthropic.BadRequestError, 400, "invalid_request_error"),
        NO_RETENTION: (anthropic.BadRequestError, 400, "invalid_request_error"),
        BAD_MODEL: (anthropic.NotFoundError, 404, "not_found_error"),
    }[message]
    calls = []

    def create(**kw):
        calls.append(kw)
        raise api_error(cls, status, kind, message)

    monkeypatch.setattr(lra.config, "anthropic_client",
                        lambda: contract.strict(NS(messages=NS(create=create))))
    rec = Recorder()
    assert main(where, steps=rec.steps(preflight=livecheck.step_preflight)) == 1
    assert calls and calls[0]["model"] == "claude-fable-5-1" and calls[0]["max_tokens"] <= 64
    assert rec.ran == [], "nothing after a failed preflight runs"
    out = capsys.readouterr().out
    assert "live-check stopped at preflight" in out and says in out
    text = report(where)
    assert "| preflight | FAIL |" in text and "| skills | not run |" in text
    assert says in text and "lra live-check --from preflight" in text


def test_the_preflight_without_a_key_makes_no_call(where, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    settings.cache_clear()
    monkeypatch.setattr(lra.config, "anthropic_client",
                        lambda: (_ for _ in ()).throw(AssertionError("a client was built")))
    rec = Recorder()
    assert main(where, steps=rec.steps(preflight=livecheck.step_preflight)) == 1
    assert "ANTHROPIC_API_KEY is not set" in report(where)


def test_a_preflight_that_answers_passes(where, monkeypatch):
    reply = NS(model="claude-fable-5-1", stop_reason="end_turn",
               usage=NS(input_tokens=14, output_tokens=40))
    monkeypatch.setattr(lra.config, "anthropic_client",
                        lambda: contract.strict(NS(messages=NS(create=lambda **kw: reply))))
    rec = Recorder()
    assert main(where, steps=rec.steps(preflight=livecheck.step_preflight)) == 0
    assert rec.ran == ["skills", "agents", "eval", "triage", "rehearse"]
    assert "| preflight | pass | $0.05 |" in report(where)


# --- stopping, and resuming ----------------------------------------------------------


def test_the_run_stops_at_the_first_failure(where, capsys):
    rec = Recorder(fail="agents")
    assert main(where, steps=rec.steps()) == 1
    assert rec.ran == ["preflight", "skills", "agents"]
    out = capsys.readouterr().out
    assert "live-check stopped at agents: agents broke" in out
    assert "What to do: fix agents" in out and "lra live-check --from agents" in out
    text = report(where)
    for name in ("preflight", "skills"):
        assert f"| {name} | pass |" in text
    assert "| agents | FAIL |" in text and "| eval | not run |" in text
    [out_dir] = where.root.iterdir()
    assert "agents says hello" in (out_dir / "agents.log").read_text()


def test_from_resumes_in_the_same_report_and_skips_what_passed(where, monkeypatch):
    first = Recorder(fail="eval", spend=1.0)

    def skills(ctx):
        first.ran.append("skills")
        livecheck._set_id(ctx, "SANDBOX_SKILL_ID", "skill_from_first_run")
        return livecheck.StepResult(True, "uploaded")

    assert main(where, steps=first.steps(skills=skills)) == 1
    assert first.ran == ["preflight", "skills", "agents", "eval"]
    # The run puts the environment back as it found it.
    assert settings().sandbox_skill_id == ""

    second = Recorder(spend=1.0)
    seen = {}

    def eval_step(ctx):
        second.ran.append("eval")
        seen["skill"] = settings().sandbox_skill_id
        return livecheck.StepResult(True, "scored", links=[("Model scorecard",
                                                             "eval/scorecard.md")])

    assert main(where, "--from", "eval", steps=second.steps(eval=eval_step)) == 0
    assert second.ran == ["eval", "triage", "rehearse"]
    assert seen["skill"] == "skill_from_first_run", "a resumed run uses the ids it set"
    assert len(list(where.root.iterdir())) == 1, "resumed in the same report directory"
    text = report(where)
    assert all(f"| {n} | pass |" in text for n in livecheck.STEP_NAMES)
    assert "[Model scorecard](eval/scorecard.md)" in text
    # $1 each for preflight, agents and the failed eval the first time (the
    # failed eval's spend still counts once it is run again), then triage and
    # the rehearsal; the fake skills and the second eval spent nothing.
    assert "Committed so far across every step recorded here: $5.00" in text


# --- the ids written where they are read -------------------------------------------


def _fake_apply_client(monkeypatch):
    created: list[dict] = []

    def create_agent(**kw):
        created.append(kw)
        return NS(id=f"agent_{len(created)}", name=kw["name"], version=1)

    client = contract.strict(NS(beta=NS(
        agents=NS(create=create_agent),
        environments=NS(create=lambda **kw: NS(id="env_1", name=kw["name"])),
        memory_stores=NS(create=lambda **kw: NS(id="memstore_1")),
    )))
    monkeypatch.setattr(lra.config, "anthropic_client", lambda: client)


def _jsonc(text: str) -> dict:
    return json.loads(re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE))


def test_agents_apply_writes_its_ids_into_wrangler_and_env_keeping_every_comment(
        where, monkeypatch):
    _fake_apply_client(monkeypatch)
    before = where.wrangler.read_text()
    rec = Recorder()

    def skills(ctx):
        livecheck._set_id(ctx, "SANDBOX_SKILL_ID", "skill_tools")
        livecheck._set_id(ctx, "SANDBOX_CLOSING_SKILL_ID", "skill_closing")
        return livecheck.StepResult(True, "uploaded")

    assert main(where, steps=rec.steps(skills=skills, agents=livecheck.step_agents)) == 0
    after = where.wrangler.read_text()
    vars_ = _jsonc(after)["vars"]
    assert vars_["MANAGED_REVIEW_AGENT_ID"] == "agent_1"
    assert vars_["MANAGED_ASSOCIATE_AGENT_ID"] == "agent_2"
    assert vars_["MANAGED_ENVIRONMENT_ID"] == "env_1"
    assert vars_["MANAGED_FIRM_MEMORY_STORE_ID"] == "memstore_1"
    assert vars_["SANDBOX_SKILL_ID"] == "skill_tools"
    assert vars_["MANAGED_PLAYBOOK_AGENT_ID"] == "agent_3"
    assert vars_["SANDBOX_CLOSING_SKILL_ID"] == "skill_closing"
    # Only those lines changed: every comment, blank line and other var stays.
    old, new = before.splitlines(), after.splitlines()
    assert len(old) == len(new)
    changed = [(a, b) for a, b in zip(old, new, strict=True) if a != b]
    assert len(changed) == 10
    assert all(re.sub(r'"[^"]*",?$', "", a) == re.sub(r'"[^"]*",?$', "", b)
               for a, b in changed), "indentation and key unchanged"
    env = where.env.read_text()
    assert env.startswith("# local settings\nANTHROPIC_API_KEY=sk-test\n")
    assert "MANAGED_REVIEW_AGENT_ID=agent_1" in env and env.count("MANAGED_REVIEW_AGENT_ID") == 1
    assert "MANAGED_PLAYBOOK_AGENT_ID=agent_3" in env
    assert "SANDBOX_CLOSING_SKILL_ID=skill_closing" in env
    # Every id the run set has a var, so none is left for the operator to place.
    assert "not in wrangler.jsonc vars" not in report(where)


def test_no_write_prints_the_lines_to_change_and_writes_nothing(where, monkeypatch, capsys):
    _fake_apply_client(monkeypatch)
    wrangler, env = where.wrangler.read_text(), where.env.read_text()
    rec = Recorder()
    assert main(where, "--no-write", steps=rec.steps(agents=livecheck.step_agents)) == 0
    assert where.wrangler.read_text() == wrangler and where.env.read_text() == env
    out = capsys.readouterr().out
    n = next(i for i, line in enumerate(wrangler.splitlines(), 1)
             if '"MANAGED_REVIEW_AGENT_ID"' in line)
    assert f"line {n}:" in out
    assert '- "MANAGED_REVIEW_AGENT_ID": "",' in out
    assert '+ "MANAGED_REVIEW_AGENT_ID": "agent_1",' in out
    assert "MANAGED_ENVIRONMENT_ID=env_1" in out


def test_rewrite_wrangler_sets_a_value_already_there_and_names_what_is_absent():
    text = '{\n  // ids\n  "vars": {\n    "A_ID": "old", // trailing\n    "B": "x"\n  }\n}\n'
    new, changes, absent = livecheck.rewrite_wrangler(text, {"A_ID": "new", "C_ID": "c"})
    assert new == text.replace('"old"', '"new"')
    assert changes == [(4, '    "A_ID": "old", // trailing', '    "A_ID": "new", // trailing')]
    assert absent == ["C_ID"]


# --- the dry run --------------------------------------------------------------------


def test_the_dry_run_lists_the_steps_checks_locally_and_spends_nothing(
        where, monkeypatch, capsys):
    monkeypatch.setattr(lra.config, "anthropic_client",
                        lambda: (_ for _ in ()).throw(AssertionError("a client was built")))
    assert main(where, "--dry-run") == 0
    out = capsys.readouterr().out
    for name in livecheck.STEP_NAMES:
        assert f"  {name}" in out
    assert "[ok] ANTHROPIC_API_KEY is set" in out
    assert "claude-fable-5-1 (needs 30-day retention" in out
    assert "split between them in proportion" in out, "$40 is less than every worst case"
    assert not where.root.exists(), "a dry run writes no report"
    assert where.wrangler.read_text() == (ROOT / "cloudflare" / "wrangler.jsonc").read_text()


def test_the_dry_run_fails_on_a_missing_prerequisite(where, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    settings.cache_clear()
    assert main(where, "--dry-run", "--from", "eval", "--budget-usd", "200") == 1
    out = capsys.readouterr().out
    assert "[!!] ANTHROPIC_API_KEY is set" in out
    assert "[!!] the agents are configured" in out
    assert "  preflight" not in out and "split between them" not in out


# --- the shared budget --------------------------------------------------------------


def test_the_budget_is_shared_in_proportion_and_what_one_step_leaves_passes_on(tmp_path):
    ctx = livecheck.Context(out=tmp_path, wrangler=tmp_path / "w", env_file=tmp_path / "e",
                            budget_usd=40.0, remaining_steps=list(livecheck.STEP_NAMES))
    assert ctx.allowance("preflight") == livecheck.PREFLIGHT_USD
    worst = {s: livecheck.worst_case(s) for s in ("eval", "triage", "rehearse")}
    share = ctx.allowance("eval")
    assert share == pytest.approx((40 - livecheck.PREFLIGHT_USD) * worst["eval"]
                                  / sum(worst.values()))
    # The eval commits only whole sessions; the rest goes to the steps after it.
    ctx.committed_usd = livecheck.PREFLIGHT_USD + 16.0
    ctx.remaining_steps = ["triage", "rehearse"]
    assert ctx.allowance("triage") > (40 - livecheck.PREFLIGHT_USD - share) * worst["triage"] \
        / (worst["triage"] + worst["rehearse"])
    # With enough budget every step gets its whole worst case.
    ctx = livecheck.Context(out=tmp_path, wrangler=tmp_path / "w", env_file=tmp_path / "e",
                            budget_usd=500.0, remaining_steps=list(livecheck.STEP_NAMES))
    assert ctx.allowance("eval") == worst["eval"]


def test_the_triage_cost_matches_the_evals_own_guard():
    from evals import score_model

    assert livecheck.TRIAGE_CALL_USD == score_model.TRIAGE_USD_PER_CALL


def test_the_eval_step_runs_the_scorecard_within_its_share(where, monkeypatch):
    from evals import score_model
    from lra import managed

    monkeypatch.setattr(managed, "require_configured", lambda: None)
    seen = {}

    def fake_main(argv):
        seen["argv"] = argv
        out = Path(argv[argv.index("--out") + 1])
        for i, (error, not_run) in enumerate([("", ""), ("", ""),
                                              ("", "the $5.00 budget would be passed")]):
            (out / "results" / f"doc{i}").mkdir(parents=True)
            (out / "results" / f"doc{i}" / "result.json").write_text(
                json.dumps({"name": f"doc{i}", "error": error, "not_run": not_run}))
        (out / "scorecard.md").write_text("# card\n")
        return 1  # targets not met: the baseline, not a failure

    monkeypatch.setattr(score_model, "main", fake_main)
    rec = Recorder()
    assert main(where, "--budget-usd", "40", steps=rec.steps(eval=livecheck.step_eval)) == 0
    argv = seen["argv"]
    budget = float(argv[argv.index("--budget-usd") + 1])
    assert 0 < budget < 40 and "--live" in argv
    text = report(where)
    assert "| eval | pass | $4.00 |" in text, "two sessions committed at $2 each"
    assert "[Model scorecard](eval/scorecard.md)" in text
    assert "targets are not met" in text


def test_the_eval_step_fails_when_the_account_cannot_pay(where, monkeypatch):
    from evals import score_model
    from lra import managed

    monkeypatch.setattr(managed, "require_configured", lambda: None)

    def fake_main(argv):
        out = Path(argv[argv.index("--out") + 1])
        for i, (error, not_run) in enumerate([(f"BadRequestError: {NO_CREDIT}", ""),
                                              ("", f"BadRequestError: {NO_CREDIT}")]):
            (out / "results" / f"doc{i}").mkdir(parents=True)
            (out / "results" / f"doc{i}" / "result.json").write_text(
                json.dumps({"name": f"doc{i}", "error": error, "not_run": not_run}))
        return 1

    monkeypatch.setattr(score_model, "main", fake_main)
    rec = Recorder()
    assert main(where, steps=rec.steps(eval=livecheck.step_eval)) == 1
    assert rec.ran == ["preflight", "skills", "agents"]
    text = report(where)
    assert "| eval | FAIL |" in text and "Add credit" in text


def test_the_command_is_wired_into_the_cli(monkeypatch, capsys):
    import sys

    from lra import cli

    got = {}
    monkeypatch.setattr(livecheck, "main", lambda args: got.setdefault("args", args) and 0)
    monkeypatch.setattr(sys, "argv", ["lra", "live-check", "--dry-run"])
    cli.main()
    assert got["args"] == ["--dry-run"]
    monkeypatch.setattr(sys, "argv", ["lra"])
    cli.main()
    assert "lra live-check" in capsys.readouterr().out


def test_the_rehearsal_runs_as_many_steps_as_its_share_covers(where, monkeypatch):
    from demos.falcon import rehearse
    from lra import managed

    monkeypatch.setattr(managed, "configured", lambda: True)
    steps = livecheck.rehearsal_steps()
    seen = {}

    def fake_run(out, live=False, until=None, only=None, echo=print):
        seen.update(live=live, until=until,
                    cap=lra.config.settings().managed_session_budget_cents)
        upto = steps.index(until) + 1 if until else len(steps)
        return {sid: {"replies": ["reply"], "missing": []} for sid in steps[:upto]}

    monkeypatch.setattr(rehearse, "run", fake_run)
    # Only the preflight and the rehearsal spend here, and the budget has room
    # for three and a half rehearsal steps after the preflight.
    monkeypatch.setattr(livecheck, "SPENDING", ("preflight", "rehearse"))
    budget = livecheck.PREFLIGHT_USD + 3.5 * livecheck.rehearsal_step_usd()
    rec = Recorder()
    assert main(where, "--budget-usd", f"{budget}",
                steps=rec.steps(rehearse=livecheck.step_rehearse)) == 0
    assert seen == {"live": True, "until": steps[2],
                    "cap": round(livecheck.REHEARSAL_SESSION_USD * 100)}
    text = report(where)
    assert f"3 of {len(steps)} step(s) run (the budget stopped it after step {steps[2]})" \
        in text
    assert "[Rehearsal replies](rehearse/)" in text


def test_the_triage_step_fails_when_every_call_fails(where, monkeypatch):
    from evals import score_model

    def fake_main(argv):
        out = Path(argv[argv.index("--out") + 1])
        out.mkdir(parents=True)
        card = {"metrics": {"triage_errors": 2}, "cases": [
            {"name": "a", "model": [f"failed: BadRequestError: {NO_CREDIT}"]},
            {"name": "b", "model": [f"failed: BadRequestError: {NO_CREDIT}"]},
            {"name": "c", "model": ["failed: not run: budget"]}]}
        (out / "scorecard.json").write_text(json.dumps(card))
        (out / "scorecard.md").write_text("# card\n")
        return 1

    monkeypatch.setattr(score_model, "main", fake_main)
    rec = Recorder()
    assert main(where, steps=rec.steps(triage=livecheck.step_triage)) == 1
    assert "rehearse" not in rec.ran
    text = report(where)
    assert "| triage | FAIL | $0.60 |" in text and "Every triage call failed" in text
