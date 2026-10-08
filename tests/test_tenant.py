"""One deployment per firm (src/secondeye/tenant.py, `second-eye tenant`).

Every deployment is a tenant, the operator's own (Jim's, `jim`, marked
primary) included: its config is rendered from deployments/<name>/tenant.jsonc
and the shared parts of cloudflare/wrangler.jsonc, which holds placeholders
for every name, id and var that is a deployment's own. Jim's tenant is
private (deployments/jim/ is not in the public repository), so the tests that
read it are skipped without it, and the rest use a primary tenant made here
with example values. A firm's shares no resource and no var with the
primary's, and is provisioned by wrangler commands that are printed unless
--apply is given. Nothing here runs wrangler: --apply is driven through a
recording runner.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import stat
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from secondeye import livecheck, tenant
from secondeye.config import settings
from tests import fake_sessions as fs
from tests.test_livecheck import Recorder, _fake_apply_client

ROOT = Path(__file__).resolve().parents[1]
WRANGLER = ROOT / "cloudflare" / "wrangler.jsonc"
JIM = ROOT / "deployments" / "jim" / "tenant.jsonc"
private = pytest.mark.skipif(not JIM.exists(),
                             reason="deployments/jim/ is private, not in the public repository")
D1_ID = "0c5b1a7e-9d55-4a5e-8a1f-3f1f0c2d4e6b"
PRIMARY_D1_ID = "5f0e2c1d-6a3b-4c7d-8e9f-0a1b2c3d4e5f"
LIFECYCLE_MAIL = ("r2 bucket lifecycle add second-eye-acme-llp-docs expire-unreviewed-mail "
                  "inbound/ --expire-days 7 -y --jurisdiction eu")
LIFECYCLE_JOB = ("r2 bucket lifecycle add second-eye-acme-llp-docs expire-job-state job/ "
                 "--expire-days 7 -y --jurisdiction eu")


def _jsonc(text: str) -> dict:
    """The parse the other tests use, independent of tenant.parse_jsonc."""
    return json.loads(re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE))


def primary(root: Path) -> tenant.Tenant:
    """A primary deployment shaped like Jim's, with example values, written
    and rendered under root."""
    shared = tenant.parse_jsonc((root / "cloudflare" / "wrangler.jsonc").read_text())
    vars_ = dict(shared["vars"]) | {
        "EDGE_URL": "https://legal-review-agent.operator.workers.dev",
        "MAIL_AGENT_ADDRESS": "review@legal.operator.example",
        "FIRM_DOMAINS": "operator.example", "ALLOWED_SENDERS": "lawyer@operator.example"}
    data = {"primary": True, "worker": "legal-review-agent", "account_id": "",
            "instance_type": "basic", "d1_database_name": "legal-review-agent",
            "d1_database_id": PRIMARY_D1_ID, "r2_bucket": "legal-review-agent-docs",
            "jurisdiction": "", "workflow": "legal-review-flow", "vars": vars_}
    where = root / "deployments" / "jim"
    where.mkdir(parents=True, exist_ok=True)
    (where / "tenant.jsonc").write_text("// The primary deployment, for the tests.\n"
                                        + json.dumps(data, indent=2) + "\n")
    t = tenant.load("jim", root)
    tenant.write(t)
    return t


def make_repo(root: Path) -> Path:
    """A repository with the real shared config and a primary tenant in it."""
    (root / "cloudflare").mkdir(parents=True)
    shutil.copyfile(WRANGLER, root / "cloudflare" / "wrangler.jsonc")
    primary(root)
    return root


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path)


def acme(repo, **kw) -> tenant.Tenant:
    args = {"address": "review@legal.acme-law.com", "domains": "acme-law.com, Acme-Law.co.uk",
            "jurisdiction": "eu", "contact": "gc@acme-law.com", "root": repo}
    args.update(kw)
    return tenant.new("acme-llp", **args)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# --- the shared config names nobody ------------------------------------------------


def test_the_shared_config_carries_placeholders_only():
    """cloudflare/wrangler.jsonc is published: no deployment's address,
    domain, sender, Worker URL or database id is in it."""
    config = tenant.parse_jsonc(WRANGLER.read_text())
    assert config["d1_databases"][0]["database_id"] == "00000000-0000-0000-0000-000000000000"
    v = config["vars"]
    assert v["MAIL_AGENT_ADDRESS"] == "review@legal.example.com"
    assert v["FIRM_DOMAINS"] == "example.com"
    assert v["ALLOWED_SENDERS"] == "lawyer@example.com"
    assert v["EDGE_URL"] == "https://legal-review-agent.example.workers.dev"
    text = WRANGLER.read_text()
    for t in tenant.tenants():
        own = [t.get("d1_database_id"), t.get("account_id")]
        own += [str(t.vars.get(k, "")) for k in
                (*tenant.EXCLUSIVE_VARS, "FIRM_DOMAINS", "ALLOWED_SENDERS", "PLAYBOOK_ADMINS")]
        for value in filter(None, own):
            for part in value.split(","):
                assert part.strip() not in text, f"{t.name}'s {part} is in the shared config"


# --- Jim's deployment, unmoved -----------------------------------------------------


@private
def test_jims_rendered_config_is_committed_and_points_where_production_did():
    """Production used to deploy cloudflare/wrangler.jsonc from cloudflare/;
    it now deploys deployments/jim/wrangler.jsonc with --config. Every path
    in it resolves to the same file as before."""
    jim = tenant.load("jim")
    assert jim.primary
    assert tenant.problems(jim) == []
    config = tenant.parse_jsonc(jim.config_path.read_text())
    assert config == tenant.render(jim)
    here = jim.config_path.parent
    assert (here / config["main"]).resolve() == ROOT / "cloudflare" / "src" / "index.ts"
    [container] = config["containers"]
    assert (here / container["image"]).resolve() == ROOT / "Dockerfile"
    assert (here / container["image_build_context"]).resolve() == ROOT


@private
def test_jims_names_and_ids_are_the_production_ones():
    """Pinned, so a change to both files at once is still a diff here. The
    identifying values are pinned by their SHA-256, so this file names
    nothing of Jim's."""
    config = tenant.render(tenant.load("jim"))
    assert config["name"] == "legal-review-agent"
    assert "account_id" not in config
    assert config["main"] == "../../cloudflare/src/index.ts"
    assert config["containers"] == [{"class_name": "ReviewContainer", "image": "../../Dockerfile",
                                     "image_build_context": "../..", "instance_type": "basic",
                                     "max_instances": 1}]
    [db] = config["d1_databases"]
    assert (db["binding"], db["database_name"]) == ("DB", "legal-review-agent")
    assert _sha(db["database_id"]) == (
        "b9fef98a45b797fa7d0d98d1fa32f98f9cf6c669de5b7985f5bc4b45ad598d32")
    assert config["r2_buckets"] == [{"binding": "DOCS", "bucket_name": "legal-review-agent-docs"}]
    assert config["workflows"] == [{"binding": "REVIEW_FLOW", "name": "legal-review-flow",
                                    "class_name": "ReviewFlow"}]
    assert _sha(config["vars"]["MAIL_AGENT_ADDRESS"]) == (
        "4a6d526f02733fbea4de9614714b1d1bd93e809d1d820c2a8b3c4efdbe160c1b")
    assert _sha(config["vars"]["EDGE_URL"]) == (
        "7ae6d65f228f4adc1a4dca7b09c49e1880df8e1178b8209619d94ea26a88b818")


