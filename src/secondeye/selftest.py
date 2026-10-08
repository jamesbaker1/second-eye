"""`second-eye selftest`: a firm's IT checks a deployment for themselves.

A security team is handed a document that says "replies go only to the
sender", "strangers are dropped", "documents are sealed under your key".
This is the command that lets them check those claims against the
deployment instead of trusting the document (docs/it/README.md). It prints
one line per check, PASS, WARN, FAIL, INFO or SKIP, with the evidence, and
exits 1 if anything failed, so it can be run after every upgrade and the
output filed with the change ticket.

It is read-only, and says so in its header:

- no model call: the Anthropic checks only *retrieve* the agent, environment
  and memory store definitions with the firm's own key;
- no email: "a stranger's mail is dropped" is checked against the Worker's
  own admission rule (`isAllowed` in cloudflare/src/mail.ts), replicated in
  `worker_admits` and held to that source by tests/test_selftest.py;
- no write: the audit export is read over a connection that runs no schema
  statements (a read-only SQLite file, or D1 through the Worker with only
  SELECTs), and the Worker probes are GETs that a correct Worker refuses.

  second-eye selftest [--tenant FIRM] [--env-file PATH] [--offline] [--wrangler] [--json]

--tenant reads the firm's settings from deployments/<firm>/tenant.jsonc and
its secrets from deployments/<firm>/.env (or --env-file); without it the
settings are this machine's (environment and .env). --offline skips every
network check. --wrangler also lists the Worker's secret *names* with
`wrangler secret list` (needs a Cloudflare token for the firm's account).
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import sqlite3
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

PASS, WARN, FAIL, INFO, SKIP = "PASS", "WARN", "FAIL", "INFO", "SKIP"

# Keys that appear in this repository's tests and the Worker's test config
# (cloudflare/vitest.config.ts). A deployment holding one of them is holding
# a key anybody with the source can read.
KNOWN_TEST_KEYS = {
    "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
    "ZmVkY2JhOTg3NjU0MzIxMGZlZGNiYTk4NzY1NDMyMTA=",
}

# Bare domains that would admit anybody with a free mailbox. An exact address
# on one of them is fine (Jim's own deployment is one Gmail address).
PUBLIC_MAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com",
    "yahoo.com", "ymail.com", "icloud.com", "me.com", "mac.com", "aol.com", "proton.me",
    "protonmail.com", "gmx.com", "gmx.net", "mail.com", "zoho.com", "yandex.com",
    "hotmail.co.uk", "yahoo.co.uk", "btinternet.com",
}

# The agent definitions a deployment points at, by setting.
AGENT_IDS = ("MANAGED_REVIEW_AGENT_ID", "MANAGED_ASSOCIATE_AGENT_ID",
             "MANAGED_PLAYBOOK_AGENT_ID", "MANAGED_COMMENTS_AGENT_ID",
             "MANAGED_CLOSING_AGENT_ID", "MANAGED_BLACKLINE_AGENT_ID")
REQUIRED_IDS = ("MANAGED_REVIEW_AGENT_ID", "MANAGED_ASSOCIATE_AGENT_ID",
                "MANAGED_ENVIRONMENT_ID", "MANAGED_FIRM_MEMORY_STORE_ID")

TRUE = {"true", "1", "yes", "on"}


@dataclass
class Check:
    name: str
    status: str
    detail: str


# --------------------------------------------------------------------------
# The Worker's rule, replicated
# --------------------------------------------------------------------------


def worker_admits(sender: str, allowlist: str) -> bool:
    """`isAllowed` in cloudflare/src/mail.ts, line for line: an entry is an
    exact address or an exact domain, the list is split on commas and
    whitespace, and an empty list admits nobody. The application's own rule
    (intake.check_sender) differs only in that an empty list there means the
    firm's domains; the Worker sees the mail first, so this is the one that
    decides what a stranger's mail does."""
    entries = [e for e in allowlist.lower().replace(",", " ").split() if e]
    sender = sender.strip().lower()
    domain = sender.split("@")[-1] if sender else ""
    return sender in entries or domain in entries


