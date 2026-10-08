"""`lra tenant offboard <firm>`: delete everything of a firm's, and say so in writing.

"How do we prove it is all gone when we leave?" is asked in every security
review, and a client's outside-counsel guidelines ask the firm the same at
the end of a matter. This is the answer for a whole deployment, run by the
firm's IT with the firm's own Cloudflare token and Anthropic key:

  1. pause it (killswitch.py), so nothing new starts while it is taken apart;
  2. export the audit trail to the firm (no contents), the one record kept;
  3. purge every registered client (purge.py: rows, documents, notes, and
     Anthropic's sessions, files and memory stores for each);
  4. delete what is left at Anthropic for this deployment: every session of
     its agents and every one the audit trail names, with their files; the
     uploads it names; every memory store it made; the document-system
     vaults it made; its skills; its environment; and archive its agents
     (the API archives an agent, it has no delete);
  5. delete it at Cloudflare: the Worker (and with it every secret on it,
     DATA_KEY included), its Workflow and instances, its container
     application, its D1 database, and its R2 bucket (emptied by a lifecycle
     rule first: R2 will not delete a bucket with objects in it, and wrangler
     cannot list them);
  6. remove DATA_KEY, EDGE_SECRET and the Anthropic key from this machine's
     deployments/<firm>/.env;
  7. write a deletion certificate: what was deleted, by kind, id, count and
     time, and nothing of any document. Signed with HMAC-SHA256 under a key
     the firm supplies (--key-file), and in any case carrying the SHA-256 of
     its own canonical form; `lra tenant certificate <file>` checks both.

What it cannot do, and lists as by hand: the firm's DNS and Email Routing
rule, its transport rule, the webhook in its Anthropic Console, its Anthropic
workspace and keys, and every copy of DATA_KEY outside this machine (the
firm's password manager or KMS, the offline copy).

A dry run by default: it reads (the database through the Worker, Anthropic's
lists) and lists everything it would delete. With --apply it deletes. It is
resumable: a failure at Anthropic stops it before anything at Cloudflare is
deleted (the database is the list of what to delete), and running it again
finishes. An object already gone counts as deleted.
"""

from __future__ import annotations

import contextlib
import getpass
import hashlib
import hmac
import json
import os
import re
import socket
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from lra import tenant as tenants

CERT_TYPE = "second-eye-deletion-certificate"
CERT_VERSION = 1
# The agent ids a deployment's vars name (tenant.EXCLUSIVE_VARS, less the rest).
AGENT_VARS = ("MANAGED_REVIEW_AGENT_ID", "MANAGED_ASSOCIATE_AGENT_ID", "MANAGED_PLAYBOOK_AGENT_ID",
              "MANAGED_COMMENTS_AGENT_ID", "MANAGED_CLOSING_AGENT_ID",
              "MANAGED_BLACKLINE_AGENT_ID")
SKILL_VARS = ("SANDBOX_SKILL_ID", "SANDBOX_PLAYBOOK_SKILL_ID", "SANDBOX_KEY_TERMS_SKILL_ID",
              "SANDBOX_COMMENTS_SKILL_ID", "SANDBOX_CLOSING_SKILL_ID",
              "SANDBOX_BLACKLINE_SKILL_ID", "SANDBOX_NEGOTIATION_SKILL_ID")
LOCAL_SECRETS = ("DATA_KEY", "DATA_KEY_PREVIOUS", "EDGE_SECRET", "ANTHROPIC_API_KEY",
                 "ANTHROPIC_WORKSPACE_ID", "ANTHROPIC_WEBHOOK_SIGNING_KEY")
# Order at Anthropic: sessions (they hold the work) before what they used.
ANTHROPIC_KINDS = ("session", "file", "memory_store", "vault", "skill", "environment", "agent")
STATEMENT = ("This certificate lists what was deleted, by kind, identifier, count and time. "
             "It contains no document, message, name of a party, finding or other content.")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class Item:
    where: str          # anthropic | cloudflare | local | by hand
    kind: str
    id: str
    status: str = "planned"   # planned | deleted | already gone | archived | failed | pending | by hand
    at: str = ""
    detail: str = ""


