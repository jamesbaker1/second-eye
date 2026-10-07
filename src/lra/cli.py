"""Local driver. `lra replay some.eml` runs the whole pipeline with no vendor."""

from __future__ import annotations

import sys
from pathlib import Path

from lra.handler import handle
from lra.mail.console import ConsoleProvider

USAGE = """usage: lra replay <file.eml>               run one email through the pipeline, sending nothing
       lra review --live <file.docx>         one real Managed Agents review session, end to end:
                                             start it, poll until it stops, read its files
       lra eval --live [--budget-usd N]      score the model on a corpus with known answers
                                             (also --stub perfect|bad, --replay <dir>)
       lra eval --triage --live|--stub perfect|bad|--replay <dir>
                                             score triage against the rules' baseline
       lra live-check [--budget-usd N] [--from STEP] [--dry-run] [--no-write] [--tenant FIRM]
                                             the first live run in one command: preflight,
                                             skills, agents (ids into .env and wrangler.jsonc),
                                             eval, triage, the Falcon rehearsal; stops at the
                                             first failure, report in work/live-check/;
                                             --tenant: one firm's deployment, its ids into
                                             deployments/<firm>/tenant.jsonc
       lra compare <earlier.docx> <later.docx>  write "<later> (comparison).docx" beside the later file
       lra clean <file.docx>                 write "<file> (clean).docx" beside it
       lra agents apply                      create or update the agents in agents/*.yaml and
                                             the environment, and print the ids for .env
       lra skills build <dir> [name]         assemble a skill, to see what is uploaded
       lra skills sync [name]                upload it to Anthropic and print the id for .env
                                             (names: lra-document-tools, lra-playbook, lra-key-terms,
                                             lra-closing, lra-comments,
                                             lra-negotiation, lra-blackline)
       lra tenant new <firm> --address review@legal.firm.com --domains firm.com [...]
                                             one firm's own deployment, in deployments/<firm>/
       lra tenant provision <firm> [--apply] its D1, R2, Worker and secrets: the wrangler
                                             commands printed, run only with --apply
       lra tenant render|check|list          regenerate, verify and list the deployments
       lra tenant deploy <firm> --release vX.Y.Z [--apply]
                                             a self-hosted firm deploys a release, its own token
       lra tenant support <firm> [--days 3] [--write]
                                             the scoped, expiring token a firm grants for support
       lra tenant offboard <firm> [--apply] [--key-file F]
                                             delete everything, with a deletion certificate
       lra pause <firm> [--apply] | lra resume <firm> [--apply]
                                             the kill switch in the firm's D1, no deploy needed
       lra keygen                            print a new DATA_KEY for encrypting stored documents
       lra purge                             the daily retention sweep, run now
       lra purge --lawyer x | --client n | --matter m [--apply] [--by name]
                                             everything held for one lawyer, client or matter,
                                             Anthropic's copies included; a dry run without
                                             --apply, and run again to finish one that failed
       lra purge --history                   the purges run so far: who, scope, counts, when
       lra clients add <number> <name> [--alias NAME]... [--domain acme.com]...
                                             register a client, the key memory is walled by
       lra clients link <matter> <number>    say whose a matter is when its number does not
       lra clients list                      the registered clients and their domains
       lra audit --since 2026-09-01 [--until 2026-10-01] [--lawyer x] [--matter y]
                 [--format csv|json]         the audit trail: one row per job, no contents
       lra selftest [--tenant FIRM] [--env-file PATH] [--offline] [--wrangler] [--json]
                                             a deployment's security settings checked, PASS/FAIL,
                                             read-only: no model call, no email (docs/it/)"""


