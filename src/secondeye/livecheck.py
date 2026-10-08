"""`second-eye live-check`: the first live run, as one command.

The account has never had credit, so nothing that needs the model has run on
the current code. When credit arrives the first run was six manual steps,
each with its own failure and its own place to copy an id to. This runs them
in order and stops at the first that fails, saying what failed and what to do:

  preflight  the API key, and one minimal Messages call on the review model,
             which is where no credit, retention not enabled and a wrong
             model id show up, each named from the API's own error
  skills     upload every skill in skillsync.CATALOGUE (`second-eye skills sync`)
  agents     `second-eye agents apply`, then the ids into .env and the primary
             deployment's deployments/<name>/tenant.jsonc, its config
             rendered again (or, with --no-write, the lines to change
             printed); with no tenant at all, cloudflare/wrangler.jsonc
  eval       `second-eye eval --live`, the model scorecard
  triage     `second-eye eval --triage --live`, triage against the rules
  rehearse   `python -m demos.falcon.rehearse --live`, the demo end to end

One dollar budget (--budget-usd, default 40) covers every step that spends.
It is enforced the way `second-eye eval --live` enforces its own: as a worst case,
each session capped on the platform and none started unless its whole cap
still fits, so the run can cost less than the budget and never more. When the
budget is less than every step's worst case, it is split between the steps in
proportion to it, and what a step does not commit passes to the steps after.

`--tenant <firm>` runs it for one firm's deployment (`second-eye tenant`): settings
from deployments/<firm>/.env and tenant.jsonc and nothing else, so the
firm's Anthropic key creates the firm's agents, and the ids go into its
tenant.jsonc, whose wrangler config is rendered again. Never the
primary deployment's.

Every step's output goes to work/live-check/<time>/<step>.log, and REPORT.md
there is rewritten after each step: pass or fail, the spend committed, the
time taken, and links to the scorecards. `--from STEP` resumes in the latest
report directory after a fix; `--dry-run` lists the steps and checks what can
be checked locally, spending nothing.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
import time
import traceback
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WRANGLER = REPO / "cloudflare" / "wrangler.jsonc"

# A one-line Messages call on Fable 5.1 costs well under a cent; this errs high.
PREFLIGHT_USD = 0.05
# The platform cap on each eval session, as `second-eye eval --live` defaults it.
EVAL_PER_DOC_USD = 2.0
# The platform cap on each rehearsal session, plus one triage call per email
# (TRIAGE defaults to shadow, which calls the model on every message).
REHEARSAL_SESSION_USD = 2.0
TRIAGE_CALL_USD = 0.30  # evals/score_model.TRIAGE_USD_PER_CALL, kept equal by a test

STEP_NAMES = ("preflight", "skills", "agents", "eval", "triage", "rehearse")
SPENDING = ("preflight", "eval", "triage", "rehearse")


@dataclass
class StepResult:
    ok: bool
    summary: str
    fix: str = ""
    spent_usd: float = 0.0
    links: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    seconds: float = 0.0


@dataclass
class Context:
    out: Path
    wrangler: Path
    env_file: Path
    budget_usd: float
    write: bool = True
    # Every id the run has set, by setting name: what goes into .env and
    # wrangler.jsonc, and what a resumed run puts back in the environment.
    ids: dict[str, str] = field(default_factory=dict)
    committed_usd: float = 0.0
    remaining_steps: list[str] = field(default_factory=list)
    # What the environment held for each id before the run set it, restored
    # when the run ends.
    env_before: dict[str, str | None] = field(default_factory=dict)
    # Called after ids are written into `wrangler`: a tenant's rendered
    # config is generated from its tenant.jsonc (second-eye tenant).
    after_write: Callable[[], object] | None = None

    def allowance(self, step: str) -> float:
        """What `step` may commit: its worst case when the rest of the budget
        covers every spending step still to run, otherwise its share. The
        preflight is a few cents and comes off the top."""
        left = max(0.0, self.budget_usd - self.committed_usd)
        if step == "preflight":
            return min(PREFLIGHT_USD, left)
        if "preflight" in self.remaining_steps:
            left = max(0.0, left - PREFLIGHT_USD)
        still = [s for s in self.remaining_steps if s in SPENDING and s != "preflight"]
        worst = {s: worst_case(s) for s in still}
        total = sum(worst.values())
        if step not in worst or total <= left + 1e-9:
            return min(worst.get(step, 0.0), left)
        return left * worst[step] / total


# --------------------------------------------------------------------------
# What each spending step can cost at worst
# --------------------------------------------------------------------------


def _repo_on_path() -> None:
    """evals/ and demos/ sit beside the package in a checkout, as for `second-eye eval`."""
    if not (REPO / "evals").is_dir() or not (REPO / "demos").is_dir():
        raise RuntimeError("second-eye live-check runs from a checkout of the repository: "
                           "evals/ and demos/ are not installed.")
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))


def eval_documents() -> int:
    _repo_on_path()
    from evals import model_corpus

    return len(model_corpus.ALL)


def triage_cases() -> int:
    _repo_on_path()
    from evals import model_corpus

    return len(model_corpus.TRIAGE)


def rehearsal_steps() -> list[str]:
    _repo_on_path()
    from demos.falcon import mail

    return [str(s["id"]) for s in mail.steps()]


def rehearsal_step_usd() -> float:
    return REHEARSAL_SESSION_USD + TRIAGE_CALL_USD


def worst_case(step: str) -> float:
    if step == "preflight":
        return PREFLIGHT_USD
    if step == "eval":
        return eval_documents() * EVAL_PER_DOC_USD
    if step == "triage":
        return triage_cases() * TRIAGE_CALL_USD
    if step == "rehearse":
        return len(rehearsal_steps()) * rehearsal_step_usd()
    return 0.0


# --------------------------------------------------------------------------
# Reading the API's errors
# --------------------------------------------------------------------------


def classify(exc: BaseException, model: str) -> tuple[str, str, str]:
    """(kind, what failed, what to do) for an exception from the API.

    Read from the SDK's exception class, the status and the error type and
    message the API returned, the way the documentation names them: no
    credit is a 400 "credit balance is too low" (or a 402 billing_error); a
    Fable model on an organisation without 30-day retention is a 400 saying
    data retention must be enabled; a model id that does not exist, or that
    the organisation cannot use, is a 404 not_found_error naming the model.
    """
    import anthropic

    message = str(getattr(exc, "message", "") or exc)
    lowered = message.lower()
    status = getattr(exc, "status_code", None)
    body = getattr(exc, "body", None)
    error_type = ""
    if isinstance(body, dict):
        inner = body.get("error") if isinstance(body.get("error"), dict) else body
        error_type = str(inner.get("type") or "")

    if isinstance(exc, anthropic.APIConnectionError):
        return ("network", f"The API could not be reached: {message}",
                "Check the network connection, then run the step again.")
    if status == 402 or error_type == "billing_error" or "credit balance" in lowered \
            or "credit" in lowered and "billing" in lowered:
        return ("credit", f"The Anthropic account has no credit: {message}",
                "Add credit in the Console (Settings, Billing), then run this again.")
    if "retention" in lowered:
        return ("retention",
                (f"{model} needs 30-day data retention, and this organisation does not "
                f"have it: {message}"),
                ("Turn on 30-day data retention for the organisation in the Console "
                "(Claude Fable 5.1 requires it), or set ZERO_RETENTION=true to run on "
                "Claude Opus 5 instead, then run this again."))
    if isinstance(exc, anthropic.NotFoundError) or status == 404 \
            or error_type == "not_found_error":
        return ("model",
                (f"The model {model} does not exist or this organisation cannot use it: "
                f"{message}"),
                ("Check REVIEW_MODEL in .env (and agents/*.yaml) against the models the "
                "Console lists for this organisation."))
    if isinstance(exc, anthropic.AuthenticationError) or status == 401:
        return ("key", f"The API key was refused: {message}",
                "Set ANTHROPIC_API_KEY in .env to a live key for this organisation.")
    if isinstance(exc, anthropic.PermissionDeniedError) or status == 403:
        return ("permission", f"The key is not allowed to do this: {message}",
                ("Check the key's workspace (ANTHROPIC_WORKSPACE_ID) and its permissions "
                "in the Console."))
    if isinstance(exc, anthropic.RateLimitError) or status == 429:
        return ("rate_limit", f"Rate limited: {message}",
                "Wait a minute and run the step again.")
    if isinstance(exc, anthropic.APIStatusError):
        return ("api", f"The API returned {status} {error_type}: {message}",
                "Read the message above; the step's log has the rest.")
    return ("error", f"{type(exc).__name__}: {message}",
            "Read the traceback in the step's log.")


# --------------------------------------------------------------------------
# The steps
# --------------------------------------------------------------------------


def step_preflight(ctx: Context) -> StepResult:
    """The cheapest call that can fail the way the run would: one short
    message to the model every direct call names."""
    from secondeye.config import anthropic_client, settings

    cfg = settings()
    model = cfg.effective_model
    if not cfg.anthropic_api_key.strip():
        return StepResult(False, "ANTHROPIC_API_KEY is not set.",
                          "Put the key in .env (ANTHROPIC_API_KEY=...), then run this again.")
    if ctx.allowance("preflight") + 1e-9 < PREFLIGHT_USD:
        return _over_budget("preflight", ctx)
    print(f"model: {model}" + (" (ZERO_RETENTION is on)" if cfg.zero_retention else ""))
    try:
        reply = anthropic_client().messages.create(
            model=model, max_tokens=64,
            messages=[{"role": "user", "content": "Reply with the one word: ready"}],
        )
    except Exception as e:  # noqa: BLE001 - classified into what to do
        kind, what, fix = classify(e, model)
        return StepResult(False, what, fix, spent_usd=PREFLIGHT_USD, notes=[f"kind: {kind}"])
    usage = getattr(reply, "usage", None)
    tokens = (f"{getattr(usage, 'input_tokens', '?')} in, "
              f"{getattr(usage, 'output_tokens', '?')} out") if usage else "no usage"
    print(f"answered by {getattr(reply, 'model', model)}: "
          f"stop_reason {getattr(reply, 'stop_reason', '?')}, {tokens}")
    if getattr(reply, "stop_reason", "") == "refusal":
        return StepResult(False, f"{model} declined a one-word prompt (stop_reason refusal).",
                          "Try again; if it persists, the classifiers are misfiring and "
                          "Anthropic support should see the request id.",
                          spent_usd=PREFLIGHT_USD)
    return StepResult(True, f"{model} answers; the account has credit and the model is "
                            f"available ({tokens}).", spent_usd=PREFLIGHT_USD)


def step_skills(ctx: Context) -> StepResult:
    """Every skill in the catalogue, uploaded, its id put in the environment
    so the agents step attaches it."""
    from secondeye import skillsync
    from secondeye.config import settings

    done = []
    for name, (_, setting, _) in skillsync.CATALOGUE.items():
        existing = getattr(settings(), setting).strip()
        try:
            skill_id, version = skillsync.sync(existing, name)
        except Exception as e:  # noqa: BLE001
            _, what, fix = classify(e, settings().effective_model)
            return StepResult(False, f"Uploading {name} failed. {what}", fix)
        print(f"{'uploaded version ' + str(version) + ' of' if existing else 'created'} "
              f"{name}: {skill_id}")
        _set_id(ctx, setting.upper(), skill_id)
        done.append(name)
    persisted = persist(ctx)
    return StepResult(True, f"{len(done)} skill(s) uploaded.", notes=persisted)


def step_agents(ctx: Context) -> StepResult:
    """`second-eye agents apply`, and its ids into .env and wrangler.jsonc."""
    from secondeye import cli

    ids: dict[str, str] = {}
    try:
        code = cli._apply_agents(ids)
    except Exception as e:  # noqa: BLE001
        from secondeye.config import settings

        _, what, fix = classify(e, settings().effective_model)
        return StepResult(False, f"`second-eye agents apply` failed. {what}", fix)
    if code:
        return StepResult(False, "`second-eye agents apply` did not run (see agents.log).",
                          "Set ANTHROPIC_API_KEY in .env, then run this again.")
    for key, value in ids.items():
        _set_id(ctx, key, value)
    persisted = persist(ctx)
    return StepResult(True, f"{len(ids)} id(s) applied.", notes=persisted)


def step_eval(ctx: Context) -> StepResult:
    from secondeye import managed

    try:
        managed.require_configured()
    except managed.NotConfigured as e:
        return StepResult(False, str(e), "Run `second-eye live-check --from agents`.")
    allowance = ctx.allowance("eval")
    if allowance + 1e-9 < EVAL_PER_DOC_USD:
        return _over_budget("eval", ctx)
    _repo_on_path()
    from evals import score_model

    where = ctx.out / "eval"
    code = _argparse_main(score_model.main, [
        "--live", "--budget-usd", f"{allowance:.2f}", "--per-doc-usd", str(EVAL_PER_DOC_USD),
        "--out", str(where)])
    links = [("Model scorecard", "eval/scorecard.md")] if (where / "scorecard.md").exists() \
        else []
    results = where / "results"
    records = [json.loads(p.read_text()) for p in sorted(results.glob("*/result.json"))] \
        if results.is_dir() else []
    ran = [r for r in records if not r.get("not_run")]
    spent = len(ran) * EVAL_PER_DOC_USD
    errored = [r for r in ran if r.get("error")]
    fatal = [r["not_run"] for r in records if r.get("not_run")
             and not r["not_run"].startswith("the $") and "time budget" not in r["not_run"]]
    notes = [f"{len(ran)} of {len(records)} document(s) reviewed, {len(errored)} errored"]
    if code == 2 or not records:
        return StepResult(False, "`second-eye eval --live` could not start (see eval.log).",
                          "Read eval.log, fix what it names, then "
                          "`second-eye live-check --from eval`.",
                          spent, links, notes)
    if fatal or (ran and len(errored) == len(ran)):
        first = (errored[0]["error"] if errored else fatal[0])
        kind, _, fix = classify(RuntimeError(first), "")
        if kind != "credit":
            fix = ("Read the session in the Console (eval/results/*/result.json has its "
                   "id), fix it, then `second-eye live-check --from eval`.")
        return StepResult(False, f"The eval stopped: {first}", fix, spent, links, notes)
    if code == 1:
        notes.append("some scorecard targets are not met: that is the baseline, not a failure")
    return StepResult(True, f"Scorecard written; {notes[0]}.", spent_usd=spent, links=links,
                      notes=notes)


def step_triage(ctx: Context) -> StepResult:
    allowance = ctx.allowance("triage")
    if allowance + 1e-9 < TRIAGE_CALL_USD:
        return _over_budget("triage", ctx)
    _repo_on_path()
    from evals import score_model

    where = ctx.out / "triage"
    code = _argparse_main(score_model.main, [
        "--triage", "--live", "--budget-usd", f"{allowance:.2f}", "--out", str(where)])
    links = [("Triage scorecard", "triage/scorecard.md")] if (where / "scorecard.md").exists() \
        else []
    card_path = where / "scorecard.json"
    if code == 2 or not card_path.exists():
        return StepResult(False,
                          "`second-eye eval --triage --live` could not start (see triage.log).",
                          "Read triage.log, fix what it names, then "
                          "`second-eye live-check --from triage`.", 0.0, links)
    card = json.loads(card_path.read_text())
    rows = card.get("cases", [])
    not_run = sum(1 for r in rows if any("not run" in m for m in r.get("model", [])))
    attempted = len(rows) - not_run
    spent = attempted * TRIAGE_CALL_USD
    errors = int(card.get("metrics", {}).get("triage_errors", 0))
    notes = [f"{attempted} of {len(rows)} email(s) triaged, {errors} call(s) failed"]
    if attempted and errors >= attempted:
        failed = next((m for r in rows for m in r.get("model", []) if m.startswith("failed")),
                      "every call failed")
        return StepResult(False, f"Every triage call failed: {failed}",
                          "Read triage.log, fix it, then `second-eye live-check --from triage`.",
                          spent, links, notes)
    if code == 1:
        notes.append("some triage targets are not met: that is the baseline, not a failure")
    return StepResult(True, f"Scorecard written; {notes[0]}.", spent_usd=spent, links=links,
                      notes=notes)


def step_rehearse(ctx: Context) -> StepResult:
    from secondeye import managed
    from secondeye.config import settings

    if not managed.configured():
        return StepResult(False, "The agents are not configured, so the rehearsal cannot run.",
                          "Run `second-eye live-check --from agents`.")
    steps = rehearsal_steps()
    per_step = rehearsal_step_usd()
    fits = min(len(steps), int((ctx.allowance("rehearse") + 1e-9) // per_step))
    if not fits:
        return _over_budget("rehearse", ctx)
    until = steps[fits - 1] if fits < len(steps) else None
    _repo_on_path()
    from demos.falcon import rehearse

    where = ctx.out / "rehearse"
    previous = os.environ.get("MANAGED_SESSION_BUDGET_CENTS")
    os.environ["MANAGED_SESSION_BUDGET_CENTS"] = str(round(REHEARSAL_SESSION_USD * 100))
    settings.cache_clear()
    try:
        results = rehearse.run(where, live=True, until=until)
    except SystemExit as e:
        return StepResult(False, f"The rehearsal stopped: {e}",
                          "Fix what it names, then `second-eye live-check --from rehearse`.",
                          fits * per_step)
    finally:
        if previous is None:
            os.environ.pop("MANAGED_SESSION_BUDGET_CENTS", None)
        else:
            os.environ["MANAGED_SESSION_BUDGET_CENTS"] = previous
        settings.cache_clear()
    ran = {k: v for k, v in results.items() if not k.startswith("_")}
    spent = len(ran) * per_step
    silent = [k for k, v in ran.items() if not v["replies"]]
    missing = [k for k, v in ran.items() if v["missing"]]
    notes = [f"{len(ran)} of {len(steps)} step(s) run"
             + (f" (the budget stopped it after step {until})" if until else ""),
             (f"steps whose reply lacks a scripted headline: {', '.join(missing)} "
              "(a live agent words things its own way; read them)") if missing
             else "every reply carries its scripted headline"]
    links = [("Rehearsal replies", "rehearse/")]
    if silent:
        return StepResult(False, f"No reply at step(s) {', '.join(silent)}.",
                          "Read rehearse.log and rehearse/<step>/, fix it, then "
                          "`second-eye live-check --from rehearse`.", spent, links, notes)
    return StepResult(True, notes[0] + ".", spent_usd=spent, links=links, notes=notes)


STEPS: dict[str, Callable[[Context], StepResult]] = {
    "preflight": step_preflight,
    "skills": step_skills,
    "agents": step_agents,
    "eval": step_eval,
    "triage": step_triage,
    "rehearse": step_rehearse,
}

DESCRIPTIONS = {
    "preflight": "API key, and one minimal Messages call on the review model",
    "skills": "second-eye skills sync, for every skill in the catalogue",
    "agents": "second-eye agents apply; ids into .env and the deployment's config",
    "eval": "second-eye eval --live, the model scorecard",
    "triage": "second-eye eval --triage --live, triage against the rules",
    "rehearse": "python -m demos.falcon.rehearse --live",
}


def _over_budget(step: str, ctx: Context) -> StepResult:
    left = max(0.0, ctx.budget_usd - ctx.committed_usd)
    return StepResult(False, f"${left:.2f} of the budget is left, which is less than one "
                             f"{step} call or session can commit.",
                      f"Run `second-eye live-check --from {step} --budget-usd N` with a larger N "
                      f"(this step's worst case is ${worst_case(step):.2f}).")


def _argparse_main(fn, argv: list[str]) -> int:
    try:
        return int(fn(argv) or 0)
    except SystemExit as e:
        return int(e.code or 0) if not isinstance(e.code, str) else 2


def _set_id(ctx: Context, key: str, value: str) -> None:
    from secondeye.config import settings

    ctx.ids[key] = value
    ctx.env_before.setdefault(key, os.environ.get(key))
    os.environ[key] = value
    settings.cache_clear()


# --------------------------------------------------------------------------
# Writing the ids where they are read
# --------------------------------------------------------------------------


def rewrite_wrangler(text: str, ids: dict[str, str]) -> tuple[str, list[tuple[int, str, str]],
                                                            list[str]]:
    """The wrangler.jsonc text with each id's var set, every comment and
    every other byte left as it was. Returns (text, [(line number, old line,
    new line)], [ids with no var there]). Only vars already in the file are
    set: one that is not there is not forwarded to the container either
    (cloudflare/src/index.ts, FORWARDED), so adding it would do nothing."""
    lines = text.splitlines(keepends=True)
    changes: list[tuple[int, str, str]] = []
    found: set[str] = set()
    for i, line in enumerate(lines):
        for key, value in ids.items():
            pattern = re.compile(rf'^(\s*"{re.escape(key)}"\s*:\s*)"[^"\n]*"')
            if pattern.match(line):
                found.add(key)
                new = pattern.sub(lambda m, v=value: f'{m.group(1)}"{v}"', line, count=1)
                if new != line:
                    changes.append((i + 1, line.rstrip("\n"), new.rstrip("\n")))
                    lines[i] = new
    return "".join(lines), changes, [k for k in ids if k not in found]


def rewrite_env(text: str, ids: dict[str, str]) -> tuple[str, list[str]]:
    """The .env text with each id set: its line replaced where there is one,
    appended where there is not. Returns (text, the KEY=value lines set)."""
    lines = text.splitlines()
    changed: list[str] = []
    for key, value in ids.items():
        entry = f"{key}={value}"
        pattern = re.compile(rf"^\s*(export\s+)?{re.escape(key)}\s*=")
        hit = [i for i, line in enumerate(lines) if pattern.match(line)]
        if hit:
            if lines[hit[-1]].strip() != entry:
                lines[hit[-1]] = entry
                changed.append(entry)
        else:
            lines.append(entry)
            changed.append(entry)
    return ("\n".join(lines) + "\n") if lines else "", changed


def persist(ctx: Context) -> list[str]:
    """The run's ids into .env and wrangler.jsonc, or with --no-write the
    lines to change, printed. Returns notes for the report."""
    notes: list[str] = []
    env_text = ctx.env_file.read_text() if ctx.env_file.exists() else ""
    new_env, env_lines = rewrite_env(env_text, ctx.ids)
    if ctx.wrangler.exists():
        new_wrangler, changes, absent = rewrite_wrangler(ctx.wrangler.read_text(), ctx.ids)
    else:
        new_wrangler, changes, absent = "", [], list(ctx.ids)
        notes.append(f"{ctx.wrangler} does not exist; nothing written there")
    if ctx.write:
        if env_lines:
            ctx.env_file.write_text(new_env)
            print(f"wrote {len(env_lines)} id(s) to {ctx.env_file}")
        if changes:
            ctx.wrangler.write_text(new_wrangler)
            print(f"wrote {len(changes)} id(s) to {ctx.wrangler}")
            if ctx.after_write:
                written = ctx.after_write()
                if written:
                    print(f"rendered {written}")
        notes.append(f"{len(env_lines)} line(s) set in .env, {len(changes)} in wrangler.jsonc")
    else:
        if env_lines:
            print(f"\nset these in {ctx.env_file}:")
            for line in env_lines:
                print(f"  {line}")
        if changes:
            print(f"\nchange these lines in {ctx.wrangler}:")
            for n, old, new in changes:
                print(f"  line {n}:\n    - {old.strip()}\n    + {new.strip()}")
        notes.append("--no-write: the lines to change are in the step's log")
    if absent and ctx.wrangler.exists():
        notes.append("not in wrangler.jsonc vars, nor forwarded to the container: "
                     + ", ".join(f"{k}={ctx.ids[k]}" for k in absent))
    return notes


# --------------------------------------------------------------------------
# The run, the report and the state a resumed run reads
# --------------------------------------------------------------------------


class _Tee:
    def __init__(self, *streams) -> None:
        self.streams = streams

    def write(self, data: str) -> int:
        for s in self.streams:
            s.write(data)
        return len(data)

    def flush(self) -> None:
        for s in self.streams:
            s.flush()


def _load_state(out: Path) -> dict:
    path = out / "state.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _save_state(out: Path, state: dict) -> None:
    (out / "state.json").write_text(json.dumps(state, indent=2))


def render_report(state: dict, stopped: str = "") -> str:
    lines = [f"# Live check, {state['started']}", ""]
    total = (state.get("earlier_usd", 0.0)
             + sum(s.get("spent_usd", 0.0) for s in state["steps"].values()))
    lines += [(f"Budget for the latest invocation: ${state['budget_usd']:.2f}. Committed so far "
              f"across every step recorded here: ${total:.2f}. Committed is the worst case "
              "(every session at its platform cap); the Console's usage page has what was "
              "actually billed."), ""]
    lines += ["| Step | Result | Committed | Time | Summary |", "|---|---|---|---|---|"]
    for name in STEP_NAMES:
        s = state["steps"].get(name)
        if not s:
            lines.append(f"| {name} | not run | | | |")
            continue
        result = "pass" if s["ok"] else "FAIL"
        lines.append(f"| {name} | {result} | ${s['spent_usd']:.2f} | {s['seconds']:.0f}s | "
                     f"{s['summary'].replace('|', '/')} |")
    links = [(label, path) for name in STEP_NAMES
             for label, path in (state["steps"].get(name) or {}).get("links", [])]
    if links:
        lines += ["", "## Scorecards and outputs", ""]
        lines += [f"- [{label}]({path})" for label, path in links]
    notes = [(name, n) for name in STEP_NAMES
             for n in (state["steps"].get(name) or {}).get("notes", [])]
    if notes:
        lines += ["", "## Notes", ""] + [f"- {name}: {n}" for name, n in notes]
    if state.get("ids"):
        lines += ["", "## Ids set by this run", ""]
        lines += [f"- `{k}={v}`" for k, v in sorted(state["ids"].items())]
    if stopped:
        s = state["steps"][stopped]
        lines += ["", f"## Stopped at {stopped}", "", s["summary"], "",
                  f"What to do: {s['fix']}", "",
                  f"Then: `second-eye live-check --from {stopped}`"]
    elif all(state["steps"].get(n, {}).get("ok") for n in STEP_NAMES):
        lines += ["", ("Every step passed. Next: deploy (push to main) so the Worker picks "
                      "up the ids in its config, set TRIAGE back to shadow "
                      "there, and send one real email.")]
    lines += ["", "Each step's full output is in `<step>.log` beside this file."]
    return "\n".join(lines) + "\n"


def run(ctx: Context, start: str = "preflight",
        steps: dict[str, Callable[[Context], StepResult]] | None = None,
        state: dict | None = None) -> int:
    from secondeye.config import settings

    steps = steps or STEPS
    state = state or {"started": ctx.out.name, "steps": {}, "ids": {}}
    state["budget_usd"] = ctx.budget_usd
    ctx.ids.update(state.get("ids", {}))
    order = list(STEP_NAMES)
    todo = order[order.index(start):]
    for key, value in list(ctx.ids.items()):  # a resumed run uses the ids it set before
        _set_id(ctx, key, value)
    stopped = ""
    try:
        for i, name in enumerate(todo):
            ctx.remaining_steps = todo[i:]
            print(f"\n== {name}: {DESCRIPTIONS[name]}")
            began = time.monotonic()
            log = ctx.out / f"{name}.log"
            with log.open("w") as fh, contextlib.redirect_stdout(_Tee(sys.stdout, fh)):
                try:
                    result = steps[name](ctx)
                except Exception as e:  # noqa: BLE001 - recorded, and the run stops
                    traceback.print_exc(file=fh)
                    _, what, fix = classify(e, settings().effective_model)
                    result = StepResult(False, what, fix)
            result.seconds = time.monotonic() - began
            ctx.committed_usd += result.spent_usd
            if name in state["steps"]:  # a step run again: its earlier spend still counts
                state["earlier_usd"] = (state.get("earlier_usd", 0.0)
                                        + state["steps"][name].get("spent_usd", 0.0))
            state["steps"][name] = asdict(result)
            state["ids"] = dict(ctx.ids)
            _save_state(ctx.out, state)
            if not result.ok:
                stopped = name
            (ctx.out / "REPORT.md").write_text(render_report(state, stopped))
            mark = "pass" if result.ok else "FAIL"
            print(f"{mark}: {result.summary} (${result.spent_usd:.2f}, "
                  f"{result.seconds:.0f}s; ${ctx.committed_usd:.2f} of "
                  f"${ctx.budget_usd:.2f} committed)")
            if stopped:
                print(f"\nlive-check stopped at {name}: {result.summary}\n"
                      f"What to do: {result.fix}\n"
                      f"Then: second-eye live-check --from {name}\n"
                      f"Report: {ctx.out / 'REPORT.md'}")
                return 1
    finally:
        for key, value in ctx.env_before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        settings.cache_clear()
    print(f"\nEvery step passed. Report: {ctx.out / 'REPORT.md'}")
    return 0


# --------------------------------------------------------------------------
# --dry-run: what would run, and what can be checked without spending
# --------------------------------------------------------------------------


def dry_run(ctx: Context, start: str) -> int:
    from secondeye import managed, skillsync
    from secondeye.config import settings

    cfg = settings()
    order = list(STEP_NAMES)
    todo = order[order.index(start):]
    checks: list[tuple[bool, str]] = []
    checks.append((bool(cfg.anthropic_api_key.strip()), "ANTHROPIC_API_KEY is set"))
    checks.append((True, f"the preflight model is {cfg.effective_model}"
                         + (" (ZERO_RETENTION is on, so no retention check)"
                            if cfg.zero_retention else
                            " (needs 30-day retention on the organisation)")))
    if "skills" in todo:
        missing = [n for n in skillsync.CATALOGUE if not (skillsync.SKILLS / n).is_dir()]
        checks.append((not missing, "every skill's source is present"
                       + (f" (missing: {', '.join(missing)})" if missing else "")))
    if "agents" in todo:
        manifests = ("review", "associate", "playbook", "closing", "comments", "blackline")
        absent = [m for m in manifests if not (managed.AGENTS / f"{m}.agent.yaml").exists()]
        checks.append((not absent, "every agent manifest is present"
                       + (f" (missing: {', '.join(absent)})" if absent else "")))
        ok = ctx.wrangler.exists()
        if ok:
            _, _, absent_vars = rewrite_wrangler(ctx.wrangler.read_text(), {
                k: "x" for k in ("MANAGED_REVIEW_AGENT_ID", "MANAGED_ASSOCIATE_AGENT_ID",
                                 "MANAGED_ENVIRONMENT_ID", "MANAGED_FIRM_MEMORY_STORE_ID")})
            ok = not absent_vars
        checks.append((ok, f"{ctx.wrangler} has the agent id vars"
                       + ("" if ctx.write else " (--no-write: printed, not written)")))
    if start in ("eval", "triage", "rehearse"):
        checks.append((managed.configured(), "the agents are configured (MANAGED_* in .env)"))
    for step in ("eval", "triage"):
        if step in todo:
            checks.append(((REPO / "evals" / "score_model.py").exists(),
                           "evals/ is in this checkout"))
            break
    if "rehearse" in todo:
        checks.append(((REPO / "demos" / "falcon" / "rehearse.py").exists(),
                       "demos/falcon is in this checkout"))

    print(f"second-eye live-check would run, with a budget of ${ctx.budget_usd:.2f}:\n")
    for name in todo:
        ctx.remaining_steps = todo[todo.index(name):]
        line = f"  {name:9} {DESCRIPTIONS[name]}"
        if name in SPENDING:
            share = ctx.allowance(name)
            line += f"  (up to ${share:.2f}; worst case for all of it ${worst_case(name):.2f})"
            ctx.committed_usd += share
        print(line)
    ctx.committed_usd = 0.0
    full = sum(worst_case(s) for s in todo if s in SPENDING)
    if full > ctx.budget_usd + 1e-9:
        print(f"\nThe budget is less than every step's worst case (${full:.2f}), so it is "
              "split between them in proportion: the eval reviews fewer documents, triage "
              "scores fewer emails and the rehearsal stops early. "
              f"--budget-usd {int(full) + 1} runs all of each.")
    print("\nLocal checks (nothing is spent):")
    for ok, what in checks:
        print(f"  [{'ok' if ok else '!!'}] {what}")
    print(f"\nReports would go to {ctx.out.parent}/<time>/.")
    return 0 if all(ok for ok, _ in checks) else 1


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main(argv: list[str], *, root: Path | None = None, wrangler: Path | None = None,
         env_file: Path | None = None,
         steps: dict[str, Callable[[Context], StepResult]] | None = None,
         tenant_root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="second-eye live-check",
        description="The first live run in one command: preflight, skills, agents, the "
                    "model eval, the triage eval and the Falcon rehearsal, stopping at the "
                    "first failure.")
    parser.add_argument("--budget-usd", type=float, default=40.0,
                        help="most this invocation may commit, worst case, across every "
                             "step that spends (default 40)")
    parser.add_argument("--from", dest="start", choices=STEP_NAMES, default="preflight",
                        help="resume at this step, in the latest report directory")
    parser.add_argument("--dry-run", action="store_true",
                        help="list the steps and check local prerequisites; spend nothing")
    parser.add_argument("--no-write", action="store_true",
                        help="print the .env and wrangler.jsonc lines to change instead of "
                             "writing them")
    parser.add_argument("--tenant", default="",
                        help="run for this firm's deployment (deployments/<firm>/): its .env "
                             "and its ids, written into its tenant.jsonc")
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0) if not isinstance(e.code, str) else 2
    if args.budget_usd <= 0:
        print("--budget-usd must be more than 0.")
        return 2

    isolate: contextlib.AbstractContextManager = contextlib.nullcontext()
    after_write = None
    from secondeye import tenant as tenancy

    try:
        # Without --tenant, the primary deployment's ids go where they always
        # have: its own config, never the shared cloudflare/wrangler.jsonc.
        firm = (tenancy.load(args.tenant, tenant_root or REPO) if args.tenant
                else tenancy.primary_tenant(tenant_root or REPO))
    except tenancy.TenantError as e:
        print(e)
        return 2
    if firm is not None and (wrangler is None or args.tenant):
        wrangler = wrangler or firm.vars_file

        def after_write():
            return tenancy.write(tenancy.load(firm.name, firm.root))

        if not firm.primary:
            # The firm's settings alone, the firm's ids written into its own
            # tenant.jsonc, and its reports beside nobody else's. The primary
            # deployment keeps the working directory's .env, as it always has.
            env_file = env_file or firm.env_file
            root = root or REPO / "work" / "live-check" / firm.name
            isolate = tenancy.environment(firm)
            print(f"tenant {firm.name}: settings from {firm.env_file} and {firm.file}; "
                  f"ids into {firm.file}")

    base = (root or REPO / "work" / "live-check")
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    ctx = Context(out=base / stamp, wrangler=wrangler or WRANGLER,
                  env_file=env_file or Path.cwd() / ".env", budget_usd=args.budget_usd,
                  write=not args.no_write, after_write=after_write)
    with isolate:
        return _main(ctx, args, base, steps)


def _main(ctx: Context, args: argparse.Namespace, base: Path,
          steps: dict[str, Callable[[Context], StepResult]] | None) -> int:
    if args.dry_run:
        return dry_run(ctx, args.start)

    state = None
    if args.start != "preflight":
        earlier = sorted(p for p in base.glob("*") if (p / "state.json").exists()) \
            if base.is_dir() else []
        if earlier:
            ctx.out = earlier[-1]
            state = _load_state(ctx.out)
            print(f"resuming in {ctx.out}")
    ctx.out.mkdir(parents=True, exist_ok=True)
    return run(ctx, args.start, steps, state)
