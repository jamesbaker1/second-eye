"""The self-hosted option (docs/it/self-hosted.md).

A firm whose IT runs Second Eye in the firm's own Cloudflare account and
Anthropic organisation: never in deploy.yml's matrix, deployed by the firm
from a release, paused and resumed without a deploy, given support access
only by a token it creates and revokes, and offboarded with a deletion
certificate. Nothing here runs wrangler or reaches Anthropic: commands go
through a recording runner and the API through a fake.
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import io
import json
import re
import tarfile
from datetime import date
from types import SimpleNamespace as NS

import pytest

from secondeye import clients, killswitch, offboard, release, support, tenant
from secondeye.store import connect
from tests.test_tenant import ROOT, Wrangler, _never, acme, make_repo

ACCOUNT = "0123456789abcdef0123456789abcdef"
D1 = "0c5b1a7e-9d55-4a5e-8a1f-3f1f0c2d4e6b"


@pytest.fixture
def repo(tmp_path):
    """As in test_tenant: the real shared config and a primary tenant."""
    return make_repo(tmp_path)


class Recorder(Wrangler):
    """Any command, not only wrangler's."""

    def __call__(self, argv, cwd, env, stdin):
        if argv[0] != "npx":
            self.calls.append(NS(argv=argv, cwd=cwd, env=env, stdin=stdin))
            return 0, "ok"
        return super().__call__(argv, cwd, env, stdin)


def firm(repo, **kw) -> tenant.Tenant:
    return acme(repo, self_hosted=True, account_id=ACCOUNT, anthropic_org="org_acme",
                anthropic_workspace="wrkspc_acme", **kw)


def provisioned(repo) -> tenant.Tenant:
    t = firm(repo)
    tenant._update(t, "d1_database_id", D1)
    tenant._update(t, "EDGE_URL", "https://second-eye-acme-llp.acme.workers.dev")
    return tenant.load("acme-llp", repo)


# --- the tenant ---------------------------------------------------------------


def test_a_self_hosted_firm_is_marked_and_names_its_own_accounts(repo):
    t = firm(repo)
    assert t.self_hosted and t.account_id == ACCOUNT
    data = t.data
    assert data["anthropic_organization"] == "org_acme"
    assert data["anthropic_workspace"] == "wrkspc_acme"
    text = t.file.read_text()
    assert "Self-hosted" in text and "no standing access" in text
    rendered = t.config_path.read_text()
    assert "Never by our deploy.yml" in rendered and "CLOUDFLARE_API_TOKEN_" not in rendered
    assert tenant.problems(t) == []
    # A firm we host is unchanged: no self_hosted key at all.
    hosted = tenant.new("globex", address="review@legal.globex.com", domains="globex.com",
                        root=repo)
    assert "self_hosted" not in hosted.data and not hosted.self_hosted


@pytest.mark.parametrize("kw, says", [
    ({"self_hosted": True}, "--account-id"),
    ({"self_hosted": True, "account_id": "not-an-id"}, "--account-id"),
    ({"anthropic_org": "org_x"}, "for --self-hosted"),
])
def test_self_hosted_needs_the_firms_own_account(repo, kw, says):
    with pytest.raises(tenant.TenantError, match=says):
        acme(repo, **kw)


def test_self_hosted_firms_are_not_in_the_deploy_matrix(repo, capsys):
    firm(repo)
    tenant.new("globex", address="review@legal.globex.com", domains="globex.com", root=repo)
    assert [row["tenant"] for row in tenant.matrix(repo)] == ["globex"]
    assert tenant.main(["self-hosted"], root=repo) == 0
    out = capsys.readouterr().out
    assert "acme-llp: self-hosted, so not deployed here" in out and ACCOUNT in out
    assert "second-eye tenant deploy acme-llp --release vX.Y.Z" in out
    assert tenant.main(["list"], root=repo) == 0
    assert "self-hosted: the firm deploys" in capsys.readouterr().out


def test_no_self_hosted_firm_is_said_plainly(repo, capsys):
    assert tenant.main(["self-hosted"], root=repo) == 0
    assert capsys.readouterr().out.strip() == "no self-hosted firm"