def _strangers(allowlist: str, firm_domains: list[str]) -> list[str]:
    """Senders the deployment must drop: someone unrelated, a lookalike of
    the firm's domain, a subdomain of it (not admitted by an exact domain
    entry), and a forged-looking address with the firm's name in its local
    part."""
    firm = firm_domains[0] if firm_domains else "firm.example"
    out = ["stranger@example.invalid",
           f"partner@{firm}.attacker.example",
           f"{firm.split('.')[0]}@attacker.example",
           f"partner@{firm.split('.')[0]}-law.example"]
    if firm.lower() in allowlist.lower().replace(",", " ").split():
        out.append(f"someone@sub.{firm}")
    return out


# --------------------------------------------------------------------------
# What is checked
# --------------------------------------------------------------------------


def _split(value: str) -> list[str]:
    return [v.strip().lower() for v in str(value or "").replace(" ", ",").split(",") if v.strip()]


def _int(value, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _on(value) -> bool:
    return str(value or "").strip().lower() in TRUE


def config_checks(v: dict[str, str], secrets: dict[str, str]) -> list[Check]:
    """Every check that needs nothing but the settings. Pure."""
    out: list[Check] = []
    allow = str(v.get("ALLOWED_SENDERS", ""))
    entries = _split(allow)
    firm = _split(v.get("FIRM_DOMAINS", ""))

    # Who can send.
    if not entries:
        out.append(Check("allowlist", FAIL, "ALLOWED_SENDERS is empty: the Worker drops every "
                         "message, so nobody can use the deployment"))
    else:
        out.append(Check("allowlist", PASS, f"ALLOWED_SENDERS has {len(entries)} entr"
                         f"{'y' if len(entries) == 1 else 'ies'}: {', '.join(entries)}"))
    public = [e for e in entries if "@" not in e and e in PUBLIC_MAIL_DOMAINS]
    if public:
        out.append(Check("allowlist-public-domain", FAIL,
                         f"{', '.join(public)} on ALLOWED_SENDERS admits anyone with a free "
                         "mailbox there; list exact addresses instead"))
    odd = [e for e in entries if "*" in e or "." not in e.split("@")[-1]]
    if odd:
        out.append(Check("allowlist-entries", WARN,
                         f"{', '.join(odd)}: not an address or a domain, so it matches nobody "
                         "(there are no wildcards)"))
    uncovered = [d for d in firm if d not in entries and not any(e.endswith("@" + d)
                                                                  for e in entries)]
    if entries and uncovered:
        out.append(Check("allowlist-firm-domains", WARN,
                         f"no entry admits anyone on {', '.join(uncovered)} (FIRM_DOMAINS); "
                         "lawyers sending from there are dropped"))

    strangers = _strangers(allow, firm)
    admitted = [s for s in strangers if worker_admits(s, allow)]
    if admitted:
        out.append(Check("stranger-dropped", FAIL,
                         f"the Worker's rule would admit {', '.join(admitted)}"))
    else:
        out.append(Check("stranger-dropped", PASS,
                         f"the Worker's rule drops all {len(strangers)} test senders "
                         f"({', '.join(strangers)}); checked against isAllowed, no mail sent"))

    if not firm:
        out.append(Check("firm-domains", FAIL, "FIRM_DOMAINS is empty: colleagues read as "
                         "outside parties, and the container refuses to start on a subdomain"))
    else:
        out.append(Check("firm-domains", PASS, f"FIRM_DOMAINS: {', '.join(firm)}"))

    # Where replies go.
    policy = str(v.get("REPLY_POLICY", "sender_only") or "sender_only").strip().lower()
    if policy == "sender_only":
        out.append(Check("reply-policy", PASS, "REPLY_POLICY=sender_only: every reply goes to "
                         "the sender alone, enforced in the container and again in the Worker"))
    else:
        out.append(Check("reply-policy", FAIL, f"REPLY_POLICY={policy}: the model chooses "
                         "recipients (DECISIONS 31); only right if the firm chose it in writing"))

    # The key.
    out.append(_data_key_check(secrets))
    if str(secrets.get("DATA_KEY_PREVIOUS", "")).strip():
        out.append(Check("data-key-previous", INFO, "DATA_KEY_PREVIOUS is set: a rotation is in "
                         "progress; the old key still opens what it sealed"))

    # Retention.
    hours = _int(v.get("RETENTION_HOURS", 24), 24)
    out.append(Check("retention-jobs", FAIL if hours <= 0 else WARN if hours > 24 else PASS,
                     f"RETENTION_HOURS={hours}" + (" (0 or less keeps job rows for ever)"
                                                   if hours <= 0 else "")))
    # 7 is the product's default (config.py, retention.py): longer is the
    # firm's call, and worth seeing; 0 keeps everything for ever.
    days = _int(v.get("THREAD_RETENTION_DAYS", 7), 7)
    out.append(Check("retention-threads", FAIL if days <= 0 else WARN if days > 7 else PASS,
                     f"THREAD_RETENTION_DAYS={days}" + (" (0 keeps conversations and their "
                                                        "documents for ever)" if days <= 0
                                                        else "")))
    if not _on(v.get("ARCHIVE_ENABLED")):
        out.append(Check("archive", PASS, "ARCHIVE_ENABLED is off: no copy of every message, "
                         "no plaintext search index"))
    elif _int(v.get("ARCHIVE_RETENTION_DAYS", 7), 7) <= 0:
        out.append(Check("archive", FAIL, "ARCHIVE_ENABLED is on with ARCHIVE_RETENTION_DAYS=0: "
                         "every message is kept for ever, its text searchable outside the key"))
    else:
        out.append(Check("archive", WARN, "ARCHIVE_ENABLED is on (ARCHIVE_RETENTION_DAYS="
                         f"{_int(v.get('ARCHIVE_RETENTION_DAYS', 7), 7)}): its search index is "
                         "plaintext in D1; needs the general counsel's written sign-off"))

    if _on(v.get("LEARN_FROM_OUTCOMES")):
        out.append(Check("learn-from-outcomes", WARN, "LEARN_FROM_OUTCOMES=true: which changes "
                         "were kept, undone or dismissed is recorded and kept"))
    else:
        out.append(Check("learn-from-outcomes", PASS, "LEARN_FROM_OUTCOMES is off: which "
                         "changes were kept or undone is not recorded"))

    # The switches.
    paused = _on(v.get("SERVICE_PAUSED"))
    out.append(Check("kill-switch", WARN if paused else INFO,
                     "SERVICE_PAUSED=true: mail is accepted and held sealed, nothing is reviewed "
                     "or sent" if paused else "SERVICE_PAUSED=false: running. Pausing takes a "
                     "config change and a deploy (docs/it/incident-response.md)"))
    if _on(v.get("WEB_SEARCH_ENABLED")):
        out.append(Check("web-search", FAIL, "WEB_SEARCH_ENABLED=true: a search query written "
                         "from a privileged document discloses part of it"))
    else:
        out.append(Check("web-search", PASS, "WEB_SEARCH_ENABLED is off"))
    geo = str(v.get("INFERENCE_GEO", "") or "").strip().lower()
    out.append(Check("inference-geo", PASS if geo == "us" else WARN,
                     "INFERENCE_GEO=us: model requests are served in the US" if geo == "us" else
                     f"INFERENCE_GEO={geo or 'unset'}: model requests may be served in any "
                     "geography unless the workspace pins a default; set us for one known "
                     "country (there is no EU or UK option)"))
    out.append(Check("zero-retention", INFO,
                     f"ZERO_RETENTION={'true' if _on(v.get('ZERO_RETENTION')) else 'false'}. "
                     "Either way Managed Agents sessions are not zero-data-retention "
                     "(docs/sandbox.md)"))
    notice = str(v.get("PRIVILEGE_NOTICE", "") or "")
    out.append(Check("privilege-notice", PASS if notice.strip() or "PRIVILEGE_NOTICE" not in v
                     else WARN,
                     f"PRIVILEGE_NOTICE: {notice.strip() or 'the default legend'}"
                     if notice.strip() or "PRIVILEGE_NOTICE" not in v
                     else "PRIVILEGE_NOTICE is empty: replies carry no privilege legend"))
    admins = _split(v.get("PLAYBOOK_ADMINS", ""))
    out.append(Check("playbook-admins", PASS if admins else WARN,
                     f"PLAYBOOK_ADMINS: {', '.join(admins)}" if admins else
                     "PLAYBOOK_ADMINS is empty: the allowlist contact, or failing that anyone "
                     "on FIRM_DOMAINS, may change the playbook and add or lift an emailed "
                     "no-AI entry"))
    cap = _int(v.get("MAX_EMAILS_PER_DAY", 0), 0)
    out.append(Check("daily-cap", PASS if cap > 0 else WARN,
                     f"MAX_EMAILS_PER_DAY={cap}" if cap > 0 else
                     "MAX_EMAILS_PER_DAY is unset or 0: no daily cap at the Worker"))
    no_ai = _split(v.get("NO_AI_MATTERS", ""))
    out.append(Check("no-ai-list", INFO, f"NO_AI_MATTERS has {len(no_ai)} configured entr"
                     f"{'y' if len(no_ai) == 1 else 'ies'} (admins may add more by email)"))
    dms = str(v.get("DMS_PROVIDER", "none") or "none").strip().lower()
    out.append(Check("document-system", INFO, "DMS_PROVIDER=none: no OAuth scopes are held"
                     if dms in ("none", "") else f"DMS_PROVIDER={dms}: read-only, per-lawyer "
                     "OAuth (docs/oauth.md)"))

    # The agents.
    missing = [k for k in REQUIRED_IDS if not str(v.get(k, "") or "").strip()]
    out.append(Check("agents-configured", WARN if missing else PASS,
                     f"not set: {', '.join(missing)} (model-backed review is off; the "
                     "mechanical checks still run)" if missing else
                     "the reviewer, associate, environment and firm memory store are set"))
    return out


def _data_key_check(secrets: dict[str, str]) -> Check:
    key = str(secrets.get("DATA_KEY", "") or "").strip()
    if not key:
        return Check("data-key", SKIP, "DATA_KEY is not readable here (a Worker secret cannot "
                     "be read back); give the firm's copy with --env-file, or use --wrangler "
                     "to see that the Worker holds one")
    if key in KNOWN_TEST_KEYS:
        return Check("data-key", FAIL, "DATA_KEY is a test key from this repository; anyone "
                     "with the source can read what it sealed")
    try:
        raw = base64.urlsafe_b64decode(key + "=" * (-len(key) % 4))
    except ValueError:
        return Check("data-key", FAIL, "DATA_KEY is not valid base64")
    if len(raw) != 32:
        return Check("data-key", FAIL, f"DATA_KEY decodes to {len(raw)} bytes, not 32 (AES-256)")
    if len(set(raw)) < 8:
        return Check("data-key", FAIL, "DATA_KEY has almost no variety (a placeholder?); "
                     "generate one with `second-eye keygen`")
    return Check("data-key", PASS, "DATA_KEY is a 32-byte key and not a known test key "
                 "(the key itself is not printed)")


# --------------------------------------------------------------------------
# Network checks
# --------------------------------------------------------------------------

Fetch = Callable[[str], tuple[int, str]]


def http_get(url: str) -> tuple[int, str]:
    """GET without credentials and without following redirects."""
    import httpx

    response = httpx.get(url, timeout=10, follow_redirects=False)
    return response.status_code, response.text[:500]


def edge_checks(v: dict[str, str], fetch: Fetch) -> list[Check]:
    base = str(v.get("EDGE_URL", "") or "").strip().rstrip("/")
    if not base:
        return [Check("edge-health", SKIP, "EDGE_URL is not set")]
    out: list[Check] = []
    try:
        status, body = fetch(f"{base}/health")
        ok = status == 200 and '"edge"' in body and '"ok"' in body
        out.append(Check("edge-health", PASS if ok else FAIL,
                         f"GET {base}/health -> {status} {body.strip()[:60]}"))
    except Exception as e:  # noqa: BLE001 - reported, not raised
        out.append(Check("edge-health", FAIL, f"GET {base}/health failed: {e}"))
        return out
    try:
        status, _ = fetch(f"{base}/internal/db")
        if status in (401, 403):
            out.append(Check("internal-endpoints", WARN,
                             f"GET {base}/internal/db without the secret -> {status}: refused, "
                             "but reachable from the internet behind one bearer secret "
                             "(EDGE_SECRET); moving it off the internet is in progress "
                             "(docs/it/architecture.md)"))
        elif status == 404:
            out.append(Check("internal-endpoints", PASS,
                             f"GET {base}/internal/db -> 404: not served to the internet"))
        else:
            out.append(Check("internal-endpoints", FAIL,
                             f"GET {base}/internal/db without the secret -> {status}"))
    except Exception as e:  # noqa: BLE001
        out.append(Check("internal-endpoints", PASS, f"{base}/internal/db unreachable: {e}"))
    dms = str(v.get("DMS_PROVIDER", "none") or "none").strip().lower()
    if dms in ("none", ""):
        try:
            status, _ = fetch(f"{base}/oauth/callback")
            out.append(Check("oauth-callback", PASS if status == 404 else FAIL,
                             f"GET {base}/oauth/callback -> {status}"
                             + ("" if status == 404 else " (should be closed with no DMS)")))
        except Exception as e:  # noqa: BLE001
            out.append(Check("oauth-callback", WARN, f"could not probe /oauth/callback: {e}"))
    return out


def anthropic_client(api_key: str, workspace: str = ""):
    """A client for read calls only. Not `config.anthropic_client`: that one
    reads this process's settings, and these are the firm's."""
    import anthropic

    kwargs: dict = {"api_key": api_key}
    if workspace.strip():
        kwargs["default_headers"] = {"anthropic-workspace-id": workspace.strip()}
    return anthropic.Anthropic(**kwargs)


def anthropic_checks(v: dict[str, str], secrets: dict[str, str],
                     factory: Callable[..., object] = anthropic_client) -> list[Check]:
    """Each agent, the environment and the firm memory store, retrieved with
    the firm's key. One the key cannot see is in somebody else's
    organisation or workspace. Retrieval only: no session, no model call."""
    key = str(secrets.get("ANTHROPIC_API_KEY", "") or "").strip()
    if not key:
        return [Check("agents-own-org", SKIP, "ANTHROPIC_API_KEY is not readable here; give "
                      "the firm's .env with --env-file to check the agents are the firm's")]
    client = factory(key, str(secrets.get("ANTHROPIC_WORKSPACE_ID", "") or ""))
    targets = [(k, client.beta.agents.retrieve) for k in AGENT_IDS]
    targets += [("MANAGED_ENVIRONMENT_ID", client.beta.environments.retrieve),
                ("MANAGED_FIRM_MEMORY_STORE_ID", client.beta.memory_stores.retrieve)]
    seen, missing, environment = [], [], None
    for setting, retrieve in targets:
        ident = str(v.get(setting, "") or "").strip()
        if not ident:
            continue
        try:
            obj = retrieve(ident)
        except Exception as e:  # noqa: BLE001 - a 404 here is the finding
            missing.append(f"{setting} {ident} ({type(e).__name__})")
            continue
        seen.append(setting)
        if setting == "MANAGED_ENVIRONMENT_ID":
            environment = obj
    out: list[Check] = []
    if missing:
        out.append(Check("agents-own-org", FAIL, "the firm's key cannot see "
                         + "; ".join(missing) + ": not in the firm's organisation/workspace"))
    elif seen:
        out.append(Check("agents-own-org", PASS, f"the firm's key retrieves all {len(seen)} "
                         "configured ids, so they are in the firm's own organisation"))
    else:
        out.append(Check("agents-own-org", SKIP, "no agent ids are set"))
    if environment is not None:
        from secondeye import managed

        net = managed._networking_of(environment)
        kind = str(net.get("type", "") or "")
        hosts = net.get("allowed_hosts") or []
        if kind == "limited" and not hosts:
            out.append(Check("sandbox-egress", PASS, "the environment's networking is limited, "
                             "no allowed hosts" + (", package managers allowed"
                                                   if net.get("allow_package_managers") else "")
                             + (", MCP servers allowed" if net.get("allow_mcp_servers") else "")))
        else:
            out.append(Check("sandbox-egress", FAIL, f"the environment's networking is {net}: "
                             "`second-eye agents apply` sets it to limited"))
    return out


Runner = Callable[[list[str], Path], tuple[int, str]]


def _run(argv: list[str], cwd: Path) -> tuple[int, str]:
    proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout + proc.stderr


def worker_secret_checks(config_arg: str, cwd: Path, runner: Runner = _run) -> list[Check]:
    """The names of the Worker's secrets (`wrangler secret list` never shows
    values). Read-only."""
    code, output = runner(["npx", "wrangler", "secret", "list", "--config", config_arg,
                           "--format", "json"], cwd)
    if code != 0:
        return [Check("worker-secrets", SKIP, "`wrangler secret list` failed (a Cloudflare "
                      f"token for the firm's account is needed): {output.strip()[:120]}")]
    try:
        names = {s["name"] for s in json.loads(output[output.find("["):])}
    except (ValueError, TypeError, KeyError):
        return [Check("worker-secrets", SKIP, "could not read `wrangler secret list` output")]
    wanted = ("DATA_KEY", "EDGE_SECRET", "ANTHROPIC_API_KEY")
    missing = [n for n in wanted if n not in names]
    out = [Check("worker-secrets", FAIL if missing else PASS,
                 f"the Worker lacks {', '.join(missing)}" if missing else
                 "the Worker holds DATA_KEY, EDGE_SECRET and ANTHROPIC_API_KEY")]
    if "ANTHROPIC_WEBHOOK_SIGNING_KEY" not in names:
        out.append(Check("webhook-key", INFO, "no ANTHROPIC_WEBHOOK_SIGNING_KEY: the webhook "
                         "answers 404 and the Workflow polls"))
    return out


# --------------------------------------------------------------------------
# The audit export, read only
# --------------------------------------------------------------------------


def _read_connection():
    """A connection that runs no schema statements: `store.connect()` creates
    every table it might need, which is a write, however harmless."""
    from secondeye.config import settings
    from secondeye.store import is_d1

    if is_d1():
        from secondeye import d1

        return d1.connect()
    path = settings().database_url.replace("sqlite:///", "", 1)
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def audit_check(days: int = 30, connect: Callable[[], object] = _read_connection) -> Check:
    from secondeye import audit

    since = (datetime.now(UTC) - timedelta(days=days)).date().isoformat()
    try:
        conn = connect()
        try:
            cur = conn.execute(f"SELECT {', '.join(audit.COLUMNS)} FROM audit_log "
                               "WHERE received_at >= ? ORDER BY received_at", (since,))
            fetched = cur.fetchall()
        finally:
            with contextlib.suppress(Exception):
                conn.close()
    except Exception as e:  # noqa: BLE001
        text = str(e)
        if "no such table" in text or "unable to open" in text:
            return Check("audit-export", WARN, "no audit trail yet: no message has been "
                         "handled by this deployment")
        return Check("audit-export", FAIL, f"could not read the audit trail: {text[:160]}")
    found = []
    for row in fetched:
        r = dict(zip(audit.COLUMNS, tuple(row), strict=True))
        for k in ("documents", "ran", "session_ids", "reply_to", "returned"):
            try:
                r[k] = json.loads(r[k] or "[]")
            except (TypeError, ValueError):
                r[k] = []
        found.append(r)
    csv_text = audit.as_csv(found)
    header = csv_text.splitlines()[0] if csv_text else ""
    return Check("audit-export", PASS, f"{len(found)} job row(s) since {since} exported as CSV "
                 f"({len(csv_text)} bytes; columns: {header})")


# --------------------------------------------------------------------------
# Putting it together
# --------------------------------------------------------------------------


@dataclass
class Target:
    label: str
    vars: dict[str, str]
    secrets: dict[str, str]
    others: list = None  # other tenants, for the shared-resource check
    tenant: object = None


def _from_settings() -> Target:
    from secondeye.config import Settings, settings

    cfg = settings()
    v: dict[str, str] = {}
    for name in Settings.model_fields:
        value = getattr(cfg, name)
        v[name.upper()] = str(value).lower() if isinstance(value, bool) else str(value)
    # A Worker var the application never reads.
    v["MAX_EMAILS_PER_DAY"] = os.environ.get("MAX_EMAILS_PER_DAY", "")
    secrets = {k: v[k] for k in ("DATA_KEY", "DATA_KEY_PREVIOUS", "EDGE_SECRET",
                                 "ANTHROPIC_API_KEY", "ANTHROPIC_WORKSPACE_ID")}
    for k in secrets:
        v.pop(k, None)
    return Target("this machine's settings", v, secrets)


def _from_tenant(name: str, env_file: str | None, root: Path) -> Target:
    from secondeye import tenant as tenants

    t = tenants.load(name, root)
    secrets = tenants.read_env(Path(env_file) if env_file else t.env_file)
    others = [o for o in tenants.tenants(root) if o.name != t.name]
    return Target(f"tenant {name} ({t.worker})", dict(t.vars), secrets, others, t)


@contextlib.contextmanager
def _database(target: Target) -> Iterator[None]:
    """Settings that point the audit read at the deployment's D1."""
    from secondeye.config import settings

    if target.tenant is None:
        yield
        return
    from secondeye import tenant as tenants

    with tenants.environment(target.tenant):
        os.environ["DATABASE_URL"] = "d1://"
        for key in ("EDGE_SECRET", "DATA_KEY"):
            if target.secrets.get(key):
                os.environ[key] = target.secrets[key]
        settings.cache_clear()
        yield


def run(target: Target, *, offline: bool = False, wrangler: bool = False,
        fetch: Fetch = http_get, factory=anthropic_client, runner: Runner = _run,
        audit_connect: Callable[[], object] | None = None) -> list[Check]:
    checks = config_checks(target.vars, target.secrets)
    if target.tenant is not None:
        from secondeye import tenant as tenants

        shared = [c for c in tenants.collisions([target.tenant, *(target.others or [])])
                  if target.tenant.name in c]
        checks.append(Check("isolation", FAIL if shared else PASS,
                            "; ".join(shared) if shared else
                            f"shares no Worker, database, bucket, Workflow, address, agent or "
                            f"memory store with the {len(target.others or [])} other tenant(s)"))
        if target.tenant.jurisdiction:
            checks.append(Check("jurisdiction", INFO, f"D1 and R2 created with jurisdiction "
                                f"{target.tenant.jurisdiction}"))
        else:
            checks.append(Check("jurisdiction", INFO, "D1 and R2 in Cloudflare's default "
                                "location (no --jurisdiction)"))
    if offline:
        checks.append(Check("network", SKIP, "--offline: Worker, Anthropic and audit checks "
                            "not run"))
        return checks
    checks += edge_checks(target.vars, fetch)
    try:
        checks += anthropic_checks(target.vars, target.secrets, factory)
    except Exception as e:  # noqa: BLE001
        checks.append(Check("agents-own-org", FAIL, f"could not reach Anthropic: {e}"))
    if wrangler and target.tenant is not None:
        checks += worker_secret_checks(target.tenant.config_arg(),
                                       target.tenant.root / "cloudflare", runner)
    if audit_connect is not None:
        checks.append(audit_check(connect=audit_connect))
    elif target.tenant is not None and not target.secrets.get("EDGE_SECRET"):
        checks.append(Check("audit-export", SKIP, "EDGE_SECRET is not readable here; give the "
                            "firm's .env with --env-file to read the audit trail"))
    else:
        with _database(target):
            checks.append(audit_check())
    return checks


def render(target: Target, checks: list[Check]) -> str:
    counts = {s: sum(1 for c in checks if c.status == s) for s in (PASS, WARN, FAIL, INFO, SKIP)}
    lines = [f"Second Eye self-test: {target.label}",
             (f"Run {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}. Read-only: no model "
              "call, no email sent, nothing written."), ""]
    width = max((len(c.name) for c in checks), default=10)
    for c in checks:
        lines.append(f"{c.status:4}  {c.name:{width}}  {c.detail}")
    lines += ["", " ".join(f"{n} {s}" for s, n in counts.items() if n)
              + (": FAILED" if counts[FAIL] else ": no failures")]
    return "\n".join(lines)


def main(argv: list[str], *, root: Path | None = None) -> int:
    from secondeye import tenant as tenants

    parser = argparse.ArgumentParser(prog="second-eye selftest",
                                     description="Check a deployment's security settings, "
                                                 "read-only.")
    parser.add_argument("--tenant", help="a firm's deployment in deployments/")
    parser.add_argument("--env-file", help="the firm's secrets (default deployments/<firm>/.env)")
    parser.add_argument("--offline", action="store_true", help="no network checks")
    parser.add_argument("--wrangler", action="store_true",
                        help="also list the Worker's secret names with wrangler")
    parser.add_argument("--json", action="store_true", help="the checks as JSON")
    try:
        opts = parser.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0) if not isinstance(e.code, str) else 2
    try:
        target = (_from_tenant(opts.tenant, opts.env_file, root or tenants.REPO)
                  if opts.tenant else _from_settings())
    except tenants.TenantError as e:
        print(e)
        return 2
    checks = run(target, offline=opts.offline, wrangler=opts.wrangler)
    if opts.json:
        print(json.dumps({"target": target.label, "checks": [asdict(c) for c in checks]},
                         indent=2))
    else:
        print(render(target, checks))
    return 1 if any(c.status == FAIL for c in checks) else 0


__all__ = ["Check", "anthropic_checks", "audit_check", "config_checks", "edge_checks", "main",
           "run", "worker_admits", "worker_secret_checks"]