def main() -> int:
    if len(sys.argv) < 2:
        print(USAGE)
        return 1
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "replay":
        # A bare `lra replay` used to index argv[2] and hand a new contributor
        # a traceback instead of the usage line it already knows how to print.
        if len(args) < 1:
            print(USAGE)
            return 1
        raw = Path(args[0]).read_bytes()
        # The same provider parses and "sends", so replay cannot reach a live
        # mail API however MAIL_PROVIDER happens to be set.
        console = ConsoleProvider()
        handle(console.parse_eml(raw), provider=console)
        return 0
    if cmd == "review" and args[:1] == ["--live"] and len(args) == 2:
        return _live_review(Path(args[1]))
    if cmd == "eval":
        return _eval(args)
    if cmd == "live-check":
        from lra import livecheck

        return livecheck.main(args)
    if cmd == "tenant":
        from lra import tenant

        return tenant.main(args)
    if cmd == "selftest":
        from lra import selftest

        return selftest.main(args)
    if cmd in ("pause", "resume"):
        from lra import killswitch

        return killswitch.main(cmd, args)
    if cmd == "compare" and len(args) == 2:
        return _compare(Path(args[0]), Path(args[1]))
    if cmd == "clean" and len(args) == 1:
        return _clean(Path(args[0]))
    if cmd == "agents" and args == ["apply"]:
        return _apply_agents()
    if cmd == "skills" and args[:1] == ["build"] and len(args) in (2, 3):
        from lra import skillsync

        print(skillsync.build(Path(args[1]), *args[2:]))
        return 0
    if cmd == "skills" and args[:1] == ["sync"] and len(args) in (1, 2):
        return _sync(*args[1:])
    if cmd == "keygen":
        from lra import crypto

        # Printed once and stored nowhere. Whoever holds it can read the firm's
        # documents, so it goes straight into the firm's secret store.
        print(crypto.generate())
        return 0
    if cmd == "purge":
        return _purge(args)
    if cmd == "clients":
        return _clients(args)
    if cmd == "audit":
        return _audit(args)
    print(USAGE if cmd in ("compare", "clean", "skills", "agents", "review", "clients")
          else f"unknown command: {cmd}")
    return 1


def _apply_agents(ids: dict[str, str] | None = None) -> int:
    """Create or update the agent definitions, the environment and the
    firm's memory store, and print the ids to put in .env.

    `ids`, when given, is filled with every id by its setting name, the ones
    already in .env as well as the new ones: `lra live-check` writes them into
    cloudflare/wrangler.jsonc, which starts empty whatever .env holds.

    This is the control plane, run once per change to agents/*.yaml or to the
    settings that feed them (AGENT_EFFORT, WEB_SEARCH_ENABLED, INFERENCE_GEO,
    the skill ids). With an id already in .env the agent is updated in place,
    which makes a new version; sessions already running keep the old one.
    """
    from lra import closing, managed, playbook
    from lra.config import anthropic_client, settings
    from lra.pipeline import instruct

    cfg = settings()
    if not cfg.anthropic_api_key:
        print("ANTHROPIC_API_KEY is not set, so there is nothing to apply the agents with.")
        return 1
    client = anthropic_client()
    to_set: list[tuple[str, str]] = []

    ids = {} if ids is None else ids
    env = managed.apply_environment(client, cfg.managed_environment_id)
    print(f"environment {env.id} ({env.name})")
    ids["MANAGED_ENVIRONMENT_ID"] = env.id
    if not cfg.managed_environment_id:
        to_set.append(("MANAGED_ENVIRONMENT_ID", env.id))

    reviewer, adopted = _reviewer_id(cfg)
    for manifest, existing, setting, tools in (
        # No custom tools, ever: a clientless session would wait for ever on
        # one (agents/review.agent.yaml).
        ("review.agent.yaml", reviewer, "MANAGED_REVIEW_AGENT_ID", []),
        ("associate.agent.yaml", cfg.managed_associate_agent_id, "MANAGED_ASSOCIATE_AGENT_ID",
         instruct.CUSTOM_TOOLS),
        ("playbook.agent.yaml", cfg.managed_playbook_agent_id, "MANAGED_PLAYBOOK_AGENT_ID",
         playbook.CUSTOM_TOOLS),
        ("closing.agent.yaml", cfg.managed_closing_agent_id, "MANAGED_CLOSING_AGENT_ID",
         closing.CUSTOM_TOOLS),
        # Detached too, so no custom tools either (agents/comments.agent.yaml).
        ("comments.agent.yaml", cfg.managed_comments_agent_id,
         "MANAGED_COMMENTS_AGENT_ID", []),
        # Detached, no custom tools (agents/blackline.agent.yaml).
        ("blackline.agent.yaml", cfg.managed_blackline_agent_id,
         "MANAGED_BLACKLINE_AGENT_ID", []),
    ):
        agent = managed.apply_agent(client, managed.AGENTS / manifest, existing, tools)
        verb = "updated" if existing else "created"
        print(f"{verb} {agent.name}: {agent.id} version {agent.version}")
        ids[setting] = agent.id
        if not existing or (setting == "MANAGED_REVIEW_AGENT_ID" and adopted):
            to_set.append((setting, agent.id))

    if not cfg.managed_firm_memory_store_id:
        store = client.beta.memory_stores.create(
            name="LRA firm memory",
            description="House conventions for this firm's documents: how they are drafted "
                        "when they are right. Personal preferences override these.",
        )
        print(f"created the firm memory store {store.id}")
        to_set.append(("MANAGED_FIRM_MEMORY_STORE_ID", store.id))
        ids["MANAGED_FIRM_MEMORY_STORE_ID"] = store.id
    else:
        ids["MANAGED_FIRM_MEMORY_STORE_ID"] = cfg.managed_firm_memory_store_id

    skills = [s["skill_id"] for s in managed.skill_refs()]
    print("skills attached: " + ", ".join(skills))
    if not cfg.sandbox_skill_id:
        print("note: SANDBOX_SKILL_ID is empty, so the agents have no writer, compare, clean "
              "or check scripts. Run `lra skills sync` first, then `lra agents apply` again.")
    if to_set:
        print("\nadd this to .env:")
        for key, value in to_set:
            print(f"  {key}={value}")
    if adopted:
        print(f"\nThe reviewer is now {reviewer}, which was MANAGED_REVIEW_DETACHED_AGENT_ID: "
              "delete that line from .env (and the Worker's secrets)."
              + (f" The old streaming reviewer {adopted} is no longer used; archive it in "
                 "the Console." if adopted != reviewer else ""))
    return 0


