"""The container's half of the review Workflow: one route per step.

cloudflare/src/review-flow.ts calls these in order, each one retried on its
own, with the review's state sealed in R2 between them. The tests drive them
the way the Workflow does, against the fake Worker, and hold the result to
the one handler.handle() produces in one process for the same message.
"""

from __future__ import annotations

import json
import re
from email.message import EmailMessage
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from secondeye import crypto, d1, edge, handler, jobstate, memory, sessions_api, store, thread
from secondeye.models import Mode, ReviewResult
from tests.fake_edge import FakeEdge
from tests.fake_sessions import configure
from tests.fake_sessions_api import FakeSessionsApi
from tests.test_cloudflare import cloud  # noqa: F401  (the fixture)
from tests.test_end_to_end import JUDGMENT, TYPO, contract, stub_agent

AUTH = {"authorization": "Bearer s3cret"}
JOB = "rf-0123456789abcdef"


def raw_review(message_id="<wf-1@firm.com>", body="Quick look before this goes to the client?"):
    msg = EmailMessage()
    msg["From"] = "Jim Baker <jim@firm.com>"
    msg["To"] = "review@legal.firm.com"
    msg["Subject"] = "Acme / Beta NDA"
    msg["Message-ID"] = message_id
    msg.set_content(body)
    msg.add_attachment(contract(), maintype="application",
                       subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
                       filename="Acme NDA.docx")
    return msg.as_bytes()


def result(*findings) -> ReviewResult:
    return ReviewResult(mode=Mode.REDLINE, summary="A short NDA with one drafting error "
                        "and one loose obligation.", findings=list(findings))


@pytest.fixture
def worker(cloud):  # noqa: F811
    return cloud


@pytest.fixture
def flow(cloud, monkeypatch):  # noqa: F811
    configure(monkeypatch)
    # Configured, so a notice is owed; but nothing here reaches the API.
    monkeypatch.setattr(memory, "stores_for", lambda *a, **k: {})
    fake = FakeSessionsApi(result(TYPO, JUDGMENT), polls=1)
    previous = sessions_api.use(fake)
    client = TestClient(main_app(), raise_server_exceptions=False)
    yield client, fake, cloud
    sessions_api.use(previous)


def main_app():
    from secondeye import main

    return main.app


def step(client, path, job=JOB, **body):
    headers = {**AUTH, "x-job-id": job}
    if path == "/prepare":
        return client.post(path, content=body["raw"], headers=headers)
    return client.post(path, content=json.dumps(body), headers=headers)


def stored(worker, key) -> dict:
    """What the Worker reads for its send step: sealed JSON in R2."""
    return json.loads(crypto.unseal(worker.blobs[key]))


def test_steps_refuse_anyone_but_the_worker_and_need_a_job_id(flow):
    client, _, _ = flow
    assert client.post("/session/start", headers={"x-job-id": JOB}).status_code == 401
    assert client.post("/session/start", headers=AUTH).status_code == 400


def test_a_review_runs_as_steps_and_leaves_its_reply_for_the_worker(flow):
    client, fake, worker = flow
    prepared = step(client, "/prepare", raw=crypto.seal(raw_review())).json()
    assert prepared["status"] == "review"
    assert prepared["filename"] == "Acme NDA.docx"
    # The notice, worded for when it is due: 300 + 90 - 90 seconds is 5 minutes.
    assert prepared["notice"]["text"] == ("Reviewing Acme NDA.docx; back in about 5 minutes.\n" + "\nPrivileged & Confidential — Attorney Work Product\n")
    assert prepared["notice"]["headers"]["In-Reply-To"] == "<wf-1@firm.com>"
    assert prepared["noticeAfterSeconds"] == handler.SLOW_NOTICE_AFTER
    assert worker.sent == []                      # nothing sent by the container

    started = step(client, "/session/start").json()
    session = started["sessionId"]
    # A retried start does not start a second session.
    assert step(client, "/session/start").json()["sessionId"] == session
    assert len(fake.started) == 1
    assert fake.started[0].mode is Mode.REDLINE and fake.started[0].original

    assert step(client, "/session/status", sessionId=session).json() == {"state": "running"}
    assert step(client, "/session/status", sessionId=session).json() == {"state": "done"}

    keys = step(client, "/finish", noticeMessageId="cf-notice").json()
    reply = stored(worker, keys["outbound"])
    assert reply["to"] == ["jim@firm.com"]
    assert reply["headers"]["In-Reply-To"] == "<wf-1@firm.com>"
    assert reply["text"].startswith("Don't send yet")
    assert any(a["filename"] == "Acme NDA (redline).docx" for a in reply["attachments"])
    fallback = stored(worker, keys["fallback"])
    assert fallback["attachments"] == []
    assert fallback["text"].startswith("I reviewed this but could not send the marked-up copy")

    # A retried finish hands back the same reply rather than building another.
    assert step(client, "/finish", noticeMessageId="cf-notice").json() == keys
    assert len(fake.collected) == 1

    assert step(client, "/finished", kind="review", messageId="cf-reply").json() == {
        "status": "ok"}
    with store.connect() as c:
        status = c.execute("SELECT status FROM jobs WHERE id = ?", (JOB,)).fetchone()[0]
    assert status == "replied"
    # A reply to the verdict, or to the notice, finds the conversation.
    for sent in ("cf-reply", "cf-notice"):
        assert thread.resolve("jim@firm.com", None, "<x@firm.com>", sent, []) is not None
    # And nothing of the review is left in object storage but the conversation.
    assert not [k for k in worker.blobs if k.startswith("job/")]
    assert step(client, "/finished", kind="review", messageId="cf-reply").json() == {
        "status": "gone"}