def test_the_primary_is_deployed_by_the_first_job_and_is_never_provisioned(repo, capsys):
    jim = tenant.load("jim", repo)
    assert jim.primary and jim.secret_suffix == ""
    assert tenant.primary_tenant(repo).name == "jim"
    assert tenant.matrix(repo) == []
    text = jim.config_path.read_text()
    assert "CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID" in text
    assert "CLOUDFLARE_API_TOKEN_" not in text
    assert tenant.provision(jim, apply=True, runner=_never) == 2
    assert "Nothing to provision" in capsys.readouterr().out
    assert tenant.main(["url", "jim"], root=repo) == 0
    assert capsys.readouterr().out.strip() == "https://legal-review-agent.operator.workers.dev"


def test_jims_deploy_job_deploys_his_rendered_config():
    """The firms' jobs were added after it; Jim's deploys his tenant's rendered
    config, with the unsuffixed secrets, the queue detach and the health check
    at the URL his tenant names."""
    import yaml

    text = (ROOT / ".github" / "workflows" / "deploy.yml").read_text()
    assert "workers.dev" not in text, "the Worker URL comes from the tenant"
    jobs = yaml.safe_load(text)["jobs"]
    deploy = jobs["deploy"]
    assert deploy["env"] == {"CLOUDFLARE_API_TOKEN": "${{ secrets.CLOUDFLARE_API_TOKEN }}",
                             "CLOUDFLARE_ACCOUNT_ID": "${{ secrets.CLOUDFLARE_ACCOUNT_ID }}"}
    runs = [s.get("run", "") for s in deploy["steps"]]
    assert any(r.strip().endswith("npx wrangler deploy --config ../deployments/jim/wrangler.jsonc")
               for r in runs)
    assert any("python3 -m secondeye.tenant url jim)/health" in r for r in runs)
    # Production deploys only from the private repository, never a fork.
    for job in jobs.values():
        assert "github.repository == 'jamesbaker1/second-eye-private'" in job["if"]
    assert jobs["tenants"]["needs"] == "deploy"
    firm = jobs["tenant"]
    assert "CLOUDFLARE_API_TOKEN_{0}" in firm["env"]["CLOUDFLARE_API_TOKEN"]
    assert "CLOUDFLARE_ACCOUNT_ID_{0}" in firm["env"]["CLOUDFLARE_ACCOUNT_ID"]
    check = firm["steps"][0]["run"]
    assert "ready=false" in check and "provisioned" in check