def test_provisioning_a_self_hosted_firm_ends_in_a_release_not_github_secrets(repo, capsys):
    firm(repo)
    assert tenant.main(["provision", "acme-llp"], root=repo, runner=_never) == 0
    out = capsys.readouterr().out
    assert f"CLOUDFLARE_ACCOUNT_ID={ACCOUNT} npx wrangler d1 create" in out
    assert "second-eye tenant deploy acme-llp --release '<vX.Y.Z>'" in out
    assert "gh secret set" not in out and "CLOUDFLARE_API_TOKEN_ACME_LLP" not in out
    assert "rolls out with `second-eye tenant deploy`" in out


def test_deploy_yml_says_which_firms_deploy_themselves():
    import yaml

    jobs = yaml.safe_load((ROOT / ".github" / "workflows" / "deploy.yml").read_text())["jobs"]
    runs = [s.get("run", "") for s in jobs["tenants"]["steps"]]
    assert any("secondeye.tenant matrix" in r for r in runs)
    assert any("secondeye.tenant self-hosted" in r for r in runs)


# --- releases -------------------------------------------------------------------


def test_the_release_workflow_builds_a_draft_and_deploys_nothing():
    import yaml

    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text())
    on = wf[True] if True in wf else wf["on"]   # YAML 1.1 reads `on` as true
    assert on == {"push": {"tags": ["v*"]}}
    assert wf["permissions"] == {"contents": "write"}
    steps = wf["jobs"]["release"]["steps"]
    runs = "\n".join(s.get("run", "") for s in steps)
    assert "merge-base --is-ancestor" in runs
    assert "secondeye.release build --version" in runs
    assert "wrangler deploy --dry-run" in runs and "secondeye.release pack" in runs
    assert "secondeye.release checksums dist" in runs
    assert "gh release create" in runs and "--draft" in runs and "--verify-tag" in runs
    assert any(s.get("uses", "").startswith("anchore/sbom-action") for s in steps)
    # Never a real deploy, and no Cloudflare or Anthropic credential anywhere.
    assert not re.search(r"wrangler deploy(?! --dry-run)", runs)
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert "CLOUDFLARE_API_TOKEN" not in text and "ANTHROPIC" not in text
    # Checksums come last, so they cover everything attached.
    names = [s.get("name", "") for s in steps]
    assert names.index("Checksums") == len(names) - 2


def test_the_source_archive_is_reproducible_and_carries_no_firm(tmp_path):
    first, record = release.source_archive("v1.2.3", ROOT)
    again, _ = release.source_archive("v1.2.3", ROOT)
    assert first == again, "the same tag gives the same bytes"
    assert record["version"] == "v1.2.3" and re.match(r"^[0-9a-f]{40}$", record["commit"])
    with tarfile.open(fileobj=io.BytesIO(first)) as tar:
        names = tar.getnames()
        assert all(n.startswith("second-eye-v1.2.3/") for n in names)
        assert not [n for n in names if "/deployments/" in n]
        assert "second-eye-v1.2.3/RELEASE" in names
        assert "second-eye-v1.2.3/Dockerfile" in names
        shared = tar.extractfile("second-eye-v1.2.3/cloudflare/wrangler.jsonc").read().decode()
        assert {m.mtime for m in tar.getmembers()} == {tar.getmembers()[0].mtime}
    assert gzip.decompress(first)  # a real gzip
    v = tenant.parse_jsonc(shared)["vars"]
    for key in ("EDGE_URL", "MAIL_AGENT_ADDRESS", "ALLOWED_SENDERS", "FIRM_DOMAINS"):
        assert v[key] == "", key
    assert v["SERVICE_PAUSED"] == "false" and v["REPLY_POLICY"] == "sender_only"
    assert v["REVIEW_MODEL"]  # the knobs keep their values
    assert tenant.parse_jsonc(shared)["d1_databases"][0]["database_id"] == ""
    for t in tenant.tenants(ROOT):
        for key in ("EDGE_URL", "MAIL_AGENT_ADDRESS", "ALLOWED_SENDERS", "FIRM_DOMAINS"):
            if t.vars.get(key):
                assert t.vars[key] not in shared, (t.name, key)


