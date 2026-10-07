"""`lra selftest` (src/lra/selftest.py): a firm's IT checks the deployment.

What it must never do is as tested as what it reports: no model call (the
fake Anthropic client has nothing but `retrieve`), no email, no write, and
the stranger check is the Worker's own rule, held to cloudflare/src/mail.ts.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from lra import audit, crypto, selftest, tenant
from lra.selftest import FAIL, INFO, PASS, SKIP, WARN

ROOT = Path(__file__).resolve().parents[1]

SAFE = {
    "ALLOWED_SENDERS": "acme-law.com, gc@acme-law.co.uk",
    "FIRM_DOMAINS": "acme-law.com,acme-law.co.uk",
    "REPLY_POLICY": "sender_only",
    "RETENTION_HOURS": "24",
    "THREAD_RETENTION_DAYS": "7",
    "ARCHIVE_ENABLED": "false",
    "SERVICE_PAUSED": "false",
    "WEB_SEARCH_ENABLED": "false",
    "INFERENCE_GEO": "us",
    "PLAYBOOK_ADMINS": "gc@acme-law.com",
    "MAX_EMAILS_PER_DAY": "200",
    "EDGE_URL": "https://legal-review-agent-acme.example.workers.dev",
    "MANAGED_REVIEW_AGENT_ID": "agent_review",
    "MANAGED_ASSOCIATE_AGENT_ID": "agent_assoc",
    "MANAGED_ENVIRONMENT_ID": "env_1",
    "MANAGED_FIRM_MEMORY_STORE_ID": "memstore_1",
}


def by_name(checks) -> dict[str, selftest.Check]:
    return {c.name: c for c in checks}


def statuses(v, secrets=None) -> dict[str, str]:
    return {c.name: c.status for c in selftest.config_checks(v, secrets or {})}


# --- the Worker's admission rule ------------------------------------------------


@pytest.mark.parametrize("sender, allowlist, admitted", [
    ("jane@acme-law.com", "acme-law.com", True),
    ("Jane@Acme-Law.com", "ACME-LAW.COM", True),
    ("jane@london.acme-law.com", "acme-law.com", False),     # exact domain only
    ("jane@acme-law.com.evil.example", "acme-law.com", False),
    ("gc@acme-law.co.uk", "acme-law.com gc@acme-law.co.uk", True),
    ("other@acme-law.co.uk", "acme-law.com, gc@acme-law.co.uk", False),
    ("jane@acme-law.com", "", False),                         # empty admits nobody
])
def test_the_worker_rule_admits_exact_addresses_and_domains_only(sender, allowlist, admitted):
    assert selftest.worker_admits(sender, allowlist) is admitted


def test_the_replica_still_matches_the_worker_source():
    """If isAllowed in mail.ts changes, this replica (and the stranger check
    built on it) has to change with it."""
    source = (ROOT / "cloudflare" / "src" / "mail.ts").read_text()
    body = source[source.index("export function isAllowed"):]
    body = body[:body.index("\n}\n")]
    assert 'toLowerCase().split(/[\\s,]+/).filter(Boolean)' in body
    assert 'sender.split("@").pop()' in body
    assert "return entries.includes(sender) || entries.includes(domain);" in body


# --- configuration --------------------------------------------------------------


def test_a_safe_configuration_has_no_failure():
    found = statuses(SAFE, {"DATA_KEY": crypto.generate()})
    assert FAIL not in found.values(), found
    assert found["stranger-dropped"] == PASS
    assert found["reply-policy"] == PASS
    assert found["data-key"] == PASS
    assert found["archive"] == PASS


@pytest.mark.parametrize("change, check", [
    ({"ALLOWED_SENDERS": ""}, "allowlist"),
    ({"ALLOWED_SENDERS": "acme-law.com,gmail.com"}, "allowlist-public-domain"),
    ({"REPLY_POLICY": "model"}, "reply-policy"),
    ({"THREAD_RETENTION_DAYS": "0"}, "retention-threads"),
    ({"RETENTION_HOURS": "0"}, "retention-jobs"),
    ({"ARCHIVE_ENABLED": "true", "ARCHIVE_RETENTION_DAYS": "0"}, "archive"),
    ({"WEB_SEARCH_ENABLED": "true"}, "web-search"),
    ({"FIRM_DOMAINS": ""}, "firm-domains"),
])
def test_each_unsafe_setting_fails(change, check):
    assert statuses({**SAFE, **change})[check] == FAIL


@pytest.mark.parametrize("change, check", [
    ({"INFERENCE_GEO": ""}, "inference-geo"),
    ({"INFERENCE_GEO": "global"}, "inference-geo"),
    ({"PLAYBOOK_ADMINS": ""}, "playbook-admins"),
    ({"MAX_EMAILS_PER_DAY": "0"}, "daily-cap"),
    ({"SERVICE_PAUSED": "true"}, "kill-switch"),
    ({"ARCHIVE_ENABLED": "true", "ARCHIVE_RETENTION_DAYS": "30"}, "archive"),
    ({"THREAD_RETENTION_DAYS": "30"}, "retention-threads"),
    ({"LEARN_FROM_OUTCOMES": "true"}, "learn-from-outcomes"),
    ({"MANAGED_REVIEW_AGENT_ID": ""}, "agents-configured"),
    ({"ALLOWED_SENDERS": "acme-law.com"}, "allowlist-firm-domains"),
])
def test_each_weaker_setting_warns(change, check):
    assert statuses({**SAFE, **change})[check] == WARN


def test_retention_defaults_are_the_products_seven_days():
    """Unset, THREAD_RETENTION_DAYS and ARCHIVE_RETENTION_DAYS are 7
    (config.py), which passes; the archive on with no retention set is the
    default 7 days, a WARN for being on, not a FAIL for keeping for ever."""
    from lra.config import Settings

    assert Settings.model_fields["thread_retention_days"].default == 7
    assert Settings.model_fields["archive_retention_days"].default == 7
    bare = {k: v for k, v in SAFE.items() if k != "THREAD_RETENTION_DAYS"}
    assert statuses(bare)["retention-threads"] == PASS
    assert statuses({**bare, "ARCHIVE_ENABLED": "true"})["archive"] == WARN


def test_an_exact_public_address_is_fine_but_the_domain_is_not():
    assert "allowlist-public-domain" not in statuses({**SAFE, "ALLOWED_SENDERS": "fictional.lawyer@gmail.com"})


def test_a_stranger_that_the_list_would_admit_fails():
    """A lookalike admitted by a sloppy entry (here, the attacker's domain
    itself on the list) is reported, not hidden."""
    found = by_name(selftest.config_checks(
        {**SAFE, "ALLOWED_SENDERS": "acme-law.com,attacker.example"}, {}))
    assert found["stranger-dropped"].status == FAIL
    assert "acme-law@attacker.example" in found["stranger-dropped"].detail


@pytest.mark.parametrize("key, why", [
    ("MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=", "test key"),
    ("dG9vLXNob3J0", "not 32"),
    ("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=", "variety"),
    ("not base64 !!", "base64"),
])
def test_a_bad_data_key_fails(key, why):
    check = by_name(selftest.config_checks(SAFE, {"DATA_KEY": key}))["data-key"]
    assert check.status == FAIL
    assert why in check.detail


def test_the_data_key_is_never_printed():
    key = crypto.generate()
    checks = selftest.config_checks(SAFE, {"DATA_KEY": key})
    assert all(key not in c.detail for c in checks)


def test_the_test_keys_listed_are_the_ones_the_repository_uses():
    vitest = (ROOT / "cloudflare" / "vitest.config.ts").read_text()
    assert any(k in vitest for k in selftest.KNOWN_TEST_KEYS)
    tests = (ROOT / "tests" / "test_cloudflare.py").read_text()
    assert all(k in tests for k in selftest.KNOWN_TEST_KEYS)


def test_no_data_key_here_is_a_skip_not_a_pass():
    assert statuses(SAFE)["data-key"] == SKIP


# --- the Worker, probed --------------------------------------------------------


def fetcher(responses: dict[str, tuple[int, str]]):
    calls: list[str] = []

    def fetch(url: str):
        calls.append(url)
        for path, answer in responses.items():
            if url.endswith(path):
                return answer
        return 404, "not found"

    fetch.calls = calls
    return fetch


def test_a_healthy_edge_with_internal_endpoints_refused():
    fetch = fetcher({"/health": (200, '{"edge":"ok"}'), "/internal/db": (401, "unauthorised")})
    found = by_name(selftest.edge_checks(SAFE, fetch))
    assert found["edge-health"].status == PASS
    # Refused, but on the internet: said plainly until it moves off it.
    assert found["internal-endpoints"].status == WARN
    assert found["oauth-callback"].status == PASS
    # GETs only, and nothing carries the secret.
    assert all(c.startswith(SAFE["EDGE_URL"]) for c in fetch.calls)


def test_internal_endpoints_off_the_internet_pass_and_open_ones_fail():
    gone = fetcher({"/health": (200, '{"edge":"ok"}')})
    assert by_name(selftest.edge_checks(SAFE, gone))["internal-endpoints"].status == PASS
    open_ = fetcher({"/health": (200, '{"edge":"ok"}'), "/internal/db": (200, "{}")})
    assert by_name(selftest.edge_checks(SAFE, open_))["internal-endpoints"].status == FAIL


def test_an_unhealthy_edge_fails():
    found = by_name(selftest.edge_checks(SAFE, fetcher({"/health": (500, "boom")})))
    assert found["edge-health"].status == FAIL


def test_an_unreachable_edge_fails_without_raising():
    def fetch(url):
        raise OSError("connection refused")

    assert by_name(selftest.edge_checks(SAFE, fetch))["edge-health"].status == FAIL


# --- Anthropic: retrieval only ---------------------------------------------------


class NotFound(Exception):
    pass


def fake_anthropic(known: set[str], networking: dict | None = None):
    calls: list[tuple[str, str]] = []

    def retriever(kind):
        def retrieve(ident):
            calls.append((kind, ident))
            if ident not in known:
                raise NotFound(ident)
            if kind == "environments":
                return NS(id=ident, config=NS(networking=networking or {
                    "type": "limited", "allow_package_managers": True,
                    "allow_mcp_servers": False, "allowed_hosts": []}))
            return NS(id=ident)
        return retrieve

    # Nothing but retrieve: a session, a message or a create fails the test.
    client = NS(beta=NS(agents=NS(retrieve=retriever("agents")),
                        environments=NS(retrieve=retriever("environments")),
                        memory_stores=NS(retrieve=retriever("memory_stores"))))
    keys: list[tuple[str, str]] = []

    def factory(key, workspace):
        keys.append((key, workspace))
        return client

    factory.calls, factory.keys = calls, keys
    return factory


def test_agents_the_firms_key_can_see_are_the_firms():
    factory = fake_anthropic({"agent_review", "agent_assoc", "env_1", "memstore_1"})
    found = by_name(selftest.anthropic_checks(SAFE, {"ANTHROPIC_API_KEY": "sk-firm"}, factory))
    assert found["agents-own-org"].status == PASS
    assert found["sandbox-egress"].status == PASS
    assert factory.keys == [("sk-firm", "")]
    assert {kind for kind, _ in factory.calls} == {"agents", "environments", "memory_stores"}


def test_an_agent_in_someone_elses_organisation_fails():
    factory = fake_anthropic({"agent_assoc", "env_1", "memstore_1"})
    check = by_name(selftest.anthropic_checks(SAFE, {"ANTHROPIC_API_KEY": "k"}, factory))
    assert check["agents-own-org"].status == FAIL
    assert "MANAGED_REVIEW_AGENT_ID agent_review" in check["agents-own-org"].detail


def test_an_environment_with_open_egress_fails():
    factory = fake_anthropic({"agent_review", "agent_assoc", "env_1", "memstore_1"},
                             networking={"type": "unrestricted"})
    found = by_name(selftest.anthropic_checks(SAFE, {"ANTHROPIC_API_KEY": "k"}, factory))
    assert found["sandbox-egress"].status == FAIL


def test_no_key_is_a_skip():
    found = by_name(selftest.anthropic_checks(SAFE, {}, fake_anthropic(set())))
    assert found["agents-own-org"].status == SKIP


# --- the Worker's secrets, by name ----------------------------------------------


def test_worker_secret_names_are_listed_never_values():
    seen = []

    def runner(argv, cwd):
        seen.append(argv)
        return 0, json.dumps([{"name": "DATA_KEY", "type": "secret_text"},
                              {"name": "EDGE_SECRET", "type": "secret_text"},
                              {"name": "ANTHROPIC_API_KEY", "type": "secret_text"}])

    found = by_name(selftest.worker_secret_checks("../deployments/x/wrangler.jsonc",
                                                  ROOT, runner))
    assert found["worker-secrets"].status == PASS
    assert found["webhook-key"].status == INFO
    assert seen == [["npx", "wrangler", "secret", "list", "--config",
                     "../deployments/x/wrangler.jsonc", "--format", "json"]]


def test_a_missing_worker_secret_fails():
    found = by_name(selftest.worker_secret_checks(
        "c", ROOT, lambda argv, cwd: (0, '[{"name": "EDGE_SECRET"}]')))
    assert found["worker-secrets"].status == FAIL
    assert "DATA_KEY" in found["worker-secrets"].detail


# --- the audit export, read only -------------------------------------------------


@pytest.fixture
def database(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/lra.sqlite3")
    from lra.config import settings

    settings.cache_clear()
    yield
    settings.cache_clear()


def test_the_audit_export_reads_the_trail(database):
    audit.status("job-1", "<m1@acme-law.com>", "replied", "jane@acme-law.com", {})
    check = selftest.audit_check()
    assert check.status == PASS
    assert check.detail.startswith("1 job row(s)")
    assert "job_id,received_at,sender" in check.detail


def test_the_audit_export_writes_nothing(database, tmp_path):
    """On a deployment that has never handled a message there is no table,
    and the self-test does not create one."""
    check = selftest.audit_check()
    assert check.status == WARN
    from lra.store import is_d1

    if not is_d1():
        assert not (tmp_path / "lra.sqlite3").exists()


# --- end to end -----------------------------------------------------------------


@pytest.fixture
def repo(tmp_path):
    from tests.test_tenant import make_repo

    return make_repo(tmp_path)


def test_a_firms_tenant_offline(repo, capsys):
    t = tenant.new("acme-llp", address="review@legal.acme-law.com", domains="acme-law.com",
                   jurisdiction="eu", admins="gc@acme-law.com", root=repo)
    t.env_file.write_text(f"DATA_KEY={crypto.generate()}\n")
    code = selftest.main(["--tenant", "acme-llp", "--offline"], root=repo)
    out = capsys.readouterr().out
    assert code == 0, out
    assert "Read-only: no model call, no email sent, nothing written." in out
    assert re.search(r"PASS\s+isolation", out)
    assert re.search(r"PASS\s+data-key", out)
    assert "jurisdiction eu" in out


def test_a_tenant_sharing_an_agent_with_another_fails(repo, capsys):
    for name in ("acme-llp", "beta-llp"):
        t = tenant.new(name, address=f"review@legal.{name}.com", domains=f"{name}.com",
                       root=repo)
        t.file.write_text(tenant.set_field(t.file.read_text(), "MANAGED_REVIEW_AGENT_ID",
                                           "agent_shared"))
    code = selftest.main(["--tenant", "acme-llp", "--offline", "--json"], root=repo)
    report = json.loads(capsys.readouterr().out)
    isolation = next(c for c in report["checks"] if c["name"] == "isolation")
    assert code == 1
    assert isolation["status"] == FAIL
    assert "agent_shared" in isolation["detail"]


def test_online_run_with_everything_injected(repo):
    t = tenant.new("acme-llp", address="review@legal.acme-law.com", domains="acme-law.com",
                   edge_url="https://acme.example.workers.dev", root=repo)
    target = selftest._from_tenant("acme-llp", None, repo)
    assert target.tenant.name == t.name
    checks = selftest.run(target, fetch=fetcher({"/health": (200, '{"edge":"ok"}')}),
                          factory=fake_anthropic(set()),
                          audit_connect=lambda: (_ for _ in ()).throw(
                              RuntimeError("no such table: audit_log")))
    found = by_name(checks)
    assert found["edge-health"].status == PASS
    assert found["agents-own-org"].status == SKIP       # no key in the tenant's .env
    assert found["audit-export"].status == WARN


def test_an_unknown_tenant_is_refused(repo, capsys):
    assert selftest.main(["--tenant", "nobody", "--offline"], root=repo) == 2
    assert "no tenant 'nobody'" in capsys.readouterr().out


def test_the_cli_dispatches(monkeypatch):
    from lra import cli

    seen = []
    monkeypatch.setattr(selftest, "main", lambda args: seen.append(args) or 0)
    monkeypatch.setattr("sys.argv", ["lra", "selftest", "--offline"])
    assert cli.main() == 0
    assert seen == [["--offline"]]
    assert "lra selftest" in cli.USAGE