@dataclass
class Offboarding:
    tenant: str
    items: list[Item] = field(default_factory=list)
    clients: list[str] = field(default_factory=list)
    purges: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    audit_export: str = ""
    started_at: str = field(default_factory=_now)

    def add(self, where: str, kind: str, ids, **kw) -> None:
        seen = {(i.where, i.kind, i.id) for i in self.items}
        for value in ids:
            value = str(value or "").strip()
            if value and (where, kind, value) not in seen:
                seen.add((where, kind, value))
                self.items.append(Item(where, kind, value, **kw))

    def of(self, where: str, kind: str | None = None) -> list[Item]:
        return [i for i in self.items if i.where == where and (kind is None or i.kind == kind)]

    @property
    def failed(self) -> list[Item]:
        return [i for i in self.items if i.status in ("failed", "pending")]


# --------------------------------------------------------------------------
# What there is. Reads only.
# --------------------------------------------------------------------------


def _gone(error: Exception) -> bool:
    return getattr(error, "status_code", None) == 404 or type(error).__name__ == "NotFoundError"


def from_database(o: Offboarding) -> None:
    """The clients, and every Anthropic id the deployment's own rows name."""
    from lra import clients, purge
    from lra.store import connect

    purge._schemas()
    with contextlib.suppress(Exception):
        from lra import dms_mcp

        dms_mcp.init()

    def q(sql: str) -> list:
        try:
            with connect() as c:
                return c.execute(sql).fetchall()
        except Exception as e:
            if "no such table" in str(e):
                return []
            raise

    o.clients = [c.id for c in clients.all_clients()]
    for sessions, files in q("SELECT session_ids, file_ids FROM audit_log"):
        o.add("anthropic", "session", json.loads(sessions or "[]"))
        o.add("anthropic", "file", json.loads(files or "[]"))
    o.add("anthropic", "session", [r[0] for r in q("SELECT session_id FROM review_sessions")])
    o.add("anthropic", "memory_store", [r[0] for r in q("SELECT store_id FROM memory_stores")])
    o.add("anthropic", "vault", [r[0] for r in q("SELECT vault_id FROM dms_vaults")])
    o.add("anthropic", "vault", [r[0] for r in q("SELECT vault_id FROM dms_session_vaults")])


def from_vars(o: Offboarding, t: tenants.Tenant) -> None:
    v = t.vars
    o.add("anthropic", "agent", [v.get(k, "") for k in AGENT_VARS])
    o.add("anthropic", "skill", [v.get(k, "") for k in SKILL_VARS])
    o.add("anthropic", "environment", [v.get("MANAGED_ENVIRONMENT_ID", "")])
    o.add("anthropic", "memory_store", [v.get("MANAGED_FIRM_MEMORY_STORE_ID", "")])


def from_anthropic(o: Offboarding, client) -> None:
    """Every session of this deployment's agents, archived ones included, and
    the vaults it tagged as its own. Only what is provably this deployment's:
    a workspace may hold other things of the firm's."""
    from lra import managed

    for agent in [i.id for i in o.of("anthropic", "agent")]:
        try:
            for s in client.beta.sessions.list(agent_id=agent, include_archived=True,
                                               betas=managed.BETAS):
                o.add("anthropic", "session", [s.id])
        except Exception as e:
            if not _gone(e):
                raise
    for vault in client.beta.vaults.list(include_archived=True):
        tag = (getattr(vault, "metadata", None) or {}).get("lra", "")
        if tag in ("dms", "dms-session"):
            o.add("anthropic", "vault", [vault.id])