def test_release_files_are_checked_against_their_checksums(tmp_path):
    written = release.build("v1.2.3", tmp_path, ROOT)
    assert sorted(p.name for p in written) == [
        "release.json",
        "second-eye-v1.2.3-source.tar.gz", "second-eye-v1.2.3-wrangler.template.jsonc"]
    manifest = json.loads((tmp_path / "release.json").read_text())
    assert manifest["excluded_from_source"] == ["deployments/"]
    assert any("prebuilt container image" in n for n in manifest["not_included"])
    assert any("not who listed them" in n for n in manifest["not_included"])
    template = tenant.parse_jsonc((tmp_path / written[1].name).read_text())
    assert template["name"] == "second-eye-<firm>"
    assert template["vars"]["MAIL_AGENT_ADDRESS"] == "<review@legal.firm.com>"
    sums = release.checksums(tmp_path).read_text()
    assert any(re.match(r"^[0-9a-f]{64}  second-eye-v1\.2\.3-source\.tar\.gz$", line) for line in sums.splitlines())
    assert release.verify(tmp_path) == []
    (tmp_path / "release.json").write_text("{}")
    assert release.verify(tmp_path) == ["release.json: checksum does not match"]
    with pytest.raises(release.ReleaseError):
        release.build("latest", tmp_path, ROOT)


def test_pack_is_deterministic_and_leaves_only_the_archive(tmp_path):
    for n in (1, 2):
        d = tmp_path / f"w{n}" / "worker"
        d.mkdir(parents=True)
        (d / "index.js").write_text("export default {}")
        (d / "index.js.map").write_text("{}")
        release.pack(d, tmp_path / f"w{n}.tar.gz")
        assert not d.exists()
    assert (tmp_path / "w1.tar.gz").read_bytes() == (tmp_path / "w2.tar.gz").read_bytes()


def _release_tree(repo, version="v1.2.3"):
    (repo / "RELEASE").write_text(json.dumps({"product": "second-eye", "version": version,
                                              "commit": "a" * 40}))


def test_deploying_a_release_is_a_dry_run_that_prints_every_step(repo, capsys):
    t = provisioned(repo)
    _release_tree(repo)
    assert tenant.main(["deploy", "acme-llp", "--release", "v1.2.3"], root=repo,
                       runner=_never) == 0
    out = capsys.readouterr().out
    assert f"account {ACCOUNT}" in out
    assert "(cd cloudflare && npm ci)" in out
    assert (f"(cd cloudflare && CLOUDFLARE_ACCOUNT_ID={ACCOUNT} npx wrangler deploy --config "
            "../deployments/acme-llp/wrangler.jsonc)") in out
    assert f"curl --fail --silent --show-error {t.vars['EDGE_URL']}/health" in out
    assert "Dry run: nothing was deployed" in out and "Docker must be running" in out


def test_deploying_a_release_runs_with_the_firms_token_and_account(repo, capsys):
    t = provisioned(repo)
    _release_tree(repo)
    run = Recorder()
    assert release.deploy(t, "v1.2.3", apply=True, runner=run,
                          environ={"CLOUDFLARE_API_TOKEN": "theirs"}) == 0
    assert [c.argv[:3] for c in run.calls] == [["npm", "ci"], ["npx", "wrangler", "deploy"],
                                               ["curl", "--fail", "--silent"]]
    assert all(c.env == {"CLOUDFLARE_ACCOUNT_ID": ACCOUNT} for c in run.calls)
    assert "Done: acme-llp runs v1.2.3." in capsys.readouterr().out


@pytest.mark.parametrize("setup, environ, says", [
    (lambda repo: None, {"CLOUDFLARE_API_TOKEN": "x"}, "This is not a release"),
    (lambda repo: _release_tree(repo, "v1.2.2"), {"CLOUDFLARE_API_TOKEN": "x"},
     "This tree is release v1.2.2, not v1.2.3"),
    (lambda repo: _release_tree(repo), {}, "CLOUDFLARE_API_TOKEN is not set"),
])
def test_deploy_refuses_the_wrong_tree_or_no_token(repo, capsys, setup, environ, says):
    t = provisioned(repo)
    setup(repo)
    assert release.deploy(t, "v1.2.3", apply=True, runner=_never, environ=environ) == 2
    assert says in capsys.readouterr().out