def test_every_committed_tenant_is_rendered_and_shares_nothing():
    ts = tenant.tenants()
    assert [p for t in ts for p in tenant.problems(t)] == []
    assert tenant.collisions(ts) == []
    assert len([t for t in ts if t.primary]) <= 1
    first = tenant.primary_tenant()
    if first is None:
        return
    jim_keys = set(first.vars)
    for t in ts:
        assert jim_keys <= set(t.vars), f"{t.name} has not decided {jim_keys - set(t.vars)}"


def test_the_jsonc_reader_reads_wrangler_as_the_tests_do():
    assert tenant.parse_jsonc(WRANGLER.read_text()) == _jsonc(WRANGLER.read_text())
    text = '{\n  // a comment\n  "url": "https://x.dev/a//b", /* block */\n  "list": [1, 2,],\n}'
    assert tenant.parse_jsonc(text) == {"url": "https://x.dev/a//b", "list": [1, 2]}


# --- a firm's own deployment -----------------------------------------------------


def test_a_new_firm_gets_its_own_everything(repo):
    t = acme(repo)
    jim = tenant.load("jim", repo)
    mine, his = tenant.render(t), tenant.render(jim)
    assert mine["name"] == "second-eye-acme-llp"
    assert mine["d1_databases"] == [{"binding": "DB", "database_name": "second-eye-acme-llp",
                                     "database_id": ""}]
    assert mine["r2_buckets"] == [{"binding": "DOCS",
                                   "bucket_name": "second-eye-acme-llp-docs",
                                   "jurisdiction": "eu"}]
    assert mine["workflows"][0]["name"] == "legal-review-flow-acme-llp"
    assert mine["containers"][0]["instance_type"] == "standard-1"
    # The same code, cron and container class as Jim's.
    for key in ("compatibility_date", "durable_objects", "migrations", "send_email", "triggers"):
        assert mine[key] == his[key]
    # Paths are relative to deployments/acme-llp/, and point where Jim's do.
    assert mine["main"] == "../../cloudflare/src/index.ts"
    assert mine["containers"][0]["image"] == "../../Dockerfile"
    assert mine["containers"][0]["image_build_context"] == "../.."

    v = mine["vars"]
    assert v["MAIL_AGENT_ADDRESS"] == "review@legal.acme-law.com"
    assert v["FIRM_DOMAINS"] == "acme-law.com,acme-law.co.uk"
    assert v["ALLOWED_SENDERS"] == "acme-law.com,acme-law.co.uk"
    assert v["ALLOWLIST_CONTACT"] == "gc@acme-law.com"
    assert v["REPLY_POLICY"] == "sender_only" and v["SERVICE_PAUSED"] == "false"
    assert v["ONE_TAP_LINKS"] == "false"
    assert {"NO_AI_MATTERS", "PLAYBOOK_ADMINS"} <= set(v)
    # Nothing of Jim's identity, and none of his ids, even once he has them.
    for key, value in his["vars"].items():
        if key not in tenant.KNOBS and value and key not in tenant.FIRM_DEFAULTS:
            assert v[key] != value, key
    for key in v:
        if key.endswith("_ID"):
            assert v[key] == "", key
    assert tenant.problems(t) == []
    assert tenant.collisions(tenant.tenants(repo)) == []