def cloudflare_items(o: Offboarding, t: tenants.Tenant) -> None:
    o.add("cloudflare", "worker", [t.worker])
    o.add("cloudflare", "workflow", [t.get("workflow")])
    o.add("cloudflare", "container application", [t.worker])
    o.add("cloudflare", "d1 database", [t.get("d1_database_name")])
    o.add("cloudflare", "r2 bucket", [t.get("r2_bucket")])
    present = [k for k in LOCAL_SECRETS if k in tenants.read_env(t.env_file)]
    o.add("local", "secret", [f"{k} in {tenants._rel(t.env_file, t.root)}" for k in present])
    address = str(t.vars.get("MAIL_AGENT_ADDRESS", ""))
    o.add("by hand", "step", [
        (f"Email Routing: delete the rule for {address or 'the agent address'} and the DNS "
        "records `wrangler email sending enable` added (the zone is the firm's)"),
        "The firm's mail flow: remove the transport rule or gateway policy that BCCs the agent",
        "Anthropic Console: remove the webhook to <EDGE_URL>/anthropic/webhook",
        "Anthropic Console: delete the API key(s) this deployment used, then the workspace "
        f"{t.get('anthropic_workspace') or ''} if it was only for Second Eye".rstrip(),
        ("Every other copy of DATA_KEY: the firm's password manager or KMS entry and the "
        "offline copy (destroying them is what makes anything left anywhere unreadable)"),
        "Any Cloudflare API token made for Second Eye (deploy, support): delete it",
    ], status="by hand")


# --------------------------------------------------------------------------
# Doing it
# --------------------------------------------------------------------------


def _anthropic_delete(client, item: Item) -> str:
    """Delete one object; the status to record."""
    from lra import managed

    beta = client.beta
    try:
        if item.kind == "session":
            status = str(getattr(beta.sessions.retrieve(item.id), "status", "") or "")
            if status in managed.ACTIVE:
                managed.interrupt(client, item.id)
                raise RuntimeError("still running; interrupted, run offboard again in a minute")
            for meta in beta.files.list(scope_id=item.id, betas=managed.BETAS):
                with contextlib.suppress(Exception):
                    beta.files.delete(meta.id)
            beta.sessions.delete(item.id)
        elif item.kind == "file":
            beta.files.delete(item.id)
        elif item.kind == "memory_store":
            beta.memory_stores.delete(item.id)
        elif item.kind == "vault":
            beta.vaults.delete(item.id)
        elif item.kind == "skill":
            # A skill with versions cannot be deleted; its versions go first.
            for version in list(beta.skills.versions.list(skill_id=item.id)):
                beta.skills.versions.delete(version.id, skill_id=item.id)
            beta.skills.delete(item.id)
        elif item.kind == "environment":
            beta.environments.delete(item.id)
        elif item.kind == "agent":
            beta.agents.archive(item.id)
            return "archived"
        else:
            raise ValueError(f"not an Anthropic kind: {item.kind}")
    except Exception as e:
        if _gone(e):
            return "already gone"
        raise
    return "deleted"


def _wrangler(t: tenants.Tenant, item: Item) -> list[list[str]]:
    """The wrangler commands that delete one Cloudflare item."""
    cfg = t.config_arg()
    j = ["--jurisdiction", t.jurisdiction] if t.jurisdiction else []
    w = ["npx", "wrangler"]
    if item.kind == "worker":
        return [[*w, "delete", "--config", cfg, "--force"]]
    if item.kind == "workflow":
        return [[*w, "workflows", "delete", item.id]]
    if item.kind == "container application":
        return [[*w, "containers", "list", "--json"]]   # then delete by id (_containers)
    if item.kind == "d1 database":
        return [[*w, "d1", "delete", item.id, "-y"]]
    if item.kind == "r2 bucket":
        return [[*w, "r2", "bucket", "lifecycle", "add", item.id, "offboard-expire-everything",
                 "", "--expire-days", "1", "-y", *j],
                [*w, "r2", "bucket", "delete", item.id, *j]]
    raise ValueError(item.kind)


def _containers(output: str, worker: str) -> list[str]:
    """The ids of the container applications of this Worker, from
    `wrangler containers list --json`."""
    try:
        listed = json.loads(output[output.find("["):])
    except ValueError:
        return []
    return [str(a.get("id")) for a in listed
            if isinstance(a, dict) and str(a.get("name", "")).startswith(worker)]