def test_deploy_refuses_files_that_do_not_match_their_checksums(repo, tmp_path, capsys):
    t = provisioned(repo)
    _release_tree(repo)
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "release.json").write_text("{}")
    release.checksums(assets)
    (assets / "release.json").write_text('{"changed": true}')
    assert release.deploy(t, "v1.2.3", assets=assets, runner=_never) == 1
    assert "do not deploy them" in capsys.readouterr().out


def test_deploy_is_for_self_hosted_firms_only(repo, capsys):
    t = acme(repo)
    assert release.deploy(t, "v1.2.3", runner=_never) == 2
    assert "deploy.yml deploys it" in capsys.readouterr().out


# --- the kill switch ---------------------------------------------------------------


def test_the_sql_is_the_table_the_worker_reads():
    ts = (ROOT / "cloudflare" / "src" / "pause.ts").read_text()
    schema = "".join(re.findall(r'"([^"]*)"', ts.split("SETTINGS_SCHEMA =")[1].split(";")[0]))
    assert schema == killswitch.SCHEMA
    assert f'PAUSE_KEY = "{killswitch.KEY}"' in ts


def test_pause_is_a_dry_run_that_prints_the_command(repo, capsys):
    provisioned(repo)
    assert killswitch.main("pause", ["acme-llp", "--by", "it@acme-law.com"], root=repo,
                           runner=_never) == 0
    out = capsys.readouterr().out
    assert (f"(cd cloudflare && CLOUDFLARE_ACCOUNT_ID={ACCOUNT} npx wrangler d1 execute "
            "second-eye-acme-llp --remote --config ../deployments/acme-llp/wrangler.jsonc"
            ' --yes --command "CREATE TABLE IF NOT EXISTS edge_settings') in out
    assert "VALUES ('service_paused', 'true', " in out and "'it@acme-law.com')" in out
    assert "held sealed, nothing is sent" in out
    assert "Dry run: nothing was changed" in out and "D1 console" in out


def test_resume_says_how_to_release_held_mail_and_when_the_var_wins(repo, capsys):
    t = provisioned(repo)
    tenant._update(t, "SERVICE_PAUSED", "true")
    assert killswitch.main("resume", ["acme-llp"], root=repo, runner=_never) == 0
    out = capsys.readouterr().out
    assert "'service_paused', 'false'" in out
    assert "the var wins" in out
    assert ('curl -X POST -H "Authorization: Bearer $EDGE_SECRET" '
            "https://second-eye-acme-llp.acme.workers.dev/internal/release") in out


def test_pause_apply_writes_the_row_with_the_firms_account(repo, capsys):
    t = provisioned(repo)
    run = Wrangler()
    assert killswitch.run(t, True, apply=True, by="it'; DROP TABLE x;--", runner=run,
                          at="2026-10-04T10:00:00+00:00") == 0
    write, read = run.calls
    assert write.env == {"CLOUDFLARE_ACCOUNT_ID": ACCOUNT}
    sql = write.argv[write.argv.index("--command") + 1]
    assert sql.endswith("updated_by = excluded.updated_by")
    assert "'it DROP TABLE x--'" in sql and "';" not in sql.split("VALUES")[1]
    assert "--json" in read.argv
    assert "acme-llp is paused." in capsys.readouterr().out


def test_jims_deployment_can_be_paused_the_same_way(repo, capsys):
    assert killswitch.main("pause", ["jim"], root=repo, runner=_never) == 0
    assert ("npx wrangler d1 execute legal-review-agent --remote "
            "--config ../deployments/jim/wrangler.jsonc") in capsys.readouterr().out


# --- support access ------------------------------------------------------------------