def test_the_workflow_reply_is_the_one_the_in_process_path_sends(flow, monkeypatch):
    client, _, worker = flow
    step(client, "/prepare", raw=crypto.seal(raw_review()))
    session = step(client, "/session/start").json()["sessionId"]
    step(client, "/session/status", sessionId=session)
    keys = step(client, "/finish").json()
    via_workflow = stored(worker, keys["outbound"])

    # The same message on a fresh deployment, in one process (second-eye replay's
    # path, and a mail vendor's webhook).
    fresh = FakeEdge(worker.path + ".in-process")
    monkeypatch.setattr(edge, "call", fresh.call)
    d1._applied_scripts.clear()
    thread._migrated.clear()
    monkeypatch.setattr(handler.review, "review", stub_agent(TYPO, JUDGMENT))
    monkeypatch.setattr(handler, "SLOW_NOTICE_AFTER", 60.0)
    from secondeye.mail import get_provider

    handler.handle(get_provider().parse_raw(raw_review()))
    [in_process] = fresh.sent
    # The one-tap address carries a random token, the one thing that differs.
    def same(body):
        return re.sub(r"review\+t[0-9a-f]+@", "review+TOKEN@", body)

    assert same(via_workflow["text"]) == same(in_process["text"])
    assert same(via_workflow["html"]) == same(in_process["html"])
    assert ([a["filename"] for a in via_workflow["attachments"]]
            == [a["filename"] for a in in_process["attachments"]])


def test_a_retried_prepare_is_answered_from_what_the_first_left(flow):
    client, _, _ = flow
    first = step(client, "/prepare", raw=crypto.seal(raw_review())).json()
    assert step(client, "/prepare", raw=crypto.seal(raw_review())).json() == first
    # Another job for the same message is a duplicate, and stops.
    assert step(client, "/prepare", job="rf-other",
                raw=crypto.seal(raw_review())).json() == {"status": "duplicate"}


def test_a_message_that_is_not_a_review_is_answered_in_prepare_once(flow):
    client, _, worker = flow
    raw = crypto.seal(raw_review(body="Could you send me a clean copy of this?"))
    assert step(client, "/prepare", raw=raw).json() == {"status": "done"}
    [reply] = worker.sent
    assert reply["attachments"][0]["filename"] == "Acme NDA (clean).docx"
    assert list(worker.outbox) == [f"{JOB}:prepare-1"]
    # The step is retried (its answer was lost): the reply is not sent again.
    with store.connect() as c:
        c.execute("UPDATE jobs SET status = 'received'")
    step(client, "/prepare", raw=raw)
    assert len(worker.sent) == 1


def test_a_failed_review_sends_what_the_checks_found(flow):
    client, fake, worker = flow
    fake.outcomes = [sessions_api.ReviewFailed("the agent finished without reporting")]
    step(client, "/prepare", raw=crypto.seal(raw_review()))
    step(client, "/session/start")
    finish = step(client, "/finish")
    assert finish.status_code == 422             # not worth retrying

    failed = step(client, "/fail", error="finish: the review failed").json()
    reply = stored(worker, failed["outbound"])
    assert "only the mechanical checks" in reply["text"]
    assert step(client, "/fail").json() == failed    # idempotent
    step(client, "/finished", kind="failure", messageId="cf-failure")
    # The conversation is kept, so a reply to the apology finds the document.
    assert thread.resolve("jim@firm.com", None, "<x@firm.com>", "cf-failure", []) is not None


