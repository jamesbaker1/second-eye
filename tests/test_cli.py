"""The local driver. Its two jobs are to send nothing and to explain itself."""

import sys

from lra import cli


def test_replay_with_no_file_prints_usage_instead_of_a_traceback(monkeypatch, capsys):
    """A new contributor mistyping the command from the README got an
    IndexError, from a function that already knew the usage line."""
    monkeypatch.setattr(sys, "argv", ["lra", "replay"])
    assert cli.main() == 1
    assert "usage: lra replay" in capsys.readouterr().out


def test_no_command_prints_usage(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["lra"])
    assert cli.main() == 1
    assert "usage: lra replay" in capsys.readouterr().out


def test_an_unknown_command_says_so(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["lra", "reviewify"])
    assert cli.main() == 1
    assert "unknown command: reviewify" in capsys.readouterr().out


def test_a_live_review_without_the_agent_ids_fails_in_one_sentence(monkeypatch, tmp_path, capsys):
    """This is how the owner does the first live run, so a missing id has to
    be a sentence naming it, not a traceback from inside the SDK."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("MANAGED_REVIEW_AGENT_ID", "")
    from lra.config import settings

    settings.cache_clear()
    try:
        monkeypatch.setattr(sys, "argv", ["lra", "review", "--live", "samples/simple.docx"])
        assert cli.main() == 1
        out = capsys.readouterr().out
        assert "MANAGED_REVIEW_AGENT_ID" in out and "lra agents apply" in out
        assert out.count("\n") <= 2
    finally:
        settings.cache_clear()


def test_a_live_review_polls_to_the_end_with_no_webhook(monkeypatch, tmp_path, capsys):
    """The clientless session runs from a laptop, because the CLI polls
    rather than waiting to be called back."""
    import json
    import shutil

    from lra import managed
    from tests import fake_sessions as fs

    fs.configure(monkeypatch, DATABASE_URL=f"sqlite:///{tmp_path}/live.sqlite3")
    monkeypatch.setattr(managed.time, "sleep", lambda s: None)
    findings = {"summary": "One thing.", "findings": [
        {"severity": "blocker", "category": "party", "title": "Wrong party",
         "explanation": "", "anchor": "Acme"}]}
    fake = fs.DetachedSessions([fs.Turn(fs.idle(), polls=3, outputs={
        "findings.json": json.dumps(findings).encode()})])
    monkeypatch.setattr(managed, "anthropic_client", lambda: fake.client)
    target = tmp_path / "NDA.docx"
    shutil.copy("samples/simple.docx", target)
    try:
        monkeypatch.setattr(sys, "argv", ["lra", "review", "--live", str(target)])
        assert cli.main() == 0
    finally:
        from lra.config import settings

        settings.cache_clear()
    out = capsys.readouterr().out
    assert "started sesn_fake: https://platform.claude.com/" in out
    assert "[blocker] Wrong party" in out
    assert fake.created[0]["agent"] == "agent_review"
    assert fake.retrieves >= 4 and fake.deleted_sessions == ["sesn_fake"]


def test_agents_apply_creates_the_agents_and_prints_the_ids(monkeypatch, capsys):
    from types import SimpleNamespace as NS

    import lra.config
    from tests import fake_sessions as fs

    fs.configure(monkeypatch, MANAGED_REVIEW_AGENT_ID="", MANAGED_ASSOCIATE_AGENT_ID="",
                 MANAGED_REVIEW_DETACHED_AGENT_ID="",
                 MANAGED_ENVIRONMENT_ID="", MANAGED_FIRM_MEMORY_STORE_ID="",
                 SANDBOX_SKILL_ID="skill_tools")
    created: list[dict] = []

    def create_agent(**kw):
        created.append(kw)
        return NS(id=f"agent_{len(created)}", name=kw["name"], version=1)

    from tests import api_contract as contract

    client = contract.strict(NS(beta=NS(
        agents=NS(create=create_agent, update=lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("update called on a first apply"))),
        environments=NS(create=lambda **kw: NS(id="env_1", name=kw["name"])),
        memory_stores=NS(create=lambda **kw: NS(id="memstore_1")),
    )))
    monkeypatch.setattr(lra.config, "anthropic_client", lambda: client)
    try:
        monkeypatch.setattr(sys, "argv", ["lra", "agents", "apply"])
        assert cli.main() == 0
    finally:
        from lra.config import settings

        settings.cache_clear()
    out = capsys.readouterr().out
    for key in ("MANAGED_ENVIRONMENT_ID=env_1", "MANAGED_REVIEW_AGENT_ID=agent_1",
                "MANAGED_ASSOCIATE_AGENT_ID=agent_2", "MANAGED_FIRM_MEMORY_STORE_ID=memstore_1"):
        assert key in out
    [reviewer] = [a for a in created if a["name"] == "LRA reviewer"]
    assert created.index(reviewer) == 0
    assert not [t for t in reviewer["tools"] if t.get("type") == "custom"], \
        "a custom tool would leave a clientless session waiting for ever"
    assert "MANAGED_REVIEW_DETACHED_AGENT_ID" not in out
    names = {t["name"] for a in created for t in a["tools"] if t.get("type") == "custom"}
    # The associate still answers its own tools over the stream.
    assert {"make_changes", "note_for_next_time"} <= names
    assert "report_findings" not in names
    assert all("skill_tools" in [s["skill_id"] for s in a["skills"]] for a in created)
    assert all(a["model"]["effort"] == "high" for a in created)


def test_replay_never_reaches_a_live_mail_provider(monkeypatch, tmp_path, capsys):
    """README promises nothing is sent. The handler used to resolve its own
    provider from MAIL_PROVIDER, so a developer with postmark in their .env
    mailed a real redline to whoever was in the .eml."""
    monkeypatch.setenv("MAIL_PROVIDER", "postmark")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/cli.sqlite3")
    from lra.config import settings

    settings.cache_clear()
    try:
        used = []
        monkeypatch.setattr(cli, "handle", lambda email, provider=None: used.append(provider))
        eml = tmp_path / "m.eml"
        eml.write_bytes(
            b"From: jim@firm.com\r\nTo: review@firm.com\r\nSubject: hello\r\n"
            b"Message-ID: <1@firm.com>\r\n\r\nhave a look\r\n"
        )
        monkeypatch.setattr(sys, "argv", ["lra", "replay", str(eml)])
        assert cli.main() == 0
        assert used and isinstance(used[0], cli.ConsoleProvider)
    finally:
        settings.cache_clear()


def _apply(monkeypatch, capsys, **env):
    from types import SimpleNamespace as NS

    import lra.config
    from tests import fake_sessions as fs

    fs.configure(monkeypatch, MANAGED_FIRM_MEMORY_STORE_ID="memstore_1", **env)
    updated: list[tuple[str, dict]] = []
    from tests import api_contract as contract

    client = contract.strict(NS(beta=NS(
        agents=NS(create=lambda **kw: (_ for _ in ()).throw(AssertionError("created")),
                  update=lambda agent_id, **kw: updated.append((agent_id, kw))
                  or NS(id=agent_id, name=kw["name"], version=2)),
        environments=NS(retrieve=lambda i: NS(id=i, name="lra-review", config=NS(
            networking={"type": "limited", "allow_package_managers": True,
                        "allow_mcp_servers": False, "allowed_hosts": []}))),
    )))
    monkeypatch.setattr(lra.config, "anthropic_client", lambda: client)
    for name in ("MANAGED_PLAYBOOK_AGENT_ID", "MANAGED_CLOSING_AGENT_ID",
                 "MANAGED_COMMENTS_AGENT_ID", "MANAGED_BLACKLINE_AGENT_ID"):
        monkeypatch.setenv(name, name.lower())
    lra.config.settings.cache_clear()
    try:
        monkeypatch.setattr(sys, "argv", ["lra", "agents", "apply"])
        assert cli.main() == 0
    finally:
        lra.config.settings.cache_clear()
    return dict(updated), capsys.readouterr().out


def test_agents_apply_turns_the_old_streaming_reviewer_into_the_clientless_one(
        monkeypatch, capsys):
    """Phase 5 on a deployment with only MANAGED_REVIEW_AGENT_ID: the agent
    there is updated in place to review.agent.yaml, which has no custom
    tools, so the id every session already uses stops offering them."""
    updated, out = _apply(monkeypatch, capsys)
    body = updated["agent_review"]
    assert body["name"] == "LRA reviewer"
    assert not [t for t in body["tools"] if t.get("type") == "custom"]
    assert "MANAGED_REVIEW_AGENT_ID=" not in out


def test_agents_apply_adopts_the_detached_reviewer_when_its_id_is_still_set(
        monkeypatch, capsys):
    """A deployment that ran phase 1 has the clientless reviewer at its own
    id. That agent becomes the reviewer; the streaming one is left alone and
    named for archiving, and the old setting is named for deletion."""
    updated, out = _apply(monkeypatch, capsys,
                          MANAGED_REVIEW_DETACHED_AGENT_ID="agent_review_detached")
    assert "agent_review" not in updated
    assert updated["agent_review_detached"]["name"] == "LRA reviewer"
    assert "MANAGED_REVIEW_AGENT_ID=agent_review_detached" in out
    assert "delete that line" in out and "agent_review is no longer used" in out