def test_support_access_is_a_token_the_firm_creates_scoped_and_expiring(repo):
    t = firm(repo)
    text = support.recipe(t, days=3, today=date(2026, 10, 4))
    assert "Second Eye support 2026-10-04 (expires 2026-10-07)" in text
    assert f"Include | {ACCOUNT} only" in text
    assert "Workers Tail | Read" in text and "| Write" not in text
    assert "Workers R2 Storage" in text.split("Never in it:")[1]
    assert "Delete" in text and "Audit Log" in text
    wider = support.recipe(t, days=3, write=True, today=date(2026, 10, 4))
    assert "D1 | Write" in wider and "reading client material" in wider


def test_support_is_bounded_and_only_for_self_hosted(repo, capsys):
    assert support.main(firm(repo), days=30) == 2
    assert "At most 14 days" in capsys.readouterr().out
    assert support.main(tenant.new("globex", address="review@legal.globex.com",
                                   domains="globex.com", root=repo)) == 2
    assert "hosted by us" in capsys.readouterr().out


# --- offboarding ------------------------------------------------------------------------


class FakeAnthropic:
    """What offboarding calls, recorded. 'gone' ids answer 404."""

    def __init__(self, fail: str = "") -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail = fail
        b = NS()
        rec = self._rec
        b.sessions = NS(list=lambda agent_id, **kw: [NS(id=f"sesn_of_{agent_id}")],
                        retrieve=lambda i: NS(status="idle"), delete=rec("sessions.delete"))
        b.files = NS(list=lambda scope_id, **kw: [NS(id=f"file_by_{scope_id}")],
                     delete=rec("files.delete"))
        b.memory_stores = NS(delete=rec("memory_stores.delete"))
        b.vaults = NS(list=lambda **kw: [NS(id="vlt_ours", metadata={"lra": "dms"}),
                                         NS(id="vlt_not_ours", metadata={})],
                      delete=rec("vaults.delete"))
        b.skills = NS(versions=NS(list=lambda skill_id: [NS(id=f"{skill_id}_v1")],
                                  delete=lambda v, skill_id: self.calls.append(
                                      ("skills.versions.delete", v))),
                      delete=rec("skills.delete"))
        b.environments = NS(delete=rec("environments.delete"))
        b.agents = NS(archive=rec("agents.archive"))
        self.client = NS(beta=b)

    def _rec(self, name):
        def call(ident, **kw):
            if self.fail and self.fail == ident:
                raise RuntimeError("Anthropic is down")
            self.calls.append((name, ident))
        return call


@pytest.fixture
def held(repo, tmp_path, monkeypatch):
    """A self-hosted firm with a client, an audit row naming a session and an
    upload, a memory store, a vault, and DATA_KEY in its local .env."""
    import secondeye.config
    from secondeye import audit, memory
    from secondeye.config import settings

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/firm.sqlite3")
    # The per-client purges reach Anthropic as `second-eye purge` does, through config.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(secondeye.config, "anthropic_client", lambda: FakeAnthropic().client)
    settings.cache_clear()
    t = provisioned(repo)
    for key, value in {"MANAGED_REVIEW_AGENT_ID": "agent_review",
                       "MANAGED_ENVIRONMENT_ID": "env_acme",
                       "MANAGED_FIRM_MEMORY_STORE_ID": "memstore_firm",
                       "SANDBOX_SKILL_ID": "skill_tools"}.items():
        tenant._update(t, key, value)
    clients.register("100", "Acme Corporation", domains=["acme.com"])
    audit.init()
    memory.init()
    with connect() as c:
        c.execute("INSERT INTO audit_log (job_id, received_at, sender, documents, session_ids, "
                  "file_ids) VALUES ('job-1', '2026-10-01', 'jim@acme-law.com', "
                  "'[\"Acme SPA.docx\"]', '[\"sesn_job1\"]', '[\"file_up1\"]')")
        c.execute("INSERT INTO memory_stores (scope, scope_key, store_id) VALUES "
                  "('client', '100', 'memstore_client100')")
    t = tenant.load("acme-llp", repo)
    t.env_file.write_text("DATA_KEY=secretkey\nEDGE_SECRET=s\nANTHROPIC_API_KEY=sk-firm\n"
                          "LOG_LEVEL=DEBUG\n")
    yield t
    settings.cache_clear()