def test_a_failure_after_the_review_carries_its_findings(flow):
    client, _, worker = flow
    step(client, "/prepare", raw=crypto.seal(raw_review()))
    step(client, "/session/start")
    step(client, "/finish")
    # The send step then failed for good.
    reply = stored(worker, step(client, "/fail", error="send").json()["outbound"])
    assert reply["text"].startswith("I reviewed this but had trouble sending the reply")
    assert reply["attachments"] == []


def test_a_failure_before_anything_was_prepared_is_left_to_the_worker(flow):
    client, _, _ = flow
    assert step(client, "/fail", error="prepare").json() == {"outbound": None}
    assert step(client, "/finish").status_code == 404


def test_a_message_no_key_opens_fails_the_step_so_it_is_retried(flow, monkeypatch):
    client, _, worker = flow
    from secondeye.config import settings
    from tests.test_cloudflare import OTHER_KEY

    sealed = crypto.seal(raw_review())
    monkeypatch.setenv("DATA_KEY", OTHER_KEY)
    settings.cache_clear()
    assert step(client, "/prepare", raw=sealed).status_code == 500
    assert worker.sent == []


def test_the_prepared_review_survives_being_stored(flow):
    """What one step leaves, the next reads in another process. Every field
    the later stages use comes back as it went in."""
    from secondeye import flow as steps

    client, _, _ = flow
    step(client, "/prepare", raw=crypto.seal(raw_review()))
    saved = jobstate.load(JOB)
    job = steps.restore(saved["review"])
    again = steps.dump(job)
    assert jobstate.dumps(again) == jobstate.dumps(saved["review"])
    assert job.working_is_original and job.working is job.att.content
    assert job.doc.docx is not None
    assert [b.text for b in job.doc.blocks][:2] == [
        "Mutual Non-Disclosure Agreement",
        ("This Agreement is entered into as of March 3, 2026 by and between "
         'Acme Holdings LLC ("Discloser") and Beta Industries Inc ("Recipient").')]
    assert job.mechanical and all(f.title for f in job.mechanical)


def test_state_is_sealed_where_the_worker_can_find_it(flow):
    client, _, worker = flow
    step(client, "/prepare", raw=crypto.seal(raw_review()))
    where = jobstate.key(JOB)
    assert where.startswith("job/") and len(where) == 36
    assert worker.blobs[where].startswith(b"enc:v1:")
    assert b"Reciever" not in worker.blobs[where]


def test_the_runner_is_anthropics_unless_one_is_given():
    previous = sessions_api.use(None)
    try:
        assert isinstance(sessions_api.backend(), sessions_api.DetachedSessions)
        assert not hasattr(sessions_api, "InProcessSessions")
        fake = FakeSessionsApi()
        sessions_api.use(fake)
        assert sessions_api.backend() is fake
    finally:
        sessions_api.use(previous)


def test_a_detached_session_runs_through_the_steps_with_nobody_attached(worker, monkeypatch):
    """Phase 1's clientless session behind the same three calls: started,
    polled by id as the Workflow does, then read from the files it left."""
    from secondeye import managed
    from tests import fake_sessions as fs

    configure(monkeypatch)
    monkeypatch.setattr(memory, "stores_for", lambda *a, **k: {})
    monkeypatch.setattr(managed.time, "sleep", lambda s: None)
    findings = {"summary": "One drafting error.", "findings": [TYPO.model_dump(mode="json")]}
    platform = fs.DetachedSessions([fs.Turn(fs.idle("end_turn"), polls=1, outputs={
        "findings.json": json.dumps(findings).encode()})])
    monkeypatch.setattr(managed, "anthropic_client", lambda: platform.client)
    previous = sessions_api.use(None)
    client = TestClient(main_app(), raise_server_exceptions=False)
    try:
        assert step(client, "/prepare", raw=crypto.seal(raw_review())).json()["status"] == "review"
        session = step(client, "/session/start").json()["sessionId"]
        assert session == "sesn_fake"                    # Anthropic's id: the webhook's key
        assert platform.created[0]["agent"] == "agent_review"
        assert step(client, "/session/status", sessionId=session).json() == {"state": "running"}
        assert step(client, "/session/status", sessionId=session).json() == {"state": "done"}
        keys = step(client, "/finish").json()
        reply = stored(worker, keys["outbound"])
        assert reply["text"].startswith("Don't send yet")
        assert any(a["filename"] == "Acme NDA (redline).docx" for a in reply["attachments"])
        assert platform.deleted_sessions == ["sesn_fake"]        # nothing left behind
    finally:
        sessions_api.use(previous)