def test_a_var_jim_adds_later_is_not_inherited(repo):
    shared = repo / "cloudflare" / "wrangler.jsonc"
    shared.write_text(shared.read_text().replace(
        '"LOG_LEVEL": "INFO"', '"LOG_LEVEL": "INFO",\n    "MANAGED_SOMETHING_ID": "jims_secret"'))
    assert acme(repo).vars["MANAGED_SOMETHING_ID"] == ""


def test_the_rendered_config_is_committed_and_checked(repo):
    t = acme(repo)
    text = t.config_path.read_text()
    assert text.startswith("// Generated by `second-eye tenant render acme-llp`")
    assert "CLOUDFLARE_API_TOKEN_ACME_LLP" in text
    assert tenant.parse_jsonc(text) == tenant.render(t)
    t.file.write_text(tenant.set_field(t.file.read_text(), "TRIAGE", "shadow"))
    t = tenant.load("acme-llp", repo)
    assert any("out of date" in p for p in tenant.problems(t))
    assert tenant.main(["check"], root=repo) == 1
    assert tenant.main(["render", "acme-llp"], root=repo) == 0
    assert tenant.main(["check"], root=repo) == 0
    assert tenant.render(tenant.load("acme-llp", repo))["vars"]["TRIAGE"] == "shadow"


def test_two_firms_sharing_anything_is_a_failure(repo):
    acme(repo)
    other = tenant.new("bravo", address="review@legal.bravo.com", domains="bravo.com",
                       worker="second-eye-bravo", root=repo)
    other.file.write_text(tenant.set_field(other.file.read_text(), "r2_bucket",
                                           "second-eye-acme-llp-docs"))
    other.file.write_text(tenant.set_field(other.file.read_text(),
                                           "MANAGED_FIRM_MEMORY_STORE_ID", "memstore_1"))
    first = tenant.load("acme-llp", repo)
    first.file.write_text(tenant.set_field(first.file.read_text(),
                                           "MANAGED_FIRM_MEMORY_STORE_ID", "memstore_1"))
    found = tenant.collisions(tenant.tenants(repo))
    assert any("R2 bucket second-eye-acme-llp-docs" in f for f in found)
    assert any("MANAGED_FIRM_MEMORY_STORE_ID memstore_1" in f for f in found)


@pytest.mark.parametrize("name, kw, says", [
    ("Acme", {}, "lower-case"),
    ("jim", {}, "already exists"),
    ("acme", {"address": "nope"}, "not an email address"),
    ("acme", {"domains": " , "}, "at least one"),
])
def test_new_refuses_what_would_not_deploy(repo, name, kw, says):
    args = {"address": "review@legal.acme.com", "domains": "acme.com", "root": repo} | kw
    with pytest.raises(tenant.TenantError, match=says):
        tenant.new(name, **args)


def test_the_deploy_matrix_names_each_firms_secrets(repo, capsys):
    acme(repo)
    assert tenant.matrix(repo) == [{"tenant": "acme-llp",
                                    "config": "deployments/acme-llp/wrangler.jsonc",
                                    "secret": "ACME_LLP", "edge_url": "", "provisioned": "false"}]
    assert tenant.main(["matrix"], root=repo) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["include"][0]["secret"] == "ACME_LLP"


def test_no_firm_yet_is_an_empty_matrix(repo, capsys):
    assert tenant.main(["matrix"], root=repo) == 0
    assert capsys.readouterr().out.strip() == ""