def _run(t, fake, **kw):
    return offboard.run(t, client_factory=lambda: fake.client,
                        environment=lambda t: contextlib.nullcontext(), **kw)


def test_offboard_is_a_dry_run_that_lists_everything(held, capsys):
    fake = FakeAnthropic()
    assert _run(held, fake, runner=_never) == 0
    out = capsys.readouterr().out
    assert "nothing has been deleted" in out
    assert "Purge 1 registered client(s): 100" in out
    for line in ("delete   2 session(s): sesn_job1, sesn_of_agent_review",
                 "delete   1 file(s): file_up1",
                 "delete   2 memory_store(s): memstore_firm, memstore_client100",
                 "delete   1 vault(s): vlt_ours",
                 "delete   1 skill(s): skill_tools",
                 "delete   1 environment(s): env_acme",
                 "archive   1 agent(s): agent_review"):
        assert line in out, line
    pre = f"(cd cloudflare && CLOUDFLARE_ACCOUNT_ID={ACCOUNT} npx wrangler"
    for line in ("delete --config ../deployments/acme-llp/wrangler.jsonc --force",
                 "workflows delete legal-review-flow-acme-llp",
                 "containers list --json",
                 "d1 delete second-eye-acme-llp -y",
                 ("r2 bucket lifecycle add second-eye-acme-llp-docs "
                  "offboard-expire-everything '' --expire-days 1 -y --jurisdiction eu"),
                 "r2 bucket delete second-eye-acme-llp-docs --jurisdiction eu"):
        assert f"{pre} {line}" in out, line
    assert "DATA_KEY in deployments/acme-llp/.env" in out
    assert "Every other copy of DATA_KEY" in out and "wrkspc_acme" in out
    assert "vlt_not_ours" not in out, "only what is provably this deployment's"
    assert fake.calls == [], "a dry run deletes nothing"
    assert "DATA_KEY=secretkey" in held.env_file.read_text()


def test_offboard_apply_deletes_everything_and_certifies_it(held, capsys):
    fake = FakeAnthropic()
    run = Wrangler()
    key = b"the-firms-own-signing-key"
    assert _run(held, fake, apply=True, key=key, actor="it@acme-law.com", runner=run) == 0

    # Paused first, Anthropic before Cloudflare, sessions before what they used.
    lines = run.lines()
    assert lines[0].startswith("d1 execute second-eye-acme-llp --remote")
    assert "'service_paused', 'true'" in lines[0]
    assert [line.split(" --")[0] for line in lines[2:]] == [
        "delete", "workflows delete legal-review-flow-acme-llp", "containers list",
        "d1 delete second-eye-acme-llp -y",
        "r2 bucket lifecycle add second-eye-acme-llp-docs offboard-expire-everything ",
        "r2 bucket delete second-eye-acme-llp-docs"]
    kinds = [name for name, _ in fake.calls]
    assert kinds.index("sessions.delete") < kinds.index("memory_stores.delete") < \
        kinds.index("environments.delete") < kinds.index("agents.archive")
    assert ("files.delete", "file_by_sesn_job1") in fake.calls
    assert ("skills.versions.delete", "skill_tools_v1") in fake.calls
    assert ("vaults.delete", "vlt_not_ours") not in fake.calls

    # DATA_KEY and the other secrets are gone from this machine; the rest stays.
    assert held.env_file.read_text() == "LOG_LEVEL=DEBUG\n"
    export = next((held.dir / "offboarding").glob("audit-*.csv"))
    assert "job-1" in export.read_text()

    path = next((held.dir / "offboarding").glob("deletion-certificate-*.json"))
    cert = json.loads(path.read_text())
    body = cert["certificate"]
    assert body["type"] == "second-eye-deletion-certificate" and body["complete"] is True
    assert body["tenant"] == "acme-llp" and body["cloudflare_account"] == ACCOUNT
    assert body["anthropic_workspace"] == "wrkspc_acme" and body["run_by"] == "it@acme-law.com"
    assert body["counts"]["anthropic session"] == 2
    assert body["counts"]["cloudflare d1 database"] == 1
    assert body["counts"]["local secret"] == 3
    assert body["client_purges"][0]["client"] == "100"
    assert any(d["kind"] == "agent" and d["id"] == "agent_review" and d["status"] == "archived"
               for d in body["deleted"])
    assert all(d["at"] for d in body["deleted"])
    assert body["by_hand"] and body["not_deleted"] == []
    # No contents: not a client's name, not a document's.
    text = path.read_text()
    assert "Acme Corporation" not in text and "Acme SPA" not in text
    assert "secretkey" not in text and "sk-firm" not in text
    # Intact and signed; any change shows.
    assert cert["sha256"] == hashlib.sha256(offboard.canonical(body)).hexdigest()
    assert offboard.check(cert, key) == []
    assert offboard.check(cert, b"someone-elses-key-x") == ["hmac_sha256 does not match this key"]
    body["counts"]["anthropic session"] = 3
    assert "the certificate was changed" in offboard.check(cert)[0]