def test_a_detached_review_that_failed_is_not_retried(worker, monkeypatch):
    from secondeye import managed
    from tests import fake_sessions as fs

    configure(monkeypatch)
    monkeypatch.setattr(memory, "stores_for", lambda *a, **k: {})
    monkeypatch.setattr(managed.time, "sleep", lambda s: None)
    platform = fs.DetachedSessions([fs.Turn(fs.idle("retries_exhausted"), polls=0)])
    monkeypatch.setattr(managed, "anthropic_client", lambda: platform.client)
    previous = sessions_api.use(None)
    client = TestClient(main_app(), raise_server_exceptions=False)
    try:
        step(client, "/prepare", raw=crypto.seal(raw_review()))
        step(client, "/session/start")
        assert step(client, "/finish").status_code == 422
        reply = stored(worker, step(client, "/fail", error="finish").json()["outbound"])
        assert "only the mechanical checks" in reply["text"]
    finally:
        sessions_api.use(previous)


def test_the_workflow_calls_only_routes_the_container_has():
    """A static contract: the paths review-flow.ts posts to are the routes
    main.py serves, and the config wires the Workflow in as the only path."""
    root = Path(__file__).resolve().parents[1]
    flow_ts = (root / "cloudflare" / "src" / "review-flow.ts").read_text()
    called = set(re.findall(r'container\(env, "(/[a-z/]+)"', flow_ts))
    assert called == {"/prepare", "/session/start", "/session/status", "/finish", "/finished",
                      "/fail"}
    served = {getattr(r, "path", "") for r in main_app().routes}
    assert called <= served

    raw = (root / "cloudflare" / "wrangler.jsonc").read_text()
    config = json.loads(re.sub(r"^\s*//.*$", "", raw, flags=re.MULTILINE))
    assert config["workflows"] == [
        {"binding": "REVIEW_FLOW", "name": "legal-review-flow", "class_name": "ReviewFlow"}]
    assert config["triggers"]["crons"]
    # No other path: no queue, and no flag choosing between the two.
    assert "queues" not in config
    assert "ORCHESTRATOR" not in config["vars"] and "WORKFLOW_SENDERS" not in config["vars"]
    assert "/jobs" not in served
    assert "ANTHROPIC_WEBHOOK_SIGNING_KEY" in raw
    index = (root / "cloudflare" / "src" / "index.ts").read_text()
    assert 'export { ReviewFlow } from "./review-flow";' in index
    # The outbox names the container writes are ones the Worker accepts.
    pattern = "[A-Za-z0-9_-]{1,100}:[a-z0-9-]{1,40}"
    assert f"^{pattern}$" in index
    assert re.fullmatch(pattern, f"rf-{'a' * 40}:prepare-1")


def reply_raw(in_reply_to: str, references: str, body: str = "30",
              message_id: str = "<wf-reply@firm.com>") -> bytes:
    """The lawyer answering one of our emails, with nothing attached."""
    msg = EmailMessage()
    msg["From"] = "Jim Baker <jim@firm.com>"
    msg["To"] = "review@legal.firm.com"
    msg["Subject"] = "Re: Acme / Beta NDA"
    msg["Message-ID"] = message_id
    msg["In-Reply-To"] = in_reply_to
    msg["References"] = references
    msg.set_content(body)
    return msg.as_bytes()


def answered(client, worker, raw, job="rf-the-reply") -> str:
    """The text of what the Workflow sent back for a reply."""
    before = len(worker.sent)
    assert step(client, "/prepare", job=job, raw=crypto.seal(raw)).json() == {"status": "done"}
    [out] = worker.sent[before:]
    return out["text"]