def _cloudflare_delete(t: tenants.Tenant, item: Item, runner: tenants.Runner) -> str:
    env = {"CLOUDFLARE_ACCOUNT_ID": t.account_id} if t.account_id else {}
    cwd = t.root / "cloudflare"
    for argv in _wrangler(t, item):
        code, output = runner(argv, cwd, env, None)
        text = output.lower()
        missing = any(s in text for s in ("not found", "does not exist", "10007", "couldn't find"))
        if item.kind == "container application":
            if code != 0:
                raise RuntimeError(output.strip()[-300:] or f"exit {code}")
            ids = _containers(output, t.worker)
            for app in ids:
                c, out = runner(["npx", "wrangler", "containers", "delete", app], cwd, env, None)
                if c != 0 and "not found" not in out.lower():
                    raise RuntimeError(out.strip()[-300:] or f"exit {c}")
            item.detail = ", ".join(ids)
            return "deleted" if ids else "already gone"
        if code != 0 and missing:
            return "already gone"
        if code != 0 and item.kind == "r2 bucket" and "not empty" in text:
            item.detail = ("emptying: a lifecycle rule expires every object within a day; "
                           "run offboard --apply again then to delete the bucket")
            return "pending"
        if code != 0:
            raise RuntimeError(output.strip()[-300:] or f"exit {code}")
    return "deleted"


def _destroy_local(t: tenants.Tenant) -> None:
    """DATA_KEY and the other secrets out of this machine's tenant .env. The
    file is rewritten without them, and removed if nothing else is in it."""
    path = t.env_file
    if not path.exists():
        return
    kept = [line for line in path.read_text().splitlines()
            if not re.match(rf"^\s*(?:export\s+)?({'|'.join(LOCAL_SECRETS)})\s*=", line)]
    if any(line.strip() and not line.lstrip().startswith("#") for line in kept):
        path.write_text("\n".join(kept) + "\n")
    else:
        path.write_bytes(b"\0" * path.stat().st_size)
        path.unlink()


def _actor(by: str | None) -> str:
    if by:
        return by
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return "unknown"


# --------------------------------------------------------------------------
# The certificate
# --------------------------------------------------------------------------


def canonical(body: dict) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def certificate(t: tenants.Tenant, o: Offboarding, *, actor: str, key: bytes | None = None,
                finished_at: str | None = None) -> dict:
    """The deletion certificate: ids, counts and times, no contents."""
    done = [i for i in o.items if i.where != "by hand"]
    counts: dict[str, int] = {}
    for i in done:
        if i.status in ("deleted", "already gone", "archived"):
            counts[f"{i.where} {i.kind}"] = counts.get(f"{i.where} {i.kind}", 0) + 1
    body = {
        "type": CERT_TYPE,
        "version": CERT_VERSION,
        "tenant": t.name,
        "worker": t.worker,
        "cloudflare_account": t.account_id,
        "anthropic_organization": t.get("anthropic_organization"),
        "anthropic_workspace": t.get("anthropic_workspace"),
        "run_by": actor,
        "host": socket.gethostname(),
        "started_at": o.started_at,
        "finished_at": finished_at or _now(),
        "complete": not o.failed,
        "audit_export": o.audit_export,
        "client_purges": o.purges,
        "counts": dict(sorted(counts.items())),
        "deleted": [{k: v for k, v in asdict(i).items() if v != ""} for i in done
                    if i.status in ("deleted", "already gone", "archived")],
        "not_deleted": [{k: v for k, v in asdict(i).items() if v != ""} for i in o.failed],
        "by_hand": [i.id for i in o.items if i.where == "by hand"],
        "statement": STATEMENT,
    }
    cert: dict = {"certificate": body, "sha256": hashlib.sha256(canonical(body)).hexdigest()}
    if key:
        cert["hmac_sha256"] = hmac.new(key, canonical(body), hashlib.sha256).hexdigest()
        cert["key_fingerprint"] = hashlib.sha256(key).hexdigest()[:16]
    return cert


def check(cert: dict, key: bytes | None = None) -> list[str]:
    """What is wrong with a certificate: empty if it is intact (and, given
    the key, signed with it)."""
    body = cert.get("certificate")
    if not isinstance(body, dict) or body.get("type") != CERT_TYPE:
        return ["not a Second Eye deletion certificate"]
    found = []
    if hashlib.sha256(canonical(body)).hexdigest() != cert.get("sha256"):
        found.append("sha256 does not match: the certificate was changed")
    if key is not None:
        if "hmac_sha256" not in cert:
            found.append("not signed (no hmac_sha256)")
        elif not hmac.compare_digest(hmac.new(key, canonical(body), hashlib.sha256).hexdigest(),
                                     cert["hmac_sha256"]):
            found.append("hmac_sha256 does not match this key")
    return found