def test_the_certificate_is_checked_from_the_command_line(held, tmp_path, capsys):
    key_file = tmp_path / "key"
    key_file.write_text("0123456789abcdef0123")
    assert _run(held, FakeAnthropic(), apply=True, key=offboard.read_key(key_file),
                runner=Wrangler()) == 0
    path = next((held.dir / "offboarding").glob("deletion-certificate-*.json"))
    capsys.readouterr()
    assert tenant.main(["certificate", str(path), "--key-file", str(key_file)],
                       root=held.root) == 0
    assert "intact: acme-llp offboarded" in capsys.readouterr().out
    unsigned = offboard.certificate(held, offboard.Offboarding("acme-llp"), actor="x")
    assert "hmac_sha256" not in unsigned and offboard.check(unsigned) == []
    assert offboard.check(unsigned, b"0123456789abcdef0123") == ["not signed (no hmac_sha256)"]


def test_a_failure_at_anthropic_deletes_nothing_at_cloudflare(held, capsys):
    fake = FakeAnthropic(fail="env_acme")
    run = Wrangler()
    assert _run(held, fake, apply=True, runner=run) == 1
    assert [line for line in run.lines() if "d1 execute" not in line] == []
    assert "DATA_KEY=secretkey" in held.env_file.read_text(), "the key stays until it is all gone"
    out = capsys.readouterr().out
    assert "nothing at Cloudflare was deleted" in out
    cert = json.loads(next((held.dir / "offboarding").glob("deletion-*.json")).read_text())
    assert cert["certificate"]["complete"] is False
    assert cert["certificate"]["not_deleted"][0]["id"] == "env_acme"


def test_a_bucket_still_emptying_is_finished_by_running_again(held, capsys):
    run = Wrangler(fail="r2 bucket delete")
    run_fail = run

    def runner(argv, cwd, env, stdin):
        code, out = run_fail(argv, cwd, env, stdin)
        return (code, "The bucket you tried to delete is not empty (10008)") if code else (code, out)

    assert _run(held, FakeAnthropic(), apply=True, runner=runner) == 1
    out = capsys.readouterr().out
    assert "pending" in out and "lifecycle rule expires every object" in out


def test_offboard_will_not_touch_jims_deployment(repo, capsys):
    assert offboard.run(tenant.load("jim", repo), runner=_never) == 2
    assert "the primary deployment" in capsys.readouterr().out


def test_cli_lists_the_new_commands():
    from secondeye import cli

    for words in ("second-eye tenant deploy", "second-eye tenant support",
                  "second-eye tenant offboard", "second-eye pause <firm>"):
        assert words in cli.USAGE


def test_the_new_modules_import_only_the_standard_library_at_the_top():
    """release.yml runs `python -m secondeye.release` on a bare runner."""
    for name in ("release.py", "tenant.py"):
        top = (ROOT / "src" / "secondeye" / name).read_text().split("\ndef ")[0]
        for line in top.splitlines():
            if line.startswith(("import ", "from ")):
                mod = line.split()[1].split(".")[0]
                assert mod in {"__future__", "argparse", "contextlib", "gzip", "hashlib", "io",
                               "json", "os", "re", "subprocess", "sys", "tarfile", "pathlib",
                               "collections", "dataclasses", "secondeye"}, line