def test_a_reply_to_the_mechanical_only_review_is_answered_as_one(flow):
    """Canary 3, 2026-09-29: the review degraded to the checks (no model
    credit), the reply went out, and "30" in answer to its question was told
    "I could not find a document to review". The id Email Service hands back
    need not be spelt as the header the lawyer's client quotes, and the reply
    need not name the lawyer's first message."""
    client, fake, worker = flow
    fake.outcomes = [sessions_api.ReviewFailed("Managed Agents is not set up")]
    step(client, "/prepare", raw=crypto.seal(raw_review()))
    step(client, "/session/start")
    assert step(client, "/finish").status_code == 422
    reply = stored(worker, step(client, "/fail", error="finish").json()["outbound"])
    assert "Should this be 30 or 13?" in reply["text"]
    step(client, "/finished", kind="failure", messageId="cf-failure@cloudflare.net")

    text = answered(client, worker, reply_raw("<cf-failure@cloudflare.net>",
                                              "<cf-failure@cloudflare.net>"))
    assert text.startswith("Noted: 30. I have made that change.")


def test_a_degraded_review_keeps_its_conversation_before_the_reply_is_sent(flow):
    """Kept by /fail, not /finished: a Workflow that never gets as far as
    /finished still leaves a conversation that a reply to the notice finds."""
    client, fake, worker = flow
    fake.outcomes = [sessions_api.ReviewFailed("the platform exhausted its retries")]
    step(client, "/prepare", raw=crypto.seal(raw_review()))
    step(client, "/session/start")
    step(client, "/finish")
    step(client, "/fail", error="finish", noticeMessageId="cf-notice")

    text = answered(client, worker, reply_raw("<cf-notice>", "<cf-notice>"))
    assert text.startswith("Noted: 30.")


def test_a_degraded_review_on_an_old_conversation_holds_the_new_document(flow):
    """A document sent as a reply on a conversation already held is its new
    version, with its own questions, whether or not the review then reached
    the model. The degraded path used to keep the old document and record
    nothing when the conversation already existed."""
    client, fake, worker = flow
    fake.outcomes = [result(), sessions_api.ReviewFailed("no credit")]
    first = raw_review(message_id="<wf-0@firm.com>", body="Another NDA, please look.")
    step(client, "/prepare", job="rf-first", raw=crypto.seal(first))
    step(client, "/session/start", job="rf-first")
    step(client, "/finish", job="rf-first")
    step(client, "/finished", job="rf-first", kind="review", messageId="cf-first")
    # Whatever the first review asked has been answered since.
    with store.connect() as c:
        c.execute("DELETE FROM questions")

    again = raw_review().replace(
        b"Message-ID: <wf-1@firm.com>",
        b"Message-ID: <wf-1@firm.com>\nIn-Reply-To: <cf-first>\nReferences: <cf-first>")
    step(client, "/prepare", raw=crypto.seal(again))
    step(client, "/session/start")
    step(client, "/finish")
    step(client, "/fail", error="finish")
    step(client, "/finished", kind="failure", messageId="cf-failure")

    text = answered(client, worker, reply_raw("<cf-failure>", "<cf-failure>"))
    assert text.startswith("Noted: 30.")


def test_a_reply_to_the_notice_or_the_verdict_of_a_review_is_an_answer(flow):
    client, _, worker = flow
    step(client, "/prepare", raw=crypto.seal(raw_review()))
    session = step(client, "/session/start").json()["sessionId"]
    step(client, "/session/status", sessionId=session)
    step(client, "/finish", noticeMessageId="cf-notice")
    step(client, "/finished", kind="review", messageId="cf-reply")

    assert answered(client, worker, reply_raw("<cf-notice>", "<cf-notice>")).startswith(
        "Noted: 30.")
    text = answered(client, worker, reply_raw("<cf-reply>", "<cf-reply>", body="undo 1",
                                              message_id="<wf-reply-2@firm.com>"),
                    job="rf-the-second-reply")
    assert text.startswith("I reversed")



def test_a_session_that_cannot_start_for_want_of_setup_is_permanent(monkeypatch):
    """No Managed Agents ids: retrying /session/start three times changed
    nothing and delayed the mechanical-only reply."""
    from secondeye import flow, managed, sessions_api

    class Unset:
        def start(self, job):
            raise managed.NotConfigured("Managed Agents is not set up")

    monkeypatch.setattr(sessions_api, "backend", lambda: Unset())
    monkeypatch.setattr(flow, "_load", lambda job_id: {"review": {}})
    monkeypatch.setattr(flow, "restore", lambda saved: object())
    monkeypatch.setattr(flow.handler, "review_job", lambda job: job)
    import pytest

    with pytest.raises(flow.Permanent):
        flow.session_start("j1")
