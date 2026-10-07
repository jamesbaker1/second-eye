"""One deployment per firm: `lra tenant`.

A firm's documents live in a deployment of their own (DECISIONS 27): its own
Worker, container, D1 database, R2 bucket, Workflow, address, allowlist,
DATA_KEY, Anthropic agents and firm memory store. Each is described by one
file, `deployments/<firm>/tenant.jsonc`. What every deployment shares (the
Worker's code, its compatibility date, the container class, the cron) is
read from `cloudflare/wrangler.jsonc`, so a change there reaches every firm
on the next deploy.

From the tenant file this renders `deployments/<firm>/wrangler.jsonc`, the
config `wrangler deploy --config` takes. It is generated and committed, so
what a firm runs is in the diff, and a test fails if it falls behind.

The operator's own deployment (Jim's: deployments/jim/, which is private and
not in the public repository) is a tenant like any other, marked
`"primary": true`. Its config is rendered the same way; being primary only
means deploy.yml deploys it first, as the canary, with the unsuffixed
CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID, and that provision and
offboard leave it alone. `cloudflare/wrangler.jsonc` itself carries
placeholders for every name, id and var that belongs to a deployment, so it
names nobody's.

  lra tenant new <firm> --address review@legal.firm.com --domains firm.com
  lra tenant provision <firm>            the wrangler commands, printed
  lra tenant provision <firm> --apply    ... and run
  lra tenant render [<firm>]             regenerate deployments/<firm>/wrangler.jsonc
  lra tenant check                       every tenant rendered, none sharing a resource
  lra tenant list | matrix               the tenants (matrix: JSON for deploy.yml)
  lra tenant url <firm>                  its EDGE_URL (deploy.yml's health check)

Self-hosted (docs/it/self-hosted.md): a firm whose IT runs Redline Desk in
the firm's own Cloudflare account and Anthropic organisation, with no
standing access for us. It is made with `new <firm> --self-hosted
--account-id <theirs>`, is never in deploy.yml's matrix, and is deployed by
the firm from a release:

  lra tenant deploy <firm> --release vX.Y.Z [--assets DIR] [--apply]
  lra tenant support <firm> [--days 3] [--write]   the support token to grant us
  lra tenant offboard <firm> [--apply] [--key-file F]   delete everything, with a
                                         deletion certificate (offboard.py)

This module imports only the standard library at the top: deploy.yml runs
`python3 -m lra.tenant matrix` on a bare runner.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEPLOYMENTS = REPO / "deployments"
BASE = REPO / "cloudflare" / "wrangler.jsonc"
CLOUDFLARE = REPO / "cloudflare"

NAME = re.compile(r"^[a-z][a-z0-9-]{1,30}[a-z0-9]$")

# The vars a new tenant starts from the shared config's values for: how the
# service behaves, not whose it is. Copied into the firm's tenant file once,
# by `tenant new`, and the firm's from then on. Every other var in
# cloudflare/wrangler.jsonc is the firm's own and starts empty, so a var
# added there later is never inherited by accident (an agent id, a memory
# store, a sender list). The rendered config never reads the shared vars at
# all: a tenant's vars are its own.
KNOBS = ("MAIL_AGENT_NAME", "TRIAGE", "MAX_EMAILS_PER_DAY", "REVIEW_MODEL", "ZERO_RETENTION",
         "THREAD_RETENTION_DAYS", "RETENTION_HOURS", "LOG_LEVEL")
# Set for every new firm whatever the shared config says: the safe value.
FIRM_DEFAULTS = {
    # Needs subaddressing proved on the firm's zone first (docs/deploy-cloudflare.md).
    "ONE_TAP_LINKS": "false",
    "REPLY_POLICY": "sender_only",
    "SERVICE_PAUSED": "false",
    # Nothing learned unless a lawyer asks; the firm turns it on, not us.
    "LEARN_FROM_OUTCOMES": "false",
}
# Not in the shared vars, and the firm's to fill (docs/onboarding/README.md, section 2).
FIRM_EXTRAS = ("ALLOWLIST_CONTACT", "PLAYBOOK_ADMINS", "NO_AI_MATTERS")
# What two deployments may never share. An agent, environment or memory store
# id shared between firms is one firm reading the other's memory.
EXCLUSIVE_VARS = ("EDGE_URL", "MAIL_AGENT_ADDRESS", "MANAGED_FIRM_MEMORY_STORE_ID",
                  "MANAGED_ENVIRONMENT_ID", "MANAGED_REVIEW_AGENT_ID",
                  "MANAGED_ASSOCIATE_AGENT_ID", "MANAGED_PLAYBOOK_AGENT_ID",
                  "MANAGED_COMMENTS_AGENT_ID", "MANAGED_CLOSING_AGENT_ID",
                  "MANAGED_BLACKLINE_AGENT_ID")
# Worker secrets, put once by `provision` and never stored in the repository
# or in GitHub.
GENERATED_SECRETS = ("DATA_KEY", "EDGE_SECRET")
OPERATOR_SECRETS = ("ANTHROPIC_API_KEY", "ANTHROPIC_WORKSPACE_ID")


# --------------------------------------------------------------------------
# JSONC
# --------------------------------------------------------------------------


def parse_jsonc(text: str) -> dict:
    """JSON with // and /* */ comments and trailing commas, as wrangler reads
    it. Comment markers inside strings (a URL's //) are left alone."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            out.append(c)
            i += 1
    return json.loads(re.sub(r",(\s*[}\]])", r"\1", "".join(out)))


def set_field(text: str, key: str, value: str) -> str:
    """`"key": "..."` set to value on the first line that has it, every
    comment and other line as it was."""
    pattern = re.compile(rf'^(\s*"{re.escape(key)}"\s*:\s*)"[^"\n]*"', re.MULTILINE)
    if not pattern.search(text):
        raise KeyError(key)
    return pattern.sub(lambda m: f'{m.group(1)}{json.dumps(value)}', text, count=1)


# --------------------------------------------------------------------------
# A tenant
# --------------------------------------------------------------------------


class TenantError(Exception):
    pass


@dataclass
class Tenant:
    name: str
    dir: Path
    data: dict
    root: Path = field(default=REPO)

    @property
    def file(self) -> Path:
        return self.dir / "tenant.jsonc"

    @property
    def primary(self) -> bool:
        """The operator's own deployment (Jim's): deployed first by deploy.yml,
        as the canary, with the unsuffixed secrets; never provisioned or
        offboarded by these commands."""
        return bool(self.data.get("primary"))

    @property
    def config_path(self) -> Path:
        return self.dir / "wrangler.jsonc"

    @property
    def vars_file(self) -> Path:
        """Where this tenant's vars, and so its agent ids, are written."""
        return self.file

    @property
    def env_file(self) -> Path:
        """The operator's local settings for this tenant: its Anthropic key,
        and the DATA_KEY `provision` generated. Ignored by git (.env)."""
        return self.dir / ".env"

    @property
    def vars(self) -> dict[str, str]:
        return dict(self.data.get("vars", {}))

    def get(self, key: str) -> str:
        return str(self.data.get(key) or "")

    @property
    def worker(self) -> str:
        return self.get("worker")

    @property
    def jurisdiction(self) -> str:
        return self.get("jurisdiction")

    @property
    def account_id(self) -> str:
        return self.get("account_id")

    @property
    def secret_suffix(self) -> str:
        """`CLOUDFLARE_API_TOKEN_<suffix>` and `CLOUDFLARE_ACCOUNT_ID_<suffix>`
        in GitHub. The primary deployment keeps the unsuffixed pair."""
        return "" if self.primary else self.name.upper().replace("-", "_")

    @property
    def provisioned(self) -> bool:
        return bool(self.get("d1_database_id"))

    @property
    def self_hosted(self) -> bool:
        """In the firm's own Cloudflare account and Anthropic organisation,
        deployed by the firm from a release; never by deploy.yml."""
        return bool(self.data.get("self_hosted"))

    def config_arg(self) -> str:
        """--config as wrangler is run from cloudflare/."""
        return os.path.relpath(self.config_path, self.root / "cloudflare")


def load(name: str, root: Path = REPO) -> Tenant:
    path = root / "deployments" / name / "tenant.jsonc"
    if not path.exists():
        raise TenantError(f"no tenant {name!r}: {path} does not exist "
                          f"(`lra tenant new {name} ...` makes one)")
    return Tenant(name, path.parent, parse_jsonc(path.read_text()), root)


def tenants(root: Path = REPO) -> list[Tenant]:
    base = root / "deployments"
    if not base.is_dir():
        return []
    return [load(p.parent.name, root) for p in sorted(base.glob("*/tenant.jsonc"))]


def primary_tenant(root: Path = REPO) -> Tenant | None:
    """The operator's own deployment, if this checkout has one (the public
    repository does not)."""
    return next((t for t in tenants(root) if t.primary), None)


# --------------------------------------------------------------------------
# Rendering the wrangler config
# --------------------------------------------------------------------------

_PATH_KEYS = ("$schema", "main")


def _relocate(value: str, frm: Path, to: Path) -> str:
    return os.path.relpath((frm / value).resolve(), to.resolve())


def render(t: Tenant, base: Path | None = None) -> dict:
    """The tenant's wrangler config: the shared parts of cloudflare/wrangler.jsonc,
    with every name, id and binding target the tenant's own, its vars, and
    every path made relative to where the config is written."""
    base = base or t.root / "cloudflare" / "wrangler.jsonc"
    shared = parse_jsonc(base.read_text())
    frm, to = base.parent, t.config_path.parent
    out: dict = {}
    for key, value in shared.items():
        out[key] = value
        if key == "name":
            out["name"] = t.worker
            if t.account_id:
                out["account_id"] = t.account_id
    for key in _PATH_KEYS:
        if key in out:
            out[key] = _relocate(out[key], frm, to)

    [container] = out["containers"]
    container = dict(container)
    for key in ("image", "image_build_context"):
        if key in container:
            container[key] = _relocate(container[key], frm, to)
    container["instance_type"] = t.get("instance_type") or container["instance_type"]
    out["containers"] = [container]

    [db] = out["d1_databases"]
    out["d1_databases"] = [{"binding": db["binding"], "database_name": t.get("d1_database_name"),
                            "database_id": t.get("d1_database_id")}]
    [bucket] = out["r2_buckets"]
    out["r2_buckets"] = [{"binding": bucket["binding"], "bucket_name": t.get("r2_bucket")}
                         | ({"jurisdiction": t.jurisdiction} if t.jurisdiction else {})]
    [flow] = out["workflows"]
    out["workflows"] = [{**flow, "name": t.get("workflow")}]
    out["vars"] = t.vars
    return out


PRIMARY_HEADER = """\
// Generated by `lra tenant render {name}` from deployments/{name}/tenant.jsonc
// and the shared parts of cloudflare/wrangler.jsonc. Do not edit: change the
// tenant file (or the shared config) and render again. The primary
// deployment: deployed first by .github/workflows/deploy.yml, with the
// GitHub secrets CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID.
"""

HEADER = """\
// Generated by `lra tenant render {name}` from deployments/{name}/tenant.jsonc
// and the shared parts of cloudflare/wrangler.jsonc. Do not edit: change the
// tenant file (or the shared config) and render again. Deployed by
// .github/workflows/deploy.yml with the GitHub secrets
// CLOUDFLARE_API_TOKEN_{suffix} and CLOUDFLARE_ACCOUNT_ID_{suffix}.
"""

SELF_HOSTED_HEADER = """\
// Generated by `lra tenant render {name}` from deployments/{name}/tenant.jsonc
// and the shared parts of cloudflare/wrangler.jsonc. Do not edit: change the
// tenant file and render again. Self-hosted: deployed by the firm's IT, in
// the firm's own Cloudflare account, from a release (`lra tenant deploy {name}
// --release vX.Y.Z`, docs/it/self-hosted.md). Never by our deploy.yml.
"""


def render_text(t: Tenant) -> str:
    header = (SELF_HOSTED_HEADER if t.self_hosted else PRIMARY_HEADER if t.primary
              else HEADER)
    return (header.format(name=t.name, suffix=t.secret_suffix)
            + json.dumps(render(t), indent=2) + "\n")


def write(t: Tenant) -> Path:
    """Write the rendered config."""
    text = render_text(t)
    if not t.config_path.exists() or t.config_path.read_text() != text:
        t.config_path.write_text(text)
    return t.config_path


def problems(t: Tenant) -> list[str]:
    """What is wrong with one tenant as committed."""
    found: list[str] = []
    for key in ("worker", "d1_database_name", "r2_bucket", "workflow"):
        if not t.get(key):
            found.append(f"{t.name}: {key} is empty")
    if t.self_hosted and not t.account_id:
        found.append(f"{t.name}: self-hosted, so account_id (the firm's Cloudflare account) "
                     "must be set")
    v = t.vars
    for key in ("MAIL_AGENT_ADDRESS", "FIRM_DOMAINS", "ALLOWED_SENDERS"):
        if not str(v.get(key, "")).strip():
            found.append(f"{t.name}: vars.{key} is empty")
    if not t.config_path.exists():
        found.append(f"{t.name}: {t.config_path} is missing; `lra tenant render {t.name}`")
    elif t.config_path.read_text() != render_text(t):
        found.append(f"{t.name}: {t.config_path} is out of date; `lra tenant render {t.name}`")
    return found


def collisions(ts: list[Tenant]) -> list[str]:
    """Anything two deployments share that would put one firm's data, mail or
    memory in the other's."""
    seen: dict[tuple[str, str], str] = {}
    found: list[str] = []
    for t in ts:
        v = t.vars
        owned = [("worker", t.worker), ("D1 database", t.get("d1_database_name")),
                 ("D1 database id", t.get("d1_database_id")), ("R2 bucket", t.get("r2_bucket")),
                 ("Workflow", t.get("workflow"))]
        owned += [(key, str(v.get(key, ""))) for key in EXCLUSIVE_VARS]
        for what, value in owned:
            if not value:
                continue
            if (what, value) in seen and seen[(what, value)] != t.name:
                found.append(f"{t.name} and {seen[(what, value)]} share {what} {value}")
            seen.setdefault((what, value), t.name)
    return found


# --------------------------------------------------------------------------
# lra tenant new
# --------------------------------------------------------------------------

SELF_HOSTED_NOTE = """\
// Self-hosted: the Cloudflare account and the Anthropic organisation are the
// firm's own, the firm's IT deploys a release when it chooses (`lra tenant
// deploy {name} --release vX.Y.Z`), and we have no standing access to
// either (docs/it/self-hosted.md).
"""

TENANT_HEADER = """\
// The {name} deployment (`lra tenant`, docs/deploy-cloudflare.md, "One
// deployment per firm"). Everything here is the firm's alone: its Worker,
// container, D1 database, R2 bucket, Workflow, address and vars. What every
// deployment shares is in cloudflare/wrangler.jsonc, whose comments say what
// each var does. After editing: `lra tenant render {name}` and commit both.
// Secrets are never here: DATA_KEY, EDGE_SECRET and ANTHROPIC_API_KEY are
// put on the Worker by `lra tenant provision {name} --apply`.
"""


def _split(values: str) -> list[str]:
    return [v.strip().lower() for v in values.split(",") if v.strip()]


def new(name: str, *, address: str, domains: str, senders: str = "", account_id: str = "",
        worker: str = "", jurisdiction: str = "", instance_type: str = "standard-1",
        edge_url: str = "", contact: str = "", admins: str = "", self_hosted: bool = False,
        anthropic_org: str = "", anthropic_workspace: str = "",
        root: Path = REPO) -> Tenant:
    if not NAME.match(name):
        raise TenantError(f"{name!r}: a tenant is lower-case letters, digits and hyphens, "
                          "3 to 32 characters, starting with a letter")
    where = root / "deployments" / name
    if (where / "tenant.jsonc").exists():
        raise TenantError(f"{where / 'tenant.jsonc'} already exists")
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", address):
        raise TenantError(f"--address {address!r} is not an email address")
    firm_domains = _split(domains)
    if not firm_domains:
        raise TenantError("--domains names at least one of the firm's mail domains")
    allowed = _split(senders) or firm_domains
    if jurisdiction not in ("", "eu"):
        raise TenantError("--jurisdiction is eu or nothing")
    if self_hosted and not re.match(r"^[0-9a-f]{32}$", account_id):
        raise TenantError("--self-hosted needs --account-id: the firm's own Cloudflare account "
                          "id (32 hex characters, from the dashboard's Account home)")
    if (anthropic_org or anthropic_workspace) and not self_hosted:
        raise TenantError("--anthropic-org and --anthropic-workspace are for --self-hosted")
    worker = worker or f"legal-review-agent-{name}"

    jim = parse_jsonc((root / "cloudflare" / "wrangler.jsonc").read_text())["vars"]
    vars_: dict[str, str] = {}
    for key, value in jim.items():
        vars_[key] = value if key in KNOBS else FIRM_DEFAULTS.get(key, "")
    for key in FIRM_EXTRAS:
        vars_.setdefault(key, "")
    vars_.update({
        "EDGE_URL": edge_url,
        "MAIL_AGENT_ADDRESS": address.lower(),
        "FIRM_DOMAINS": ",".join(firm_domains),
        "ALLOWED_SENDERS": ",".join(allowed),
        "ALLOWLIST_CONTACT": contact.lower(),
        "PLAYBOOK_ADMINS": ",".join(_split(admins)),
    })
    data = {
        "worker": worker,
        "account_id": account_id,
        "instance_type": instance_type,
        "d1_database_name": worker,
        # Printed by `wrangler d1 create`; `provision --apply` writes it here.
        "d1_database_id": "",
        "r2_bucket": f"{worker}-docs",
        "jurisdiction": jurisdiction,
        "workflow": f"legal-review-flow-{name}",
        "vars": vars_,
    }
    header = TENANT_HEADER.format(name=name)
    if self_hosted:
        # Recorded, not secret: which organisation and workspace the firm's
        # key belongs to, so offboarding and support name the right ones.
        data = {"self_hosted": True, **data, "anthropic_organization": anthropic_org,
                "anthropic_workspace": anthropic_workspace}
        header += SELF_HOSTED_NOTE.format(name=name)
    where.mkdir(parents=True, exist_ok=True)
    (where / "tenant.jsonc").write_text(header + json.dumps(data, indent=2) + "\n")
    t = load(name, root)
    write(t)
    return t


# --------------------------------------------------------------------------
# lra tenant provision
# --------------------------------------------------------------------------


@dataclass
class Step:
    title: str
    argv: list[str] = field(default_factory=list)
    # Run by hand, never by --apply: it changes the firm's mail or DNS, or
    # needs a person at a console.
    manual: bool = False
    note: str = ""
    # The name of a secret piped to the command on stdin, never printed.
    stdin: str = ""
    # What --apply does with the output (see _after).
    after: str = ""


def plan(t: Tenant) -> list[Step]:
    cfg = t.config_arg()
    db, bucket = t.get("d1_database_name"), t.get("r2_bucket")
    j = ["--jurisdiction", t.jurisdiction] if t.jurisdiction else []
    address = str(t.vars.get("MAIL_AGENT_ADDRESS", ""))
    subdomain = address.partition("@")[2] or "<subdomain>"
    suffix = t.secret_suffix
    steps = [
        Step("The D1 database (its id is written into tenant.jsonc)",
             ["npx", "wrangler", "d1", "create", db, *j], after="d1_id",
             note="skipped: tenant.jsonc already has its id" if t.provisioned else ""),
        Step("The R2 bucket for documents",
             ["npx", "wrangler", "r2", "bucket", "create", bucket, *j], after="exists_ok"),
        Step("A message never reviewed is deleted after 7 days",
             ["npx", "wrangler", "r2", "bucket", "lifecycle", "add", bucket,
              "expire-unreviewed-mail", "inbound/", "--expire-days", "7", "-y", *j],
             after="exists_ok"),
        Step("Review state is deleted after 7 days even if the cron fails",
             ["npx", "wrangler", "r2", "bucket", "lifecycle", "add", bucket,
              "expire-job-state", "job/", "--expire-days", "7", "-y", *j], after="exists_ok"),
        Step("The Worker alone, so secrets have somewhere to go (the container rolls out "
             + ("with `lra tenant deploy`)" if t.self_hosted else "from deploy.yml)"),
             ["npx", "wrangler", "deploy", "--config", cfg, "--containers-rollout=none"],
             after="edge_url"),
    ]
    for name in GENERATED_SECRETS:
        steps.append(Step(f"{name}, generated here and kept in {_rel(t.env_file, t.root)}",
                          ["npx", "wrangler", "secret", "put", name, "--config", cfg],
                          stdin=name, after="secret"))
    for name in OPERATOR_SECRETS:
        steps.append(Step(f"{name}, from {_rel(t.env_file, t.root)} (the firm's own Anthropic "
                          "organisation)",
                          ["npx", "wrangler", "secret", "put", name, "--config", cfg],
                          stdin=name, after="secret",
                          note="optional: only for a key not scoped to a workspace"
                          if name == "ANTHROPIC_WORKSPACE_ID" else ""))
    steps += [
        Step("Email sending: SPF and DKIM under the agent's subdomain only",
             ["npx", "wrangler", "email", "sending", "enable", subdomain], manual=True,
             note=("By hand: check `dig MX " + subdomain + "` first. If the domain already "
                   "receives mail elsewhere, Email Routing takes it over; use a subdomain. "
                   "The zone may be in the firm's account. Then `npx wrangler email sending "
                   f"dns get {subdomain}` for the records.")),
        Step("Email routing: the agent's address to this Worker",
             ["npx", "wrangler", "email", "routing", "rules", "create", subdomain,
              "--name", f"legal review agent ({t.name})", "--match-type", "literal",
              "--match-field", "to", "--match-value", address or "review@<subdomain>",
              "--action-type", "worker", "--action-value", t.worker], manual=True,
             note=("By hand, after sending is enabled: it routes the firm's mail. Pass the "
                   "zone (the registered domain) if the subdomain is not its own zone. Turn "
                   "on subaddressing in the zone's Email Routing settings before "
                   "ONE_TAP_LINKS=true.")),
        Step("The webhook signing key",
             ["npx", "wrangler", "secret", "put", "ANTHROPIC_WEBHOOK_SIGNING_KEY",
              "--config", cfg], manual=True,
             note=("By hand: in the firm's Anthropic Console, Manage -> Webhooks, add "
                   f"{t.vars.get('EDGE_URL') or '<EDGE_URL>'}/anthropic/webhook "
                   "(session.status_idled, session.status_terminated), and paste the whsec_ "
                   "secret. Without it the Workflow polls every minute.")),
        Step("The agents, environment and memory store, in the firm's Anthropic organisation",
             ["lra", "live-check", "--tenant", t.name], manual=True,
             note=(f"By hand, with the firm's ANTHROPIC_API_KEY in {_rel(t.env_file, t.root)}"
                   ": skills and agents in the firm's organisation, every id written into "
                   "tenant.jsonc and the config rendered, then the evals within "
                   "--budget-usd. `--dry-run` first spends nothing.")),
    ]
    if t.self_hosted:
        steps.append(Step("A release, deployed by the firm",
                          ["lra", "tenant", "deploy", t.name, "--release", "<vX.Y.Z>"],
                          manual=True,
                          note=("By the firm's IT, from the unpacked release, with its own "
                                "CLOUDFLARE_API_TOKEN: a dry run, then --apply "
                                "(docs/it/self-hosted.md). deploy.yml never deploys it.")))
    else:
        steps.append(Step("GitHub secrets for deploy.yml",
                          ["gh", "secret", "set", f"CLOUDFLARE_API_TOKEN_{suffix}"], manual=True,
                          note=(f"By hand: CLOUDFLARE_API_TOKEN_{suffix} and "
                                f"CLOUDFLARE_ACCOUNT_ID_{suffix} (the account the tenant's "
                                "resources are in). Until both are set, deploy.yml skips this "
                                f"tenant and stays green. Then commit deployments/{t.name}/ "
                                "and push.")))
    return steps


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _env_prefix(t: Tenant) -> str:
    return f"CLOUDFLARE_ACCOUNT_ID={t.account_id} " if t.account_id else ""


def shell(t: Tenant, step: Step) -> str:
    import shlex

    line = " ".join(shlex.quote(a) for a in step.argv)
    if step.argv[:1] == ["npx"]:
        line = f"(cd cloudflare && {_env_prefix(t)}{line})"
    if step.stdin:
        line = f"<{step.stdin}> | {line}"
    return line


def read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if m and not line.lstrip().startswith("#"):
            out[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return out


def _append_env(path: Path, key: str, value: str) -> None:
    text = path.read_text() if path.exists() else ""
    if text and not text.endswith("\n"):
        text += "\n"
    path.write_text(text + f"{key}={value}\n")
    path.chmod(0o600)


# A string alias, so the module still imports on the python3 of a bare runner.
Runner = Callable[[list, Path, dict, "str | None"], tuple]


def run_command(argv: list[str], cwd: Path, env: dict[str, str],
                stdin: str | None) -> tuple[int, str]:
    proc = subprocess.run(argv, cwd=cwd, env={**os.environ, **env}, input=stdin,
                          capture_output=True, text=True, check=False)
    output = proc.stdout + proc.stderr
    print(output, end="" if output.endswith("\n") else "\n")
    return proc.returncode, output


BACKUP = """\
DATA_KEY for {name} is in {env} (mode 600), and on the Worker.
Back it up now, before any document is stored: it is the only key to them.
  1. Put it in the firm's own secret store (their password manager or KMS),
     under "Redline Desk DATA_KEY ({worker})". The firm holds it (DECISIONS 13).
  2. Keep a second copy offline (a sealed note or a hardware key) with ours.
  3. Then remove it from {env} unless you run `lra clients` or `lra purge`
     against the firm's database from this machine; the Worker has its own copy.
Lose every copy and every stored document is noise. Never paste it into
GitHub: deploy.yml does not need it."""


def provision(t: Tenant, *, apply: bool = False, runner: Runner = run_command,
              keygen: Callable[[], str] | None = None,
              secret_gen: Callable[[], str] | None = None) -> int:
    if t.primary:
        print(f"{t.name} is the primary deployment; its resources exist. "
              "Nothing to provision.")
        return 2
    bad = [p for p in problems(t) if "out of date" not in p and "missing" not in p]
    if bad:
        print("Fix the tenant first:\n  " + "\n  ".join(bad))
        return 2
    steps = plan(t)
    cwd = t.root / "cloudflare"
    print(f"Provisioning {t.name}: Worker {t.worker}, D1 {t.get('d1_database_name')}, "
          f"R2 {t.get('r2_bucket')}, Workflow {t.get('workflow')}"
          + (f", jurisdiction {t.jurisdiction}" if t.jurisdiction else "")
          + (f", account {t.account_id}" if t.account_id else ", the account of your "
             "CLOUDFLARE_API_TOKEN") + ".\n")
    if not apply:
        for i, step in enumerate(steps, 1):
            print(f"# {i}. {step.title}" + (" [by hand]" if step.manual else ""))
            if step.note:
                print(f"#    {step.note}")
            print(f"{shell(t, step)}\n")
        print("Dry run: nothing was created or changed. `lra tenant provision "
              f"{t.name} --apply` runs the commands not marked [by hand], in this order, "
              "stopping at the first that fails. Run it with CLOUDFLARE_API_TOKEN for "
              "the firm's account.")
        return 0

    env = {"CLOUDFLARE_ACCOUNT_ID": t.account_id} if t.account_id else {}
    local = read_env(t.env_file)
    existing: set[str] = set()
    generated_key = False
    manual: list[tuple[int, Step]] = []
    for i, step in enumerate(steps, 1):
        if step.manual:
            manual.append((i, step))
            continue
        if step.after == "d1_id" and t.provisioned:
            print(f"# {i}. {step.title}: {step.note}")
            continue
        stdin: str | None = None
        if step.stdin:
            if step.stdin in existing:
                print(f"# {i}. {step.stdin} is already set on {t.worker}; not replaced"
                      + (" (a new DATA_KEY would make every stored document unreadable)"
                         if step.stdin == "DATA_KEY" else ""))
                continue
            stdin = local.get(step.stdin, "")
            if not stdin and step.stdin in GENERATED_SECRETS:
                if step.stdin == "DATA_KEY":
                    stdin = (keygen or _keygen)()
                    generated_key = True
                else:
                    stdin = (secret_gen or _secret)()
                _append_env(t.env_file, step.stdin, stdin)
                local[step.stdin] = stdin
            if not stdin:
                print(f"# {i}. {step.stdin} is not in {_rel(t.env_file, t.root)}; "
                      + ("skipped (optional)." if step.note else
                         f"put it by hand: {shell(t, step)}"))
                if not step.note:
                    manual.append((i, step))
                continue
        print(f"# {i}. {step.title}\n$ {shell(t, step)}")
        code, output = runner(step.argv, cwd, env, None if stdin is None else stdin + "\n")
        if code != 0 and not (step.after == "exists_ok" and "already exists" in output.lower()):
            print(f"\nStopped at step {i}: `{shell(t, step)}` failed (exit {code}). Fix it "
                  f"and run `lra tenant provision {t.name} --apply` again: every step "
                  "before this one is safe to repeat.")
            return 1
        if step.after == "d1_id":
            m = re.search(r'"?database_id"?\s*[:=]\s*"([0-9a-f-]{36})"', output)
            if not m:
                print("\nwrangler did not print a database_id; put it in "
                      f"{_rel(t.file, t.root)} by hand and run this again.")
                return 1
            _update(t, "d1_database_id", m.group(1))
        if step.after == "edge_url" and not t.vars.get("EDGE_URL"):
            m = re.search(r"https://[a-z0-9.-]+\.workers\.dev", output)
            if m:
                _update(t, "EDGE_URL", m.group(0))
        if step.after == "edge_url":
            # The Worker exists now: what it already holds is never overwritten.
            code, listed = runner(["npx", "wrangler", "secret", "list", "--config",
                                   t.config_arg(), "--format", "json"], cwd, env, None)
            if code == 0:
                with contextlib.suppress(ValueError, TypeError):
                    existing = {s["name"] for s in json.loads(listed[listed.find("["):])}
    t = load(t.name, t.root)
    write(t)
    print(f"\nDone. {_rel(t.file, t.root)} and {_rel(t.config_path, t.root)} are updated: "
          "commit them.")
    if generated_key:
        print("\n" + BACKUP.format(name=t.name, env=_rel(t.env_file, t.root), worker=t.worker))
    if manual:
        print("\nStill to do by hand:")
        for i, step in manual:
            print(f"# {i}. {step.title}")
            if step.note:
                print(f"#    {step.note}")
            print(f"{shell(t, step)}\n")
    return 0


def _update(t: Tenant, key: str, value: str) -> None:
    t.file.write_text(set_field(t.file.read_text(), key, value))
    fresh = load(t.name, t.root)
    t.data = fresh.data
    write(t)


def _keygen() -> str:
    from lra import crypto

    return crypto.generate()


def _secret() -> str:
    import secrets

    return secrets.token_urlsafe(48)


# --------------------------------------------------------------------------
# The tenant's environment, for `lra live-check --tenant`
# --------------------------------------------------------------------------


@contextlib.contextmanager
def environment(t: Tenant) -> Iterator[None]:
    """Settings as the tenant's deployment sees them, and nothing of anyone
    else's: every setting the shell or the working directory's .env holds is
    hidden, the tenant's .env is read in its place, and its vars (the agent
    ids above all) are set from tenant.jsonc. Without this, `lra agents apply`
    for a firm would update whatever agent Jim's .env names, with whichever
    key the shell exported."""
    from lra.config import Settings, settings

    saved_env = dict(os.environ)
    saved_file = Settings.model_config.get("env_file")
    try:
        for name in Settings.model_fields:
            os.environ.pop(name.upper(), None)
        for key, value in t.vars.items():
            os.environ[key] = str(value)
        Settings.model_config["env_file"] = str(t.env_file)
        settings.cache_clear()
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
        Settings.model_config["env_file"] = saved_file
        settings.cache_clear()


# --------------------------------------------------------------------------
# deploy.yml
# --------------------------------------------------------------------------


def matrix(root: Path = REPO) -> list[dict[str, str]]:
    """The tenants deploy.yml deploys after the primary one: every one not
    primary and not self-hosted. A self-hosted firm deploys a release itself,
    in its own account, with a token we never hold."""
    return [{"tenant": t.name, "config": _rel(t.config_path, root),
             "secret": t.secret_suffix, "edge_url": str(t.vars.get("EDGE_URL", "")),
             "provisioned": "true" if t.provisioned else "false"}
            for t in tenants(root) if not t.primary and not t.self_hosted]


def self_hosted_notice(root: Path = REPO) -> list[str]:
    """What deploy.yml prints for each firm it does not deploy."""
    return [f"{t.name}: self-hosted, so not deployed here. The firm's IT deploys a release "
            f"in its own Cloudflare account ({t.account_id}) with `lra tenant deploy "
            f"{t.name} --release vX.Y.Z` (docs/it/self-hosted.md)."
            for t in tenants(root) if t.self_hosted]


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main(argv: list[str], *, root: Path = REPO, runner: Runner = run_command) -> int:
    parser = argparse.ArgumentParser(prog="lra tenant",
                                     description="One deployment per firm (deployments/).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("new", help="write deployments/<firm>/tenant.jsonc and its config")
    p.add_argument("firm")
    p.add_argument("--address", required=True, help="the agent's address, review@legal.firm.com")
    p.add_argument("--domains", required=True, help="the firm's mail domains, comma separated")
    p.add_argument("--senders", default="",
                   help="ALLOWED_SENDERS, comma separated (default: the firm's domains)")
    p.add_argument("--account-id", default="", help="the firm's Cloudflare account, if not ours")
    p.add_argument("--worker", default="", help="the Worker name (default legal-review-agent-<firm>)")
    p.add_argument("--jurisdiction", default="", choices=["", "eu"],
                   help="eu keeps the database and documents in the EU")
    p.add_argument("--instance-type", default="standard-1")
    p.add_argument("--edge-url", default="", help="the Worker URL, if known (provision fills it)")
    p.add_argument("--contact", default="", help="ALLOWLIST_CONTACT")
    p.add_argument("--admins", default="", help="PLAYBOOK_ADMINS, comma separated")
    p.add_argument("--self-hosted", action="store_true",
                   help="in the firm's own Cloudflare account and Anthropic organisation, "
                        "deployed by the firm from a release; needs --account-id")
    p.add_argument("--anthropic-org", default="",
                   help="the firm's Anthropic organisation id (self-hosted; for the record)")
    p.add_argument("--anthropic-workspace", default="",
                   help="the firm's Anthropic workspace id (self-hosted; for the record)")
    p = sub.add_parser("provision", help="create the tenant's Cloudflare resources")
    p.add_argument("firm")
    p.add_argument("--apply", action="store_true",
                   help="run the wrangler commands (default: print them)")
    p = sub.add_parser("render", help="regenerate deployments/<firm>/wrangler.jsonc")
    p.add_argument("firm", nargs="?")
    sub.add_parser("check", help="every tenant rendered, valid, and sharing nothing")
    sub.add_parser("list", help="the tenants")
    sub.add_parser("matrix", help="the tenants deploy.yml deploys, as JSON")
    sub.add_parser("self-hosted", help="the tenants deploy.yml does not deploy, and why")
    p = sub.add_parser("url", help="the tenant's EDGE_URL, for a health check")
    p.add_argument("firm")
    p = sub.add_parser("deploy", help="deploy a release to a self-hosted firm (run by the firm)")
    p.add_argument("firm")
    p.add_argument("--release", required=True, help="the release version, vX.Y.Z")
    p.add_argument("--assets", type=Path, help="the downloaded release files, to check "
                   "against SHA256SUMS first")
    p.add_argument("--apply", action="store_true", help="deploy (default: print the steps)")
    p = sub.add_parser("support", help="the scoped, expiring support token a firm grants us")
    p.add_argument("firm")
    p.add_argument("--days", type=int, default=3, help="how long it lasts (default 3, at most 14)")
    p.add_argument("--write", action="store_true",
                   help="also let us deploy and change settings (default: read only)")
    p = sub.add_parser("offboard", help="delete everything of a firm's, with a certificate")
    p.add_argument("firm")
    p.add_argument("--apply", action="store_true", help="delete (default: list what would go)")
    p.add_argument("--key-file", type=Path,
                   help="a key the firm supplies: the certificate is HMAC-SHA256 signed with it")
    p.add_argument("--by", help="who is running it, for the certificate (default: login name)")
    p = sub.add_parser("certificate", help="check a deletion certificate")
    p.add_argument("file", type=Path)
    p.add_argument("--key-file", type=Path, help="the firm's key, to check the HMAC too")
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0) if not isinstance(e.code, str) else 2

    try:
        if args.cmd == "new":
            t = new(args.firm, address=args.address, domains=args.domains, senders=args.senders,
                    account_id=args.account_id, worker=args.worker,
                    jurisdiction=args.jurisdiction, instance_type=args.instance_type,
                    edge_url=args.edge_url, contact=args.contact, admins=args.admins,
                    self_hosted=args.self_hosted, anthropic_org=args.anthropic_org,
                    anthropic_workspace=args.anthropic_workspace, root=root)
            print(f"wrote {_rel(t.file, root)} and {_rel(t.config_path, root)}.\n"
                  f"Next: read the vars there, then `lra tenant provision {t.name}` "
                  "(a dry run) and, when it reads right, `--apply`."
                  + ("\nSelf-hosted: the firm's IT runs provision and every deploy with its "
                     "own token, and deploy.yml skips it (docs/it/self-hosted.md)."
                     if t.self_hosted else ""))
            return 0
        if args.cmd == "provision":
            return provision(load(args.firm, root), apply=args.apply, runner=runner)
        if args.cmd == "render":
            chosen = [load(args.firm, root)] if args.firm else tenants(root)
            for t in chosen:
                print(f"{t.name}: wrote {_rel(write(t), root)}")
            return 0
        if args.cmd == "check":
            ts = tenants(root)
            found = [p for t in ts for p in problems(t)] + collisions(ts)
            for line in found:
                print(line)
            print(f"{len(ts)} tenant(s); " + ("all good." if not found else
                                              f"{len(found)} problem(s)."))
            return 1 if found else 0
        if args.cmd == "list":
            for t in tenants(root):
                state = ("primary, deployed first" if t.primary else
                         "provisioned" if t.provisioned else "not provisioned")
                if t.self_hosted:
                    state += ", self-hosted: the firm deploys"
                print(f"{t.name:16} {t.worker:32} {t.vars.get('MAIL_AGENT_ADDRESS', '')}"
                      f"  ({state})")
            return 0
        if args.cmd == "matrix":
            rows = matrix(root)
            print(json.dumps({"include": rows}) if rows else "")
            return 0
        if args.cmd == "url":
            url = str(load(args.firm, root).vars.get("EDGE_URL", "")).rstrip("/")
            if not url:
                print(f"{args.firm} has no EDGE_URL", file=sys.stderr)
                return 1
            print(url)
            return 0
        if args.cmd == "self-hosted":
            for line in self_hosted_notice(root) or ["no self-hosted firm"]:
                print(line)
            return 0
        if args.cmd == "deploy":
            from lra import release

            try:
                return release.deploy(load(args.firm, root), args.release, assets=args.assets,
                                      apply=args.apply, runner=runner)
            except release.ReleaseError as e:
                print(e)
                return 2
        if args.cmd == "support":
            from lra import support

            return support.main(load(args.firm, root), days=args.days, write=args.write)
        if args.cmd == "offboard":
            from lra import offboard

            return offboard.main(load(args.firm, root), apply=args.apply,
                                 key_file=args.key_file, actor=args.by, runner=runner)
        if args.cmd == "certificate":
            from lra import offboard

            return offboard.check_file(args.file, args.key_file)
    except TenantError as e:
        print(e)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