def read_key(path: Path | None) -> bytes | None:
    if path is None:
        return None
    key = path.read_bytes().strip()
    if len(key) < 16:
        raise ValueError(f"{path}: a signing key is at least 16 bytes")
    return key


def check_file(path: Path, key_file: Path | None = None) -> int:
    cert = json.loads(path.read_text())
    found = check(cert, read_key(key_file))
    body = cert.get("certificate", {})
    for line in found:
        print(line)
    if not found:
        print(f"intact: {body.get('tenant')} offboarded {body.get('finished_at')}, "
              f"{sum(body.get('counts', {}).values())} objects, "
              + ("complete" if body.get("complete") else "NOT complete")
              + (", signed with this key" if key_file else ""))
    return 1 if found else 0


# --------------------------------------------------------------------------
# lra tenant offboard
# --------------------------------------------------------------------------


@contextlib.contextmanager
def firm_environment(t: tenants.Tenant) -> Iterator[None]:
    """The firm's database through its Worker, and its Anthropic key, as
    `lra purge` reaches them: from deployments/<firm>/.env and its vars."""
    with tenants.environment(t):
        from lra.config import settings

        os.environ.setdefault("DATABASE_URL", "d1://")
        settings.cache_clear()
        yield


def _client():
    from lra.config import anthropic_client, settings

    if not settings().anthropic_api_key.strip():
        raise RuntimeError("ANTHROPIC_API_KEY is not in the tenant's .env")
    return anthropic_client()


def run(t: tenants.Tenant, *, apply: bool = False, key: bytes | None = None,
        actor: str | None = None, runner: tenants.Runner = tenants.run_command,
        client_factory: Callable = _client,
        environment: Callable = firm_environment, out_dir: Path | None = None) -> int:
    if t.primary:
        print(f"{t.name} is the primary deployment, the operator's own; it is not "
              "offboarded with this.")
        return 2
    actor = _actor(actor)
    o = Offboarding(t.name)
    out_dir = out_dir or t.dir / "offboarding"
    with environment(t):
        from_vars(o, t)
        try:
            from_database(o)
        except Exception as e:  # noqa: BLE001 - said, and nothing at Cloudflare is deleted
            o.notes.append(f"the database could not be read through the Worker ({e}); its "
                           "clients and the Anthropic ids it records are not listed")
        client = None
        try:
            client = client_factory()
            from_anthropic(o, client)
        except Exception as e:  # noqa: BLE001
            o.notes.append(f"Anthropic could not be listed ({e}); only the ids recorded are")
        cloudflare_items(o, t)

        if not apply:
            _print_plan(t, o, key is not None)
            return 0
        if o.notes:
            print("Not starting: " + "; ".join(o.notes) + ". Fix that (the tenant's .env "
                  "needs EDGE_SECRET and ANTHROPIC_API_KEY) and run again.")
            return 1

        from lra import audit, killswitch, purge

        print(f"Offboarding {t.name}, run by {actor}.")
        if killswitch.run(t, True, apply=True, by=actor, runner=runner) != 0:
            print("Could not pause it; stopping before anything is deleted.")
            return 1
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = o.started_at.replace(":", "")
        export = out_dir / f"audit-{stamp}.csv"
        export.write_text(audit.as_csv(audit.rows("1970-01-01")))
        o.audit_export = f"{tenants._rel(export, t.root)} (sha256 {release_sha(export)})"
        print(f"Audit trail exported to {tenants._rel(export, t.root)}: keep it, it is the "
              "firm's record of what the tool did.")

        for cid in o.clients:
            result = purge.run("client", cid, apply=True, actor=actor)
            o.purges.append({"client": cid, "purge_id": result.purge_id,
                             "counts": result.counts, "complete": result.done})
            if not result.done:
                o.items.append(Item("anthropic", "client purge", cid, "failed", _now(),
                                    f"{len(result.failed)} item(s) left"))

        order = {k: n for n, k in enumerate(ANTHROPIC_KINDS)}
        for item in sorted(o.of("anthropic"), key=lambda i: order.get(i.kind, 99)):
            if item.kind == "client purge":
                continue
            try:
                item.status = _anthropic_delete(client, item)
            except Exception as e:  # noqa: BLE001 - recorded; Cloudflare waits for it
                item.status, item.detail = "failed", str(e)[:300]
            item.at = _now()

        if o.failed:
            print("Some of Anthropic's copies could not be deleted yet, so nothing at "
                  "Cloudflare was deleted (the database is the list of what remains). Run "
                  "again to finish.")
        else:
            for item in o.of("cloudflare"):
                try:
                    item.status = _cloudflare_delete(t, item, runner)
                except Exception as e:  # noqa: BLE001
                    item.status, item.detail = "failed", str(e)[:300]
                item.at = _now()
            if not any(i.status == "failed" for i in o.of("cloudflare", "worker")):
                _destroy_local(t)
                for item in o.of("local"):
                    item.status, item.at = "deleted", _now()

    cert = certificate(t, o, actor=actor, key=key)
    path = out_dir / f"deletion-certificate-{o.started_at.replace(':', '')}.json"
    path.write_text(json.dumps(cert, indent=2) + "\n")
    _print_result(t, o, path)
    return 1 if o.failed else 0


