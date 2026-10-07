"""Nothing kept after 7 days; nothing learned unless asked (retention.py).

Jim's decision of 2026-10-04, held to the code: the defaults say 7 days and
no learning, the daily sweep deletes every store that holds something about
a document once its window has passed, a reply keeps a conversation alive,
and the sweep reaches Anthropic's sessions and uploads as well as our rows.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lra import audit, blackline, closing, memory, retention, store, tenant, thread
from lra.config import Settings
from lra.memory import Kind, MemoryEntry, Scope, Status
from tests import test_cloudflare as tc
from tests.test_cloudflare import docx_bytes

cloud = tc.cloud  # the fixture: D1 and R2, played by the fake Worker

ROOT = Path(__file__).resolve().parents[1]
JIM = "jim@firm.com"


def _ago(days: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def _age_thread(key: str, days: float) -> None:
    with store.connect() as c:
        c.execute("UPDATE threads SET updated_at = ?, active_at = NULL WHERE id = ?",
                  (_ago(days), key))


# --- the defaults ----------------------------------------------------------------


def test_the_defaults_keep_seven_days_and_learn_nothing(monkeypatch):
    for name in ("THREAD_RETENTION_DAYS", "ARCHIVE_RETENTION_DAYS", "ARCHIVE_ENABLED",
                 "LEARN_FROM_OUTCOMES"):
        monkeypatch.delenv(name, raising=False)
    cfg = Settings(_env_file=None)
    assert cfg.thread_retention_days == 7
    assert cfg.archive_retention_days == 7
    assert cfg.archive_enabled is False
    assert cfg.learn_from_outcomes is False


def test_production_and_every_new_firm_say_the_same():
    jim = tenant.parse_jsonc((ROOT / "cloudflare" / "wrangler.jsonc").read_text())["vars"]
    assert jim["THREAD_RETENTION_DAYS"] == "7"
    assert jim["LEARN_FROM_OUTCOMES"] == "false"
    assert "THREAD_RETENTION_DAYS" in tenant.KNOBS
    assert tenant.FIRM_DEFAULTS["LEARN_FROM_OUTCOMES"] == "false"


def test_the_daily_cron_runs_the_sweep_in_the_container():
    source = (ROOT / "cloudflare" / "src" / "index.ts").read_text()
    scheduled = source[source.index("async scheduled("):source.index("satisfies ExportedHandler")]
    assert "retention(env)" in scheduled
    assert '"http://container/retention/sweep"' in source
    config = tenant.parse_jsonc((ROOT / "cloudflare" / "wrangler.jsonc").read_text())
    assert config["triggers"]["crons"]


# --- conversations ------------------------------------------------------------


def test_a_conversation_quiet_for_the_window_goes_with_its_document_and_versions(cloud):
    thread.start("old", JIM, "Old.docx", docx_bytes("v1"), None)
    thread.supersede("old", "Old v2.docx", docx_bytes("v2"))
    thread.start("new", JIM, "New.docx", docx_bytes(), None)
    _age_thread("old", 8)
    with store.connect() as c:
        c.execute("UPDATE blackline_versions SET created_at = ?", (_ago(8),))
    assert len(cloud.blobs) == 3

    done = retention.sweep()

    assert done["conversations"] == 1
    assert thread.load("old") is None and thread.load("new") is not None
    assert blackline.stored("old") == []
    assert len(cloud.blobs) == 1


def test_a_reply_keeps_a_conversation_for_seven_days_from_the_reply(cloud):
    thread.start("t", JIM, "NDA.docx", docx_bytes(), None)
    thread.add_alias("t", "<review-reply@legal.firm.com>")
    _age_thread("t", 6)
    # The document is six days old; the lawyer replies "undo 2" today.
    assert thread.resolve(JIM, None, "<m2@firm.com>", "<review-reply@legal.firm.com>") == "t"
    with store.connect() as c:
        c.execute("UPDATE threads SET updated_at = ? WHERE id = 't'", (_ago(9),))

    retention.sweep()
    assert thread.load("t") is not None

    with store.connect() as c:
        c.execute("UPDATE threads SET active_at = ? WHERE id = 't'", (_ago(7.1),))
    retention.sweep()
    assert thread.load("t") is None


def test_the_day_a_reply_names_is_still_the_day_of_the_document(cloud):
    thread.start("t", JIM, "NDA.docx", docx_bytes(), None)
    thread.add_alias("t", "<r@legal.firm.com>")
    with store.connect() as c:
        c.execute("UPDATE threads SET updated_at = ? WHERE id = 't'", ("2026-09-01T10:00:00+00:00",))
    thread.resolve(JIM, None, "<m2@firm.com>", "<r@legal.firm.com>")
    assert thread.load("t").seen_on == "1 Sep"


def test_zero_keeps_everything(cloud, monkeypatch):
    from lra.config import settings

    monkeypatch.setenv("THREAD_RETENTION_DAYS", "0")
    settings.cache_clear()
    thread.start("old", JIM, "Old.docx", docx_bytes(), None)
    _age_thread("old", 400)
    retention.sweep()
    assert thread.load("old") is not None


# --- closings -----------------------------------------------------------------


def test_a_quiet_closing_goes_with_its_signed_pages_and_a_busy_one_stays(cloud):
    closing.create("quiet", JIM, "Quiet closing")
    closing.keep_file("quiet", "Signed page.pdf", "signed", b"%PDF signed")
    closing.create("busy", JIM, "Busy closing")
    closing.keep_file("busy", "Signed page.pdf", "signed", b"%PDF signed too")
    with store.connect() as c:
        c.execute("UPDATE closings SET updated_at = ?", (_ago(10),))
    # A status question on the busy one yesterday.
    closing.touch("busy")

    assert retention.sweep()["closings"] == 1
    assert closing.load("quiet") is None and closing.held("quiet") == []
    assert closing.load("busy") is not None and closing.held("busy")
    assert len(cloud.blobs) == 1


# --- memory -------------------------------------------------------------------


@dataclass
class Undone:
    category: str
    title: str = "A change"


def test_nothing_is_learned_from_undos_or_dismissals_unless_asked(cloud):
    memory.init()
    for _ in range(6):
        memory.record_rejections("job", JIM, None, [Undone("date-format")])
        memory.record_dismissals("job", JIM, None, [Undone("date-format")])
    memory.record_outcome("job", JIM, None, "typo", "", "Typo", "", None, "accepted")
    with store.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM suggestion_outcomes").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM memory").fetchone()[0] == 0


def test_what_the_lawyer_asks_for_is_still_remembered(cloud):
    asked = memory.suppression_request("stop flagging the Oxford comma")
    assert memory.remember_suppression(JIM, asked)
    assert "Oxford comma" in memory.as_prompt_block(memory.recall(JIM))


def test_an_unconfirmed_note_goes_and_a_confirmed_one_stays(cloud):
    for statement, status in (("Uses 'will', not 'shall'.", Status.PENDING),
                              ("Dates as 1 January 2026.", Status.CONFIRMED)):
        memory.remember(MemoryEntry(scope=Scope.PERSONAL, scope_key=JIM, kind=Kind.PREFERENCE,
                                    statement=statement, confidence=0.9, status=status))
    with store.connect() as c:
        c.execute("UPDATE memory SET created_at = ?", (_ago(8),))

    retention.sweep()
    assert memory.pending_notes(JIM) == []
    assert [e.statement for e in memory.recall(JIM)] == ["Dates as 1 January 2026."]


def test_outcomes_recorded_while_learning_was_on_go_once_it_is_off(cloud, monkeypatch):
    from lra.config import settings

    monkeypatch.setenv("LEARN_FROM_OUTCOMES", "true")
    settings.cache_clear()
    memory.record_outcome("job", JIM, None, "typo", "", "Typo", "", None, "rejected")
    with store.connect() as c:
        c.execute("UPDATE suggestion_outcomes SET observed_at = ?", (_ago(30),))
    retention.sweep()
    with store.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM suggestion_outcomes").fetchone()[0] == 1

    monkeypatch.setenv("LEARN_FROM_OUTCOMES", "false")
    settings.cache_clear()
    retention.sweep()
    with store.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM suggestion_outcomes").fetchone()[0] == 0


# --- Anthropic ----------------------------------------------------------------


@pytest.fixture
def api(cloud, monkeypatch):
    import lra.config
    from lra.config import settings
    from tests.test_purge import FakeAnthropic

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    settings.cache_clear()
    fake = FakeAnthropic()
    monkeypatch.setattr(lra.config, "anthropic_client", lambda: fake.client)
    return fake


def _job(api, job_id: str, days: float, sessions=(), uploads=()) -> None:
    store.claim(job_id, f"m-{job_id}", JIM)
    token = audit.begin(job_id)
    try:
        for sid in sessions:
            audit.session(sid)
            api.sessions[sid] = "idle"
            api.outputs[sid] = [f"{sid}_out"]
            api.files.add(f"{sid}_out")
        audit.uploaded(list(uploads))
        api.files.update(uploads)
    finally:
        audit.end(token)
    with store.connect() as c:
        c.execute("UPDATE audit_log SET received_at = ? WHERE job_id = ?", (_ago(days), job_id))


def test_a_jobs_sessions_and_uploads_go_from_anthropic_after_the_window(api):
    _job(api, "old", 8, sessions=["sesn_old"], uploads=["file_old"])
    _job(api, "new", 1, sessions=["sesn_new"], uploads=["file_new"])

    assert retention.sweep()["Anthropic jobs"] == 1
    assert api.deleted_sessions == ["sesn_old"]
    assert sorted(api.deleted_files) == ["file_old", "sesn_old_out"]
    row = audit.rows("2000-01-01")
    by_job = {r["job_id"]: r for r in row}
    # The record of what ran stays; it says when its copies went.
    assert by_job["old"]["session_ids"] == ["sesn_old"]
    with store.connect() as c:
        expired = dict(c.execute("SELECT job_id, expired FROM audit_log").fetchall())
    assert expired["old"] and not expired["new"]

    calls = api.calls()
    retention.sweep()
    assert api.calls() == calls


def test_a_failure_at_anthropic_is_tried_again_tomorrow(api):
    _job(api, "old", 8, sessions=["sesn_old"])
    api.fail_once.add("sesn_old")
    assert retention.sweep()["Anthropic jobs"] == 0
    assert retention.sweep()["Anthropic jobs"] == 1
    assert api.deleted_sessions == ["sesn_old"]


def test_one_failing_step_does_not_stop_the_others(cloud, monkeypatch):
    def broken() -> int:
        raise RuntimeError("D1 said no")

    monkeypatch.setattr(store, "purge", broken)
    thread.start("old", JIM, "Old.docx", docx_bytes(), None)
    _age_thread("old", 8)
    done = retention.sweep()
    assert done["job rows"] == -1 and done["conversations"] == 1


# --- the route the cron calls -------------------------------------------------


def test_only_the_worker_can_run_the_sweep(cloud):
    from lra import main

    client = TestClient(main.app, raise_server_exceptions=False)
    assert client.post("/retention/sweep").status_code == 401
    assert client.post("/retention/sweep",
                       headers={"authorization": "Bearer wrong"}).status_code == 401
    thread.start("old", JIM, "Old.docx", docx_bytes(), None)
    _age_thread("old", 8)
    answer = client.post("/retention/sweep", headers={"authorization": "Bearer s3cret"})
    assert answer.status_code == 200
    assert json.loads(answer.text)["deleted"]["conversations"] == 1
