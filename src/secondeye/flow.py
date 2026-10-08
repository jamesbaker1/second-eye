"""The container's half of the review Workflow (cloudflare/src/review-flow.ts).

On Cloudflare every message is carried by a Workflow, which runs the stages
handler.handle() runs in one process (the local path: `second-eye replay`, a mail
vendor's webhook) as separate steps, each one a call to this container, each
retried on its own, with the review's state sealed in object storage between
them (jobstate.py):

    prepare        parse, claim, route; every reply that is not a review is
                   sent here, as it always was. For a review: extract, the
                   Word copy, since-last-time, the archive, the checks.
    session_start  start the review session (sessions_api), once.
    session_status whether it has finished.
    finish         collect the result; write the redline; record the
                   conversation; build the reply and its no-attachment
                   variant and store both for the Worker to send.
    finished       after the Worker sent it: the announcement counted as
                   said, the reply's id aliased, the job marked replied.
    fail           the one email a failed review still owes, stored for the
                   Worker to send.

Every step may run twice: the Workflow retries a step whose call failed, and a
call can fail after the work was done. So each one first looks for what an
earlier attempt left behind, and the sends that happen in here carry an
outbox name (`<job>:prepare-<n>`) that the Worker sends at most once.

The job id is the Workflow instance id, which is derived from the message, so
it is also the `jobs.id` claimed for it.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from io import BytesIO

from secondeye import audit, crypto, handler, jobstate, policy, sessions_api, store, thread
from secondeye import notice as slow
from secondeye.config import settings
from secondeye.mail import get_provider
from secondeye.mail.cloudflare import MessageTooLarge, payload
from secondeye.models import Attachment, Finding, InboundEmail, Mode, ReviewResult
from secondeye.pipeline import deadlines, extract, identity, their_paper

log = logging.getLogger(__name__)

# How long past the session's own limits the Workflow keeps waiting before it
# collects whatever there is. The session stops itself first; this is for one
# that does not.
DEADLINE_MARGIN_SECONDS = 120


class Gone(LookupError):
    """No state for this job: never prepared, or already finished."""


class Permanent(RuntimeError):
    """Retrying this step will not help (a failed review, a reply too big to
    send). The Worker stops retrying and goes to the failure path."""


# -- the steps ---------------------------------------------------------------


def prepare(job_id: str, sealed: bytes) -> dict:
    saved = jobstate.load(job_id)
    if saved and saved.get("prepared"):
        return saved["prepared"]

    # A message no key opens raises crypto.KeyMissing: a configuration fault,
    # not a bad message, so the step fails and is retried.
    raw = crypto.unseal(sealed)
    provider = get_provider()
    try:
        email = provider.parse_raw(raw)
    except Exception:
        log.exception("could not parse a message for job %s", job_id)
        return {"status": "unparseable"}

    if not store.claim_for(job_id, email.message_id, email.from_address):
        log.info("message %s is claimed by another job; job %s stops", email.message_id, job_id)
        return {"status": "duplicate"}

    audit.received(job_id, email)
    # Audited inside the policy, so the row holds who a reply actually went to.
    sender = Outboxed(policy.Policed(audit.Watch(provider, job_id), email), f"{job_id}:prepare")
    token = audit.begin(job_id)
    try:
        with policy.ai_scope():
            job = handler._route(job_id, email, sender)
            if job is None or not handler._prepare(job, sender):
                return {"status": "done"}
            if policy.ai_forbidden():
                # A no-AI client (policy.py): the reply is the checks, sent from
                # here, and there is no session to start.
                handler.mechanical_only(job, sender)
                return {"status": "done"}
    finally:
        audit.end(token)

    cfg = settings()
    deadline = cfg.agent_time_budget_seconds + cfg.agent_report_grace_seconds
    response = {
        "status": "review",
        "filename": job.att.filename,
        "deadlineSeconds": deadline + DEADLINE_MARGIN_SECONDS,
        "noticeAfterSeconds": handler.notice_delay(job.entry),
        "notice": _notice(job, deadline),
    }
    jobstate.save(job_id, {"review": dump(job), "prepared": response})
    return response


def session_start(job_id: str) -> dict:
    saved = _load(job_id)
    if not saved.get("session_id"):
        job = restore(saved["review"])
        from secondeye import managed

        # The session and its uploads are recorded against this job as they
        # happen (audit.session, audit.uploaded), which a purge relies on.
        token = audit.begin(job_id)
        try:
            saved["session_id"] = sessions_api.backend().start(handler.review_job(job))
        except managed.NotConfigured as e:
            # No retry changes this. Straight to the mechanical-only reply;
            # it used to answer 500 and burn three retries first.
            raise Permanent(f"the review failed: {e}") from e
        finally:
            audit.end(token)
        jobstate.save(job_id, saved)
        audit.session(saved["session_id"], job_id)
    return {"sessionId": saved["session_id"]}


def session_status(job_id: str, session_id: str) -> dict:
    return {"state": sessions_api.backend().status(session_id)}


def finish(job_id: str, notice_message_id: str | None = None) -> dict:
    saved = _load(job_id)
    if saved.get("finished"):
        return saved["finished"]
    job = restore(saved["review"])
    if job.result is None:
        try:
            result = sessions_api.backend().collect(saved.get("session_id") or "",
                                                    handler.review_job(job, stores=False))
        except sessions_api.ReviewFailed as e:
            raise Permanent(f"the review failed: {e}") from e
        handler._merge_review(job, result)
        # Stored before the reply is built, so a retry of this step, or the
        # failure path, has the review without asking the session again.
        saved["review"] = dump(job)
        jobstate.save(job_id, saved)

    out = handler._compose(job, notice_message_id)
    # Addressed now; sent when the Worker says so (`finished`).
    audit.replying(job_id, policy.apply(out, job.email.from_address))
    try:
        full = _payload(out, job.email)
    except MessageTooLarge:
        full = None
    try:
        fallback = _payload(handler.without_attachments(out), job.email)
    except MessageTooLarge as e:
        raise Permanent("the reply is too big to send even without its files") from e
    keys = {
        "outbound": jobstate.put(job_id, "outbound", jobstate.dumps(full).encode())
        if full else None,
        "fallback": jobstate.put(job_id, "fallback", jobstate.dumps(fallback).encode()),
    }
    saved["review"] = dump(job)
    saved["finished"] = keys
    jobstate.save(job_id, saved)
    return keys


def finished(job_id: str, kind: str, message_id: str | None) -> dict:
    saved = jobstate.load(job_id)
    if saved is None:
        return {"status": "gone"}
    job = restore(saved["review"])
    audit.sent(job_id)
    if kind == "review":
        handler._after_delivery(job, message_id)
    elif message_id:
        # The conversation was kept when the failure reply was built (`fail`);
        # the lawyer's reply to it points at this id.
        try:
            thread.add_alias(job.thread_key, message_id)
        except Exception:
            log.exception("could not alias the failure reply")
    jobstate.discard(job_id)
    return {"status": "ok"}


def fail(job_id: str, error: str = "", notice_message_id: str | None = None) -> dict:
    """Store the failure reply. {"outbound": None} when there is no review to
    speak for: the Worker then answers from the message's headers alone.

    A review that degraded to its mechanical findings keeps its conversation
    here, before the reply exists, as a finished review does in `finish`: a
    reply to the apology, or to the slow-review notice, then finds the
    document and its questions whether or not `finished` is ever reached."""
    saved = jobstate.load(job_id)
    if saved is None:
        return {"outbound": None}
    if saved.get("failed"):
        return saved["failed"]
    job = restore(saved["review"])
    email = job.email
    store.record(job_id, email.message_id, "failed", email.from_address,
                 {"error": error[:300]})
    out, degraded = handler._failure_reply(job)
    if degraded:
        handler._remember_degraded(job, notice_message_id)
    audit.replying(job_id, policy.apply(out, email.from_address))
    try:
        body = _payload(out, email)
    except MessageTooLarge:
        body = _payload(handler.without_attachments(out), email)
    saved["failed"] = {"outbound": jobstate.put(job_id, "failure",
                                                jobstate.dumps(body).encode())}
    jobstate.save(job_id, saved)
    return saved["failed"]


def _load(job_id: str) -> dict:
    saved = jobstate.load(job_id)
    if saved is None:
        raise Gone(f"no state for job {job_id}")
    return saved


def _notice(job: handler._Review, deadline: float) -> dict | None:
    """The slow-review notice, ready to send, or None when none is owed.
    Worded for the moment it is due, which is when the Workflow sends it."""
    delay = handler.notice_delay(job.entry)
    if delay is None:
        return None
    return _payload(slow.compose(job.email, job.att.filename, deadline - delay), job.email)


def _payload(out, email: InboundEmail) -> dict:
    """A reply stored for the Worker to send, as firm policy lets it leave.
    The only way this module builds one: the Worker sends it as it stands."""
    return payload(policy.apply(out, email.from_address))


class Outboxed:
    """A provider whose every send is named `<prefix>-<n>`, so the Worker
    sends it once however often this step is retried. The count restarts with
    each attempt, and the code between two sends is the same each time, so
    the same send gets the same name."""

    def __init__(self, provider, prefix: str) -> None:
        self._provider = provider
        self._prefix = prefix
        self._count = 0

    def __getattr__(self, name):
        return getattr(self._provider, name)

    def send(self, out):
        self._count += 1
        if getattr(self._provider, "name", "") != "cloudflare":
            return self._provider.send(out)
        return self._provider.send(out, outbox=f"{self._prefix}-{self._count}")


# -- the review, as data ------------------------------------------------------


def dump(job: handler._Review) -> dict:
    email = job.email
    att_index = next((i for i, a in enumerate(email.attachments) if a is job.att), None)
    if att_index is None:
        att_index = email.attachments.index(job.att)
    doc = job.doc
    return {
        "job_id": job.job_id,
        "email": email.model_dump(mode="python"),
        "att_index": att_index,
        "mode": job.mode,
        "instructions": job.instructions,
        "entry": job.entry,
        "user": job.user,
        "matter": job.matter,
        "thread_key": job.thread_key,
        "doc": None if doc is None else {
            "filename": doc.filename,
            "blocks": [asdict(b) for b in doc.blocks],
            "has_docx": doc.docx is not None,
            "native": list(doc.native) if doc.native else None,
            "transcribed": doc.transcribed,
            "numbering_unresolved": doc.numbering_unresolved,
        },
        "redline_notes": job.redline_notes,
        "explain_mode": job.explain_mode,
        "working": None if job.working_is_original else job.working,
        "working_is_original": job.working_is_original,
        "since": None if job.since is None else {
            "section": list(job.since.section),
            "attachment": (job.since.attachment.model_dump(mode="python")
                           if job.since.attachment else None),
            "block": job.since.block,
        },
        "mechanical": [f.model_dump(mode="python") for f in job.mechanical],
        "dates": [asdict(r) for r in job.dates],
        "paper": asdict(job.paper),
        "result": job.result.model_dump(mode="python") if job.result else None,
        "learned": list(job.learned) if job.learned else None,
        "recipients": job.recipients,
    }


def restore(data: dict) -> handler._Review:
    email = InboundEmail.model_validate(data["email"])
    att = email.attachments[data["att_index"]]
    job = handler._Review(
        data["job_id"], email, att, Mode(data["mode"]), data["instructions"],
        identity.EntryMode(data["entry"]), data["user"], data["matter"], data["thread_key"],
    )
    d = data["doc"]
    if d is not None:
        from docx import Document

        job.doc = extract.ExtractedDoc(
            blocks=[extract.Block(**b) for b in d["blocks"]],
            docx=Document(BytesIO(att.content)) if d["has_docx"] else None,
            filename=d["filename"],
            native=tuple(d["native"]) if d["native"] else None,
            transcribed=d["transcribed"],
            numbering_unresolved=d["numbering_unresolved"],
        )
    job.redline_notes = list(data["redline_notes"])
    job.explain_mode = data["explain_mode"]
    job.working_is_original = data["working_is_original"]
    job.working = att.content if job.working_is_original else data["working"]
    s = data["since"]
    if s is not None:
        heading, line, items = s["section"]
        job.since = handler._Since(
            (heading, line, list(items)),
            Attachment.model_validate(s["attachment"]) if s["attachment"] else None,
            s["block"],
        )
    job.mechanical = [Finding.model_validate(f) for f in data["mechanical"]]
    job.dates = [deadlines.Row(**r) for r in data["dates"]]
    job.paper = their_paper.Provenance(**data["paper"])
    if data["result"] is not None:
        job.result = ReviewResult.model_validate(data["result"])
    job.learned = tuple(data["learned"]) if data["learned"] else None
    job.recipients = list(data.get("recipients") or [])
    return job