# --- provisioning ---------------------------------------------------------------


def _never(argv, cwd, env, stdin):
    raise AssertionError(f"ran {argv}")


def test_provision_is_a_dry_run_that_prints_every_command(repo, capsys):
    t = acme(repo, account_id="acc123")
    assert tenant.main(["provision", "acme-llp"], root=repo, runner=_never) == 0
    out = capsys.readouterr().out
    pre = "(cd cloudflare && CLOUDFLARE_ACCOUNT_ID=acc123 npx wrangler"
    for line in (
        "d1 create second-eye-acme-llp --jurisdiction eu",
        "r2 bucket create second-eye-acme-llp-docs --jurisdiction eu",
        LIFECYCLE_MAIL,
        LIFECYCLE_JOB,
        "deploy --config ../deployments/acme-llp/wrangler.jsonc --containers-rollout=none",
        "secret put DATA_KEY --config ../deployments/acme-llp/wrangler.jsonc",
        "secret put EDGE_SECRET --config ../deployments/acme-llp/wrangler.jsonc",
        "secret put ANTHROPIC_API_KEY --config ../deployments/acme-llp/wrangler.jsonc",
        "email sending enable legal.acme-law.com",
        "email routing rules create legal.acme-law.com",
    ):
        assert f"{pre} {line}" in out, line
    assert "--action-value second-eye-acme-llp" in out
    assert "second-eye live-check --tenant acme-llp" in out
    assert "CLOUDFLARE_API_TOKEN_ACME_LLP and CLOUDFLARE_ACCOUNT_ID_ACME_LLP" in out
    assert len(re.findall(r"^# \d+\. .*\[by hand\]$", out, re.MULTILINE)) == 5
    assert "Dry run: nothing was created or changed" in out and "--apply" in out
    # Nothing written: no key generated, no id.
    assert not t.env_file.exists()
    assert tenant.load("acme-llp", repo).get("d1_database_id") == ""


class Wrangler:
    """Records each command and answers as wrangler would."""

    def __init__(self, fail: str = "", secrets: tuple[str, ...] = ()) -> None:
        self.calls: list[NS] = []
        self.fail = fail
        self.secrets = secrets

    def __call__(self, argv, cwd, env, stdin):
        self.calls.append(NS(argv=argv, cwd=cwd, env=env, stdin=stdin))
        line = " ".join(argv)
        if self.fail and self.fail in line:
            return 1, "✘ [ERROR] something went wrong"
        if argv[2:4] == ["d1", "create"]:
            return 0, ('✅ Successfully created DB\n{\n  "d1_databases": [\n    {\n'
                       f'      "binding": "DB",\n      "database_id": "{D1_ID}"\n    }}\n  ]\n}}')
        if argv[2] == "deploy":
            return 0, ("Uploaded second-eye-acme-llp\n"
                       "  https://second-eye-acme-llp.acme.workers.dev\n")
        if argv[2:4] == ["secret", "list"]:
            return 0, json.dumps([{"name": s, "type": "secret_text"} for s in self.secrets])
        return 0, "ok"

    def lines(self) -> list[str]:
        return [" ".join(c.argv[2:]) for c in self.calls]