def _reviewer_id(cfg) -> tuple[str, str]:
    """The agent `review.agent.yaml` is applied to, and, when an older
    deployment is being moved over, the id it replaces.

    Before phase 5 (docs/migration.md) there were two reviewers: a streaming
    one at MANAGED_REVIEW_AGENT_ID, with custom tools, and a clientless one at
    MANAGED_REVIEW_DETACHED_AGENT_ID. The clientless one is now the reviewer.
    When its id is still set, it is the agent updated and printed as
    MANAGED_REVIEW_AGENT_ID; otherwise the agent at MANAGED_REVIEW_AGENT_ID is
    updated in place to the clientless definition, which removes its custom
    tools. Either way no session is ever started against an agent that could
    wait on a tool nobody answers."""
    detached = cfg.managed_review_detached_agent_id.strip()
    if detached:
        return detached, cfg.managed_review_agent_id.strip() or detached
    return cfg.managed_review_agent_id, ""


def _live_review(path: Path) -> int:
    """One real review session against the API, printed as it goes.

    The way the first live run is done. Nothing is emailed; the findings are
    printed and the outputs are written beside the document. Fails in one
    sentence if the agents are not configured or the account cannot pay.

    The session is started, its Console link printed, and then it is polled
    until it stops. Polling is what lets it run here, with no webhook to call
    us back.
    """
    import anthropic

    from lra import managed, memory
    from lra.models import Attachment, Mode
    from lra.pipeline import checks, extract, redline, review
    from lra.pipeline.filetype import Kind, identify

    try:
        managed.require_configured()
    except managed.NotConfigured as e:
        print(e)
        return 1
    if not path.exists():
        print(f"{path} does not exist.")
        return 1
    raw = path.read_bytes()
    if identify(raw, path.name) is not Kind.DOCX:
        print("The live run takes a .docx: that is the copy the writer marks up.")
        return 1

    att = Attachment(
        filename=path.name,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=len(raw), content=raw,
    )
    doc = extract.extract(att)
    mechanical = checks.run_all(doc, raw)
    print(f"{len(mechanical)} mechanical finding(s) before the session starts")

    user = "live-run@localhost"
    beats = {"n": 0}

    def heartbeat() -> None:
        beats["n"] += 1
        if beats["n"] % 10 == 1:
            print(".", end="", flush=True)

    try:
        session_id = review.start(
            doc, Mode.REDLINE, "", matter_id=None,
            memory_stores=memory.stores_for(user),
            checks_block=checks.as_prompt_block(mechanical), original=raw,
            user_address=user,
        )
        print(f"started {session_id}: {managed.console_url(session_id)}")
        print("polling until it stops", end="", flush=True)
        result = review.finish(session_id, Mode.REDLINE, user_address=user,
                               document=path.name, heartbeat=heartbeat)
    except anthropic.PermissionDeniedError as e:
        print(f"The API refused the session: {e.message}. Check the account has credit.")
        return 1
    except anthropic.APIStatusError as e:
        if e.status_code in (400, 402) and "credit" in str(e).lower():
            print("The Anthropic account has no credit, so no session can run.")
            return 1
        print(f"The API returned {e.status_code}: {e.message}")
        return 1
    except RuntimeError as e:
        print(f"The session did not produce a review: {e}")
        return 1

    print(f"\nConsole: {managed.console_url(result.session_id)}")
    print(f"\n{result.summary}\n")
    if result.cut_short:
        print("(cut short by the time or spending budget)\n")
    for f in result.findings:
        print(f"- [{f.severity.value}] {f.title}")
        if f.explanation:
            print(f"    {f.explanation}")
        print(f"    anchor: {f.anchor[:100]!r}")

    for name, data in result.outputs:
        ok, why = redline.verify(data, expect_revisions=True)
        out = path.with_name(name)
        out.write_bytes(data)
        print(f"\noutput {out} ({len(data)} bytes): "
              + ("verified" if ok else f"FAILED verification: {why}"))
    if not result.outputs:
        print("\nno redline came back from the session; the handler would have written "
              "one locally")
    return 0