def release_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _print_plan(t: tenants.Tenant, o: Offboarding, signed: bool) -> None:
    print(f"Dry run: offboarding {t.name} would delete, in this order (nothing has been "
          "deleted):\n")
    print("  1. Pause it (lra pause), so nothing new starts.")
    print("  2. Export the audit trail to deployments/<firm>/offboarding/ (the firm keeps it).")
    print(f"  3. Purge {len(o.clients)} registered client(s)"
          + (f": {', '.join(o.clients)}" if o.clients else "") + " (lra purge --client).")
    print("  4. At Anthropic, with the firm's key:")
    for kind in ANTHROPIC_KINDS:
        ids = [i.id for i in o.of("anthropic", kind)]
        verb = "archive" if kind == "agent" else "delete"
        print(f"       {verb} {len(ids):3} {kind}(s)" + (f": {', '.join(ids)}" if ids else ""))
    print("  5. At Cloudflare, with the firm's token (account "
          f"{t.account_id or 'of CLOUDFLARE_API_TOKEN'}):")
    for item in o.of("cloudflare"):
        for argv in _wrangler(t, item):
            print(f"       {tenants.shell(t, tenants.Step('', argv))}")
        if item.kind == "container application":
            print("       ... then `npx wrangler containers delete <id>` for each application "
                  f"named {t.worker}*")
    print("  6. Remove from this machine: "
          + (", ".join(i.id for i in o.of("local")) or "nothing (no secrets in its .env)"))
    print("  7. Write the deletion certificate (ids, counts, times; no contents), "
          + ("HMAC-SHA256 signed with the --key-file key." if signed else
             "with its SHA-256 (pass --key-file to have it signed with a key the firm holds)."))
    print("\nBy hand, after (the tool cannot reach these):")
    for item in o.of("by hand"):
        print(f"  - {item.id}")
    for note in o.notes:
        print(f"\nNOTE: {note}")
    print(f"\n`lra tenant offboard {t.name} --apply` does it. It cannot be undone.")


def _print_result(t: tenants.Tenant, o: Offboarding, path: Path) -> None:
    print()
    for item in o.items:
        if item.where != "by hand":
            print(f"  {item.status:12} {item.where} {item.kind} {item.id}"
                  + (f"  ({item.detail})" if item.detail else ""))
    print(f"\nDeletion certificate: {tenants._rel(path, t.root)}")
    if o.failed:
        print(f"{len(o.failed)} item(s) not done yet; run `lra tenant offboard {t.name} --apply` "
              "again to finish, and a new certificate is written.")
    print("Still by hand:")
    for item in o.of("by hand"):
        print(f"  - {item.id}")


def main(t: tenants.Tenant, *, apply: bool = False, key_file: Path | None = None,
         actor: str | None = None, runner: tenants.Runner = tenants.run_command) -> int:
    try:
        key = read_key(key_file)
    except (OSError, ValueError) as e:
        print(e)
        return 2
    return run(t, apply=apply, key=key, actor=actor, runner=runner)