def test_apply_creates_the_resources_writes_the_ids_and_keeps_the_key(repo, capsys):
    t = acme(repo, account_id="acc123")
    t.env_file.write_text("ANTHROPIC_API_KEY=sk-ant-firm\n")
    wrangler = Wrangler()
    assert tenant.provision(t, apply=True, runner=wrangler, keygen=lambda: "K" * 44,
                            secret_gen=lambda: "edge-secret") == 0
    assert wrangler.lines() == [
        "d1 create second-eye-acme-llp --jurisdiction eu",
        "r2 bucket create second-eye-acme-llp-docs --jurisdiction eu",
        LIFECYCLE_MAIL,
        LIFECYCLE_JOB,
        "deploy --config ../deployments/acme-llp/wrangler.jsonc --containers-rollout=none",
        "secret list --config ../deployments/acme-llp/wrangler.jsonc --format json",
        "secret put DATA_KEY --config ../deployments/acme-llp/wrangler.jsonc",
        "secret put EDGE_SECRET --config ../deployments/acme-llp/wrangler.jsonc",
        "secret put ANTHROPIC_API_KEY --config ../deployments/acme-llp/wrangler.jsonc",
    ], "the manual steps (mail, DNS, GitHub, Anthropic) are never run"
    assert all(c.cwd == repo / "cloudflare" for c in wrangler.calls)
    assert all(c.env == {"CLOUDFLARE_ACCOUNT_ID": "acc123"} for c in wrangler.calls)
    # Secrets go on stdin, never on the command line.
    puts = {c.argv[4]: c.stdin for c in wrangler.calls if c.argv[2:4] == ["secret", "put"]}
    assert puts == {"DATA_KEY": "K" * 44 + "\n", "EDGE_SECRET": "edge-secret\n",
                    "ANTHROPIC_API_KEY": "sk-ant-firm\n"}
    assert not any("K" * 44 in " ".join(c.argv) for c in wrangler.calls)

    t = tenant.load("acme-llp", repo)
    assert t.get("d1_database_id") == D1_ID
    assert t.vars["EDGE_URL"] == "https://second-eye-acme-llp.acme.workers.dev"
    assert tenant.render(t)["d1_databases"][0]["database_id"] == D1_ID
    assert tenant.problems(t) == [], "the rendered config was written again"
    env = tenant.read_env(t.env_file)
    assert env["DATA_KEY"] == "K" * 44 and env["EDGE_SECRET"] == "edge-secret"
    assert stat.S_IMODE(t.env_file.stat().st_mode) == 0o600
    out = capsys.readouterr().out
    assert "Back it up now" in out and "deployments/acme-llp/.env" in out
    assert "Still to do by hand" in out and "email routing rules create" in out
    assert tenant.matrix(repo)[0]["provisioned"] == "true"


def test_apply_again_never_replaces_a_key_the_worker_already_has(repo, capsys):
    t = acme(repo)
    t.file.write_text(tenant.set_field(t.file.read_text(), "d1_database_id", D1_ID))
    t = tenant.load("acme-llp", repo)
    wrangler = Wrangler(secrets=("DATA_KEY", "EDGE_SECRET"))
    assert tenant.provision(t, apply=True, runner=wrangler,
                            keygen=lambda: pytest.fail("a second key was generated")) == 0
    assert not any(line.startswith("d1 create") for line in wrangler.lines())
    assert not any(line.startswith(("secret put DATA_KEY", "secret put EDGE_SECRET"))
                   for line in wrangler.lines())
    out = capsys.readouterr().out
    assert "DATA_KEY is already set on second-eye-acme-llp; not replaced" in out
    # No Anthropic key in the firm's .env: left for the operator, not guessed.
    assert "ANTHROPIC_API_KEY is not in deployments/acme-llp/.env" in out


def test_apply_reuses_the_key_it_generated_before(repo):
    t = acme(repo)
    t.env_file.write_text("DATA_KEY=kept-from-the-first-run\n")
    wrangler = Wrangler()
    assert tenant.provision(t, apply=True, runner=wrangler,
                            keygen=lambda: pytest.fail("a second key was generated")) == 0
    [put] = [c for c in wrangler.calls if c.argv[2:5] == ["secret", "put", "DATA_KEY"]]
    assert put.stdin == "kept-from-the-first-run\n"


def test_apply_stops_at_the_first_failure(repo, capsys):
    t = acme(repo)
    wrangler = Wrangler(fail="r2 bucket create")
    assert tenant.provision(t, apply=True, runner=wrangler) == 1
    assert wrangler.lines()[-1].startswith("r2 bucket create")
    assert len(wrangler.calls) == 2
    assert "Stopped at step 2" in capsys.readouterr().out
    # The database it did create is recorded, so the next run skips it.
    assert tenant.load("acme-llp", repo).get("d1_database_id") == D1_ID


# --- live-check --tenant ----------------------------------------------------------