def _eval(args: list[str]) -> int:
    """The model scorecard (evals/score_model.py). The evals live beside the
    package, not in it, so the checkout they sit in is put on the path."""
    root = Path(__file__).resolve().parents[2]
    if not (root / "evals" / "score_model.py").exists():
        print("lra eval runs from a checkout of the repository: evals/ is not installed.")
        return 1
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from evals import score_model

    try:
        return score_model.main(args)
    except SystemExit as e:  # argparse's usage errors and --help
        return int(e.code or 0) if not isinstance(e.code, str) else 1


def _compare(earlier: Path, later: Path) -> int:
    """The deterministic half only: no model, no network, nothing leaves."""
    from lra.pipeline import compare

    result = compare.compare(earlier.read_bytes(), later.read_bytes())
    for change in result.changes:
        mark = "" if change.landed else f"  [not marked up: {change.why_not}]"
        print(f"{change.kind:9} {change.where}{mark}")
    for note in result.notes:
        print(f"note: {note}")
    if result.content:
        out = later.with_name(f"{later.stem} (comparison).docx")
        out.write_bytes(result.content)
        print(f"wrote {out}" + (" (accept-all and reject-all both verified)"
                                if result.proven else ""))
    elif result.identical:
        print("no changes")
    return 0


def _clean(path: Path) -> int:
    from lra.pipeline import clean

    try:
        result = clean.clean(path.read_bytes())
    except clean.CannotClean as e:
        print(e)
        return 1
    for line in result.removed:
        print(f"removed: {line}")
    if not result.changed:
        print("already clean")
        return 0
    out = path.with_name(f"{path.stem} (clean){path.suffix}")
    out.write_bytes(result.content)
    print(f"wrote {out}")
    return 0


def _purge(args: list[str]) -> int:
    """Retention sweeps, or everything held for one lawyer, client or matter.

    The second form is the one a firm asks about: a lawyer leaves, or a client
    exercises a deletion right, and the answer has to be one command that
    reaches the documents in object storage, what was learned, and
    Anthropic's copies as well as the rows (purge.py). It is a dry run
    unless --apply is given, and running it again finishes one that failed
    part way.
    """
    import argparse

    from lra import purge as scoped

    parser = argparse.ArgumentParser(prog="lra purge")
    parser.add_argument("address", nargs="?", help="a lawyer's address (as --lawyer)")
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--lawyer")
    which.add_argument("--client", help="the client number, as registered")
    which.add_argument("--matter")
    which.add_argument("--history", action="store_true", help="list the purges run so far")
    parser.add_argument("--apply", action="store_true", help="delete; without it, only say")
    parser.add_argument("--by", help="who is running it, for the record (default: login name)")
    try:
        opts = parser.parse_args(args)
    except SystemExit as e:
        return int(e.code or 0)
    if opts.address and (opts.lawyer or opts.client or opts.matter):
        print("Give one of an address, --lawyer, --client or --matter.")
        return 1

    if opts.history:
        for r in scoped.history():
            counts = ", ".join(f"{n} {k}" for k, n in r["counts"].items()) or "nothing held"
            state = f"finished {r['finished_at']}" if r["finished_at"] else "NOT FINISHED"
            print(f"{r['started_at']}  {r['scope']} {r['scope_key']}  by {r['actor']}  "
                  f"{state}: {counts}  [{r['id']}]")
        return 0

    scope, key = next(((s, v) for s, v in (("lawyer", opts.lawyer or opts.address),
                                           ("client", opts.client), ("matter", opts.matter))
                       if v), (None, None))
    if scope is None:
        # The same sweep the Worker's daily cron runs (retention.py).
        from lra import retention

        failed = False
        for what, n in retention.sweep().items():
            failed |= n < 0
            print(f"{what}: {'FAILED, see the log' if n < 0 else f'{n} deleted'}")
        return 1 if failed else 0

    result = scoped.run(scope, key, apply=opts.apply, actor=opts.by)
    p = result.plan
    name = f"{scope} {p.key}"
    if scope == "client":
        from lra import clients

        registered = clients.get(p.key)
        name += f" ({registered.name})" if registered else " (not a registered client)"
        if p.matters:
            name += f", matters {', '.join(p.matters)}"
    if not result.applied:
        print(f"Dry run for {name}: nothing has been deleted. Run again with --apply "
              "to delete what is listed.")
        counts = p.counts
    else:
        print(f"Purge {result.purge_id} for {name}:")
        counts = result.counts
    for kind, label in scoped.KINDS.items():
        if counts.get(kind):
            print(f"  {counts[kind]:6}  {label}")
    if not counts:
        print("  nothing is held for it")
    ids = p.anthropic_ids()
    if ids and not result.applied:
        print("Anthropic ids it would touch:")
        for what, listed in ids.items():
            print(f"  {what}: {', '.join(listed)}")
    if p.unattributed:
        print("Not included, because no client or matter was recorded on them (a purge "
              "of the lawyer who sent them reaches them): "
              + ", ".join(f"{n} {scoped.KINDS[k].split(' (')[0]}"
                          for k, n in p.unattributed.items()))
    if result.failed:
        print(f"{len(result.failed)} item(s) could not be deleted yet; run the same command "
              "again to finish:")
        for kind, item, error in result.failed:
            print(f"  {kind} {item}: {error}")
        return 1
    if result.applied:
        print("Done. The purge is recorded (lra purge --history).")
    return 0


