"""`second-eye pause <firm>` / `second-eye resume <firm>`: the kill switch, with no deploy.

The Worker reads one row of the deployment's own D1 database on every
message, send and cron run (cloudflare/src/pause.ts). This writes it, with
`wrangler d1 execute --remote` and the CLOUDFLARE_API_TOKEN of whoever runs
it: for a self-hosted firm, the firm's IT with the firm's token. It is a dry
run unless --apply is given, and the dry run prints the exact command, so it
can go into the firm's runbook as it stands.

Three other ways to stop it, none of which needs us (docs/it/self-hosted.md):
the same SQL in the D1 console of the firm's Cloudflare dashboard; turning
off the Email Routing rule or the firm's transport rule, so no mail reaches
it; revoking the firm's Anthropic key, so no review can run.

The SERVICE_PAUSED var still works and wins: paused there is paused whatever
this row says, and `second-eye resume` says so.
"""

from __future__ import annotations

import argparse
import getpass
import re
from datetime import UTC, datetime
from pathlib import Path

from secondeye import tenant as tenants

# Must equal SETTINGS_SCHEMA in cloudflare/src/pause.ts (tests/test_killswitch.py).
SCHEMA = ("CREATE TABLE IF NOT EXISTS edge_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
          "updated_at TEXT NOT NULL, updated_by TEXT)")
KEY = "service_paused"


def _who(by: str | None) -> str:
    if not by:
        try:
            by = getpass.getuser()
        except Exception:  # noqa: BLE001 - no login name in a container
            by = "unknown"
    # It goes into SQL text (wrangler binds no parameters on the command line).
    return re.sub(r"[^A-Za-z0-9@._ +-]", "", by)[:80] or "unknown"


def sql(on: bool, by: str, at: str | None = None) -> str:
    at = at or datetime.now(UTC).isoformat(timespec="seconds")
    if not re.match(r"^[0-9T:.+\-Z]+$", at):
        raise ValueError(f"not a timestamp: {at!r}")
    value = "true" if on else "false"
    return (f"{SCHEMA}; INSERT INTO edge_settings (key, value, updated_at, updated_by) "
            f"VALUES ('{KEY}', '{value}', '{at}', '{_who(by)}') ON CONFLICT(key) DO UPDATE SET "
            "value = excluded.value, updated_at = excluded.updated_at, "
            "updated_by = excluded.updated_by")


STATUS_SQL = f"SELECT value, updated_at, updated_by FROM edge_settings WHERE key = '{KEY}'"


def steps(t: tenants.Tenant, on: bool, by: str | None, at: str | None = None) -> list[tenants.Step]:
    db, cfg = t.get("d1_database_name"), t.config_arg()
    base = ["npx", "wrangler", "d1", "execute", db, "--remote", "--config", cfg]
    out = [
        tenants.Step(("Pause" if on else "Resume") + f" {t.name}: the switch row in its D1 "
                     "database, read by the Worker on the next message",
                     [*base, "--yes", "--command", sql(on, _who(by), at)]),
        tenants.Step("Read it back", [*base, "--json", "--command", STATUS_SQL]),
    ]
    if not on:
        edge = str(t.vars.get("EDGE_URL", "")) or "<EDGE_URL>"
        out.append(tenants.Step(
            "Release the mail held while paused, now rather than at the daily run (04:17 UTC)",
            ["curl", "-X", "POST", "-H", "Authorization: Bearer $EDGE_SECRET",
             f"{edge}/internal/release"], manual=True,
            note=f"By hand, with EDGE_SECRET from {tenants._rel(t.env_file, t.root)}. Without "
                 "it, held mail is released at the next daily run. A review that was already "
                 "under way picks up within the hour on its own."))
    return out


def run(t: tenants.Tenant, on: bool, *, apply: bool = False, by: str | None = None,
        runner: tenants.Runner = tenants.run_command, at: str | None = None) -> int:
    if not t.provisioned:
        print(f"{t.name} has no D1 database yet, so there is nothing to pause.")
        return 2
    plan = steps(t, on, by, at)
    verb = "pause" if on else "resume"
    print(f"{verb.capitalize()} {t.name}: Worker {t.worker}, D1 {t.get('d1_database_name')}"
          + (f", Cloudflare account {t.account_id}" if t.account_id else "") + ".")
    if on:
        print("While paused: mail from an admitted sender is accepted and held sealed, nothing "
              "is sent, no review starts, and a review under way waits before it sends.\n")
    else:
        varp = str(t.vars.get("SERVICE_PAUSED", "")).strip().lower() in ("true", "1", "yes")
        print("Held mail is released, and reviews that were waiting carry on.\n"
              + ("NOTE: SERVICE_PAUSED is true in the config as well, and the var wins: it "
                 "stays paused until that is false and deployed.\n" if varp else ""))
    if not apply:
        for i, step in enumerate(plan, 1):
            print(f"# {i}. {step.title}" + (" [by hand]" if step.manual else ""))
            if step.note:
                print(f"#    {step.note}")
            print(f"{_line(t, step)}\n")
        print(f"Dry run: nothing was changed. `second-eye {verb} {t.name} --apply` runs the "
              "first two with your CLOUDFLARE_API_TOKEN. Or paste the SQL into the D1 console "
              "of the Cloudflare dashboard (Storage & Databases -> D1 -> "
              f"{t.get('d1_database_name')} -> Console).")
        return 0
    env = {"CLOUDFLARE_ACCOUNT_ID": t.account_id} if t.account_id else {}
    for i, step in enumerate(plan, 1):
        if step.manual:
            print(f"# {i}. {step.title} [by hand]\n#    {step.note}\n{_line(t, step)}")
            continue
        print(f"# {i}. {step.title}\n$ {_line(t, step)}")
        code, _ = runner(step.argv, t.root / "cloudflare", env, None)
        if code != 0:
            print(f"\nFailed (exit {code}): the switch is as it was. Use the D1 console in the "
                  "dashboard, or turn off the Email Routing rule, to stop mail now.")
            return 1
    print(f"\n{t.name} is {'paused' if on else 'running'}.")
    return 0


def _line(t: tenants.Tenant, step: tenants.Step) -> str:
    """The command as it is pasted: SQL in double quotes (it has single
    quotes and nothing a shell expands), a header with $EDGE_SECRET likewise."""
    import shlex

    quoted = [f'"{a}"' if ("'" in a or "$" in a) and not re.search(r'["`\\]', a)
              else shlex.quote(a) for a in step.argv]
    line = " ".join(quoted)
    if step.argv[0] == "npx":
        prefix = f"CLOUDFLARE_ACCOUNT_ID={t.account_id} " if t.account_id else ""
        line = f"(cd cloudflare && {prefix}{line})"
    return line


def main(verb: str, argv: list[str], *, root: Path = tenants.REPO,
         runner: tenants.Runner = tenants.run_command) -> int:
    parser = argparse.ArgumentParser(prog=f"second-eye {verb}")
    parser.add_argument("firm", help="the tenant, as in deployments/<firm>/ (jim for Jim's)")
    parser.add_argument("--apply", action="store_true", help="change it (default: print how)")
    parser.add_argument("--by", help="who, for the row (default: login name)")
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)
    try:
        t = tenants.load(args.firm, root)
    except tenants.TenantError as e:
        print(e)
        return 2
    return run(t, verb == "pause", apply=args.apply, by=args.by, runner=runner)