def test_live_check_for_a_firm_uses_its_key_and_writes_its_ids(repo, tmp_path, monkeypatch):
    """Jim's key and agent ids are in the shell; the firm's run sees neither,
    creates the firm's agents with the firm's key, and writes the ids into the
    firm's tenant.jsonc and rendered config, leaving Jim's config alone."""
    t = acme(repo)
    t.env_file.write_text("ANTHROPIC_API_KEY=sk-firm\n")
    fs.configure(monkeypatch, ANTHROPIC_API_KEY="sk-jim", MANAGED_REVIEW_AGENT_ID="agent_jims",
                 MANAGED_ENVIRONMENT_ID="env_jims", MANAGED_FIRM_MEMORY_STORE_ID="mem_jims")
    _fake_apply_client(monkeypatch)
    shared = (repo / "cloudflare" / "wrangler.jsonc").read_text()
    jims_config = (repo / "deployments" / "jim" / "tenant.jsonc").read_text()
    seen: dict = {}

    def skills(ctx):
        cfg = settings()
        seen.update(key=cfg.anthropic_api_key, agent=cfg.managed_review_agent_id,
                    domains=cfg.firm_domains)
        livecheck._set_id(ctx, "SANDBOX_SKILL_ID", "skill_firm")
        return livecheck.StepResult(True, "uploaded")

    rec = Recorder()
    code = livecheck.main(["--tenant", "acme-llp"], root=tmp_path / "reports",
                          steps=rec.steps(skills=skills, agents=livecheck.step_agents),
                          tenant_root=repo)
    assert code == 0
    assert seen == {"key": "sk-firm", "agent": "", "domains": "acme-law.com,acme-law.co.uk"}
    t = tenant.load("acme-llp", repo)
    v = t.vars
    assert v["MANAGED_REVIEW_AGENT_ID"] == "agent_1"
    assert v["MANAGED_ENVIRONMENT_ID"] == "env_1"
    assert v["MANAGED_FIRM_MEMORY_STORE_ID"] == "memstore_1"
    assert v["SANDBOX_SKILL_ID"] == "skill_firm"
    assert tenant.problems(t) == [], "the rendered config carries the ids too"
    assert tenant.render(t)["vars"]["MANAGED_REVIEW_AGENT_ID"] == "agent_1"
    assert "MANAGED_REVIEW_AGENT_ID=agent_1" in t.env_file.read_text()
    assert (repo / "deployments" / "jim" / "tenant.jsonc").read_text() == jims_config
    assert (repo / "cloudflare" / "wrangler.jsonc").read_text() == shared
    # The shell is as it was once the run is over.
    assert settings().anthropic_api_key == "sk-jim"
    assert settings().managed_review_agent_id == "agent_jims"


@pytest.mark.parametrize("argv", [["--tenant", "jim"], []])
def test_live_check_for_jim_writes_into_his_tenant(repo, monkeypatch, tmp_path, argv):
    """With --tenant jim, or with no tenant at all, the ids go into the
    primary deployment's tenant.jsonc and its config is rendered again; the
    shared config, which is published, is never written."""
    shared = repo / "cloudflare" / "wrangler.jsonc"
    before = shared.read_text()
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=sk-test\n")
    fs.configure(monkeypatch, MANAGED_REVIEW_AGENT_ID="", MANAGED_ASSOCIATE_AGENT_ID="",
                 MANAGED_ENVIRONMENT_ID="", MANAGED_FIRM_MEMORY_STORE_ID="")
    _fake_apply_client(monkeypatch)
    rec = Recorder()
    assert livecheck.main(argv, root=tmp_path / "reports", env_file=env,
                          steps=rec.steps(agents=livecheck.step_agents), tenant_root=repo) == 0
    jim = tenant.load("jim", repo)
    assert jim.vars["MANAGED_REVIEW_AGENT_ID"] == "agent_1"
    assert _jsonc(jim.config_path.read_text())["vars"]["MANAGED_REVIEW_AGENT_ID"] == "agent_1"
    assert tenant.problems(jim) == []
    assert shared.read_text() == before


def test_live_check_names_a_tenant_that_does_not_exist(repo, capsys):
    assert livecheck.main(["--tenant", "nobody", "--dry-run"], tenant_root=repo) == 2
    assert "no tenant 'nobody'" in capsys.readouterr().out