def _audit(args: list[str]) -> int:
    """The audit trail (audit.py) to stdout, for the firm's risk committee."""
    import argparse

    from lra import audit

    parser = argparse.ArgumentParser(prog="lra audit")
    parser.add_argument("--since", required=True, help="YYYY-MM-DD, inclusive")
    parser.add_argument("--until", help="YYYY-MM-DD, exclusive")
    parser.add_argument("--lawyer", help="only jobs this address sent")
    parser.add_argument("--matter", help="only jobs on this matter")
    parser.add_argument("--format", choices=("csv", "json"), default="csv")
    try:
        opts = parser.parse_args(args)
    except SystemExit as e:
        return int(e.code or 0)
    found = audit.rows(opts.since, opts.until, opts.lawyer, opts.matter)
    print(audit.as_json(found) if opts.format == "json" else audit.as_csv(found), end="")
    return 0


def _clients(args: list[str]) -> int:
    """The client register memory is walled by (clients.py, docs/memory.md).
    Registering a client re-applies the wall, so a personal note naming it
    that was written before it was registered leaves the personal scope."""
    from lra import clients

    if args[:1] == ["list"]:
        for c in clients.all_clients():
            extra = ", ".join(c.aliases)
            print(f"{c.id:10} {c.name}" + (f" (also {extra})" if extra else "")
                  + (f"  [{', '.join(c.domains)}]" if c.domains else ""))
        return 0
    if args[:1] == ["link"] and len(args) == 3:
        if not clients.get(args[2]):
            print(f"no client {args[2]} is registered")
            return 1
        clients.link(args[1], args[2], source="admin")
        print(f"matter {args[1]} is client {args[2]}'s")
        return 0
    if args[:1] == ["add"] and len(args) >= 3:
        aliases, domains, rest = [], [], args[3:]
        while rest:
            flag, value, rest = rest[0], rest[1] if len(rest) > 1 else "", rest[2:]
            if flag == "--alias" and value:
                aliases.append(value)
            elif flag == "--domain" and value:
                domains.append(value)
            else:
                print(USAGE)
                return 1
        client = clients.register(args[1], args[2], aliases, domains)
        print(f"registered {client.id}: {client.name}")
        return 0
    print(USAGE)
    return 1


def _sync(name: str = "lra-document-tools") -> int:
    from lra import skillsync
    from lra.config import settings

    _, setting, _ = skillsync.CATALOGUE[name]
    existing = getattr(settings(), setting).strip()
    skill_id, version = skillsync.sync(existing, name)
    if existing:
        print(f"uploaded version {version} of {skill_id}")
    else:
        print(f"created {skill_id} (version {version})")
        print(f"add this to .env:  {setting.upper()}={skill_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
