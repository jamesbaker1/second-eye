"""The one function that turns an inbound email into a sent reply."""

from __future__ import annotations

import logging
import re
import traceback
import uuid

from secondeye import (
    archive,
    audit,
    blackline,
    comments,
    consent,
    followup,
    managed,
    memory,
    negotiation,
    oauth,
    playbook,
    policy,
    reconcile,
    sessions_api,
    thread,
    triage,
    versions,
)
from secondeye.config import settings
from secondeye.mail import get_provider
from secondeye.models import (
    Attachment,
    Finding,
    InboundEmail,
    Mode,
    OutboundEmail,
    ReviewResult,
)
from secondeye.pipeline import (
    annotate,
    checks,
    compare,
    deadlines,
    email_checks,
    extract,
    ics,
    identity,
    instruct,
    intake,
    redline,
    reflow,
    reply,
    review,
    router,
    sigpack,
    their_paper,
)
from secondeye.pipeline.filetype import Kind, identify
from secondeye.store import claim, record

log = logging.getLogger(__name__)

DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def handle(email: InboundEmail, provider=None) -> None:
    """Turn one inbound email into one sent reply.

    `provider` is injectable so the replay command can guarantee nothing is
    sent. It used to construct a console provider for parsing only, while this
    function resolved its own from configuration and posted the review to
    whatever live mail API happened to be set.

    The review of a document runs in stages (`_prepare`, the session,
    `_compose`, `_deliver`, `_after_delivery`, `_failure_reply`), because the
    Cloudflare Workflow (flow.py) runs the same stages one container call at a
    time. Here they run back to back in one process, as they always have.
    """
    if policy.paused():
        # Before the claim, so a redelivery after the switch is off is new work.
        log.warning("SERVICE_PAUSED: not handling %s", email.message_id)
        return
    job_id = str(uuid.uuid4())
    # Firm policy (policy.py) sits between every reply below and the mail
    # provider, so nothing composed or decided later can route around it.
    # The audit trail (audit.py) sits inside the policy, so it records who a
    # reply actually went to, and only what actually left.
    provider = policy.Policed(audit.Watch(provider or get_provider(), job_id), email)

    # Atomic. A webhook retry arriving mid-run used to pass a separate check
    # and produce two agent runs and two replies for the same document.
    if not claim(job_id, email.message_id, email.from_address):
        log.info("message %s is already claimed, skipping", email.message_id)
        return

    # The audit trail (audit.py): what arrived, and every session.
    audit.received(job_id, email)
    token = audit.begin(job_id)
    try:
        # A no-AI client found anywhere below holds until this message is done.
        with policy.ai_scope():
            job = _route(job_id, email, provider)
            if job is not None:
                _review_in_process(job, provider)
    finally:
        audit.end(token)


class _Review:
    """One document review, from the moment intake chose the document.

    Everything a later stage needs is an attribute here, and every attribute
    is plain data, so flow.py can seal it into object storage between two
    container calls and a different process can carry on from it.
    """

    def __init__(self, job_id: str, email: InboundEmail, att: Attachment, mode: Mode,
                 instructions: str, entry: identity.EntryMode, user: str,
                 matter: str | None, thread_key: str) -> None:
        self.job_id = job_id
        self.email = email
        self.att = att
        self.mode = mode
        self.instructions = instructions
        self.entry = entry
        self.user = user
        self.matter = matter
        self.thread_key = thread_key
        # Filled by _prepare.
        self.doc = None
        self.redline_notes: list[str] = []
        self.explain_mode = True
        self.working: bytes | None = None
        # `working is att.content`, kept as a flag because identity does not
        # survive being stored and loaded again.
        self.working_is_original = False
        self.since: _Since | None = None
        self.mechanical: list = []
        self.dates: list = []
        self.paper = their_paper.OURS
        # Filled once the review is back.
        self.result: ReviewResult | None = None
        # The announcement the reply carries, marked as said only once it is.
        self.learned: tuple[int, str] | None = None
        # From a triage plan (TRIAGE=model): who the reply goes to, honoured
        # only under REPLY_POLICY=model, and whose paper the document is,
        # which stands in for their_paper.detect when the plan says.
        self.recipients: list[str] = []
        self.paper_override: their_paper.Provenance | None = None


def _route(job_id: str, email: InboundEmail, provider) -> _Review | None:
    """Everything before a document review: the replies that are not one, and
    intake. Returns the review to run, or None when the message has been
    answered (or deliberately not answered) already."""
    # Never answer automated mail. An auto-responder and this agent will
    # otherwise talk to each other until somebody notices, which is the most
    # embarrassing failure available to an email product.
    automated = router.is_automated(email.from_address, email.subject, email.headers)
    if automated:
        log.info("not replying to %s: %s", email.message_id, automated)
        record(job_id, email.message_id, "ignored", email.from_address,
               {"reason": automated})
        return

    # Authorise BEFORE acting on anything. A From header is forgeable, so an
    # unauthorised sender could previously mail the single word "revoke" and
    # destroy a partner's document-system grant without ever being checked.
    #
    # Only a colleague is told why. Anyone else is dropped without a word: the
    # agent's address is visible whenever it is CC'd, so the outsider writing
    # to it is usually opposing counsel hitting reply-all, and "ask Jim to add
    # you" told them the firm runs its drafts past a machine.
    try:
        intake.check_sender(email)
    except intake.Rejection as e:
        if settings().is_internal(email.from_address):
            provider.send(reply.rejection(email, str(e)))
        record(job_id, email.message_id, "rejected", email.from_address,
               {"reason": str(e)})
        return

    # Route the replies that cannot be misread. This deliberately requires the
    # WHOLE message to be the phrase: matching a prefix meant a forwarded mail
    # beginning "Revoke the licence agreement..." silently cut a lawyer off.
    routed = router.route_deterministic(
        email.body, has_attachment=bool(email.attachments)
    )
    # Nothing is done for a sender whose domain did not authenticate. The
    # allowlist checks the From header, which is exactly the field a spoofer
    # controls. This used to cover only replies and revoke/connect, on the
    # theory that a forged new document harms nobody; but a counterparty who
    # was sent a draft with the agent BCC'd can forge the lawyer's address, and
    # a forged "document" becomes the baseline every later comparison reads
    # against. No reply either: it would go to the spoofed lawyer, which turns
    # us into the attacker's megaphone.
    unauthenticated = router.authentication_failed(email.headers)
    if unauthenticated:
        what = routed.intent.value if routed else "a message"
        log.warning(
            "not acting on %s from %s: %s",
            what, email.from_address, unauthenticated,
        )
        record(job_id, email.message_id, "ignored", email.from_address,
               {"reason": unauthenticated, "intent": what})
        return

    # A client whose guidelines exclude AI, known from the message alone: the
    # matter it names, or the client's domain on To or CC. The document's
    # parties are checked once it is read (_prepare, _handle_followup), and
    # by triage before it calls the model (triage._gate).
    excluded = policy.excluded(email, identity.resolve_matter(email))
    if excluded:
        policy.forbid_ai(excluded)

    # The model's plan, when TRIAGE=model and there is one (triage.py). None
    # in every other case, and the rules below decide as they always have.
    decided = triage.decide(job_id, email)
    if decided is not None:
        done = _dispatch(job_id, email, provider, *decided)
        if done is not _RULES:
            return done

    # "No AI for Acme" / "AI ok for Acme": which clients' work never goes to a
    # model (policy.py). A playbook admin's call, like the playbook itself.
    no_ai = policy.command(email)
    if no_ai is not None:
        _no_ai_command(job_id, email, provider, *no_ai)
        return

    # The firm's playbook, by email: new positions, "approve playbook", "undo
    # the last playbook change". Before everything else a reply can mean,
    # because a playbook email may arrive on any thread. See playbook.py.
    meaning = playbook.intent(email)
    if meaning:
        playbook.handle(job_id, email, provider, meaning)
        return

    # "Audit report for September", from a playbook admin: the audit trail
    # as a CSV, to the sender alone (audit.py).
    period = audit.request(email) if not email.attachments else None
    if period:
        done = audit.handle(job_id, email, provider, period)
        record(job_id, email.message_id, done, email.from_address, {"kind": "audit"})
        return

    # "Turn the comments", "turn their markup": a Word document other people
    # have commented on or marked up, turned in a session. See comments.py.
    turning = comments.intent(email)
    if turning:
        comments.handle(job_id, email, provider, turning)
        return

    # "Blackline against what we sent on Tuesday", "blackline v4 against v1":
    # the Word comparison and its PDF, the baseline found on the thread, made
    # in a session. Only when the blackline agent is set up. See blackline.py.
    if blackline.intent(email):
        blackline.handle(job_id, email, provider)
        return

    if routed and routed.intent in _COMMANDS:
        _command(job_id, email, provider, _COMMANDS[routed.intent])
        return

    # A closing: signature packets across a deal's documents, signed pages
    # coming back, the executed set, the checklist. After the whole-message
    # commands above, before anything that reads a reply as being about a
    # review. See closing.py.
    from secondeye import closing

    if closing.handle(job_id, email, provider):
        return

    # BCC'd on a message with nothing to review: a cover note, a scheduling
    # reply, "thanks, see you Tuesday". A lawyer whose mail rule copies us on
    # everything sends dozens of these a day, and answering each with "I could
    # not find a document" is how that rule gets deleted, and with it every
    # review it would have triggered. We were not written to, so we say
    # nothing. This has to come before the follow-up branch below: a BCC'd
    # message on a thread we once reviewed is still addressed to someone else,
    # and treating "see you Tuesday" as an instruction produced a reply about
    # it.
    if identity.bcc_only(email) and not email.attachments:
        log.info("BCC'd on %s with nothing to review; staying quiet", email.message_id)
        record(job_id, email.message_id, "ignored", email.from_address,
               {"reason": "bcc with no document"})
        return

    # A reply about a document already under review. The footer promises the
    # lawyer can answer in plain English, and until this existed a reply with
    # no attachment got "I could not find a document to review".
    user_address = identity.resolve_user(email)
    existing_key = thread.resolve(
        user_address, email.thread_id, email.message_id, email.in_reply_to,
        email.references, tap=identity.tap_token(email),
    )
    thread_key = existing_key or thread.key(
        email.thread_id, email.message_id, user_address
    )
    state = thread.load(thread_key) if existing_key else None
    if state and state.muted:
        if not email.attachments:
            log.info("thread %s is muted; not answering %s", thread_key, email.message_id)
            record(job_id, email.message_id, "ignored", email.from_address,
                   {"reason": "thread muted"})
            return
        thread.mute(thread_key, False)  # a new document is a new request
    if state and not email.attachments and state.origin == "compare":
        # A reply to a comparison. There is no redline to undo and no question
        # to answer; the reply is about the later version, so it becomes the
        # instructions for a review of that document, which is what "look
        # harder at clause 4" asks for. From here on it is a review thread.
        if router.is_acknowledgement(email.body):
            record(job_id, email.message_id, "acknowledged", email.from_address, {})
            return
        log.info("reply on a comparison thread; reviewing the later version with it")
        email = email.model_copy(update={"attachments": [_as_attachment(state)]})
        thread.set_origin(state.id, "review")
        state = None
    if state and not email.attachments and negotiation.asks_status(email):
        # "Did they accept our changes?" on a document with a ledger: the
        # answer from the last version of theirs we read (negotiation.py).
        answered = negotiation.answer(email, state.id)
        if answered is not None:
            sent_id = provider.send(answered)
            if isinstance(sent_id, str):
                thread.add_alias(state.id, sent_id)
            record(job_id, email.message_id, "replied", email.from_address,
                   {"kind": "negotiation_status"})
            return
    if state and not email.attachments:
        _check_no_ai(email, state.matter_id, state.original, state.filename)
        _followup_guarded(job_id, email, provider, state)
        return
    if not email.attachments and router.is_acknowledgement(email.body):
        # "Thanks" in reply to something that never became a conversation (a
        # rejection, a confirmation). Not "attach a .docx".
        record(job_id, email.message_id, "acknowledged", email.from_address, {})
        return

    # Requests about versions rather than about one document: "what changed
    # between these" and "give me a clean copy". Decided from the sender's own
    # words, before pick_document, because two attachments is a rejection for a
    # review and exactly right for a comparison.
    if email.attachments and (
        intake.wants_comparison(email) or intake.wants_clean_copy(email)
        or _wants_repair(email) or intake.wants_renumber(email)
        or intake.wants_signature_pack(email) or intake.wants_calendar(email)
    ):
        _handle_version_request(job_id, email, provider)
        return

    try:
        att = intake.pick_document(email)
        mode = intake.infer_mode(email)
        instructions = intake.extract_instructions(email)
        mode = intake.mode_for(att, mode)
        entry = identity.entry_mode(email)
        user = identity.resolve_user(email)
        matter = identity.resolve_matter(email)
    except intake.Rejection as e:
        provider.send(reply.rejection(email, str(e)))
        # Which conversation ids the message carried, when it found none: a
        # reply that should have been a follow-up lands here if the lookup
        # missed, and the ids are the only way to see why. Ids, not content.
        seen = ({"in_reply_to": email.in_reply_to, "references": email.references[-6:],
                 "thread_id": email.thread_id}
                if email.in_reply_to or email.references else {})
        record(job_id, email.message_id, "rejected", email.from_address,
               {"reason": str(e), **seen})
        return
    return _Review(job_id, email, att, mode, instructions, entry, user, matter, thread_key)


# What `_dispatch` returns for a plan it does not carry out, so the rules do.
_RULES = object()

# The order a message with several of these is handled in, which is the
# order `_handle_version_request` has always tested them.
_VERSION_ORDER = ("sig_pages", "deadlines_calendar", "renumber", "compare", "blackline",
                  "clean_copy", "repair_formatting")
_CLOSING_WANTS = {"sig_packets": "start", "signed_pages_returned": "session",
                  "closing_checklist": "status"}


def _dispatch(job_id: str, email: InboundEmail, provider, plan: triage.Plan,
              ctx: triage.Context):
    """Carry out a triage plan (TRIAGE=model) with the functions the rules
    path calls. Returns what `_route` returns (the review to run, or None
    once answered), or `_RULES` for what the plan does not settle: an empty
    plan, a reply on a comparison, a request with nothing to act on. The
    rules then decide it, as they always have.

    The recipients the plan chose are applied to every reply (triage.address)
    and only take effect under REPLY_POLICY=model; `provider` is already
    policy.Policed, which readdresses anything else to the sender."""
    from secondeye import closing

    names = set(plan.names)
    if not names:
        return _RULES
    chosen = triage.recipients(plan, email)
    provider = triage.Addressed(provider, chosen)
    state = ctx.state

    if names <= {"ack", "ignore"}:
        record(job_id, email.message_id, "acknowledged" if "ack" in names else "ignored",
               email.from_address, {"reason": "triage: nothing to do"})
        return None
    if names & {"no_ai_add", "no_ai_lift"}:
        said = policy.command(email)
        label = plan.no_ai_client.strip() or (said[1] if said else "")
        if not label:
            return _RULES
        _no_ai_command(job_id, email, provider, "no_ai_add" in names, label)
        return None
    for name in triage.PLAYBOOK_INTENTS:
        if name in names:
            playbook.handle(job_id, email, provider, name.removeprefix("playbook_"))
            return None
    if "audit_report" in names:
        period = audit.request(email) or _period(plan)
        if period is None:
            return _RULES
        done = audit.handle(job_id, email, provider, period)
        record(job_id, email.message_id, done, email.from_address, {"kind": "audit"})
        return None
    if names & {"turn_comments", "turn_markup"} and email.attachments:
        comments.handle(job_id, email, provider, comments.TURN)
        return None
    if "blackline" in names and blackline.configured():
        # The blackline agent chooses the baseline from the versions the
        # conversation holds; without it a blackline is a comparison, below.
        blackline.handle(job_id, email, provider)
        return None
    for name in triage.COMMANDS:
        if name in names:
            if (name == "flag_again" and state is not None
                    and memory.unsuppression_request(router.strip_reply(email.body))):
                break   # "flag the Oxford comma again" names its point: a follow-up
            _command(job_id, email, provider, name)
            return None
    for name, want in _CLOSING_WANTS.items():
        if name in names:
            return None if closing.handle(job_id, email, provider, want) else _RULES

    if state is not None and not email.attachments:
        if state.muted:
            # "Stop" is the lawyer's own word, remembered: silence until a
            # new document arrives, whatever the reply seems to ask.
            record(job_id, email.message_id, "ignored", email.from_address,
                   {"reason": "thread muted"})
            return None
        if state.origin == "compare":
            return _RULES
        _followup_guarded(job_id, email, provider, state, plan)
        return None

    if email.attachments:
        want = next((n for n in _VERSION_ORDER if n in names), None)
        if want is not None:
            _version_by_plan(job_id, email, provider, plan, want)
            return None
        if "review" in names:
            return _review_by_plan(job_id, email, provider, plan, ctx, chosen)
    return _RULES


def _period(plan: triage.Plan):
    from datetime import UTC, date, datetime

    try:
        since = date.fromisoformat(plan.audit_since)
        until = (date.fromisoformat(plan.audit_until) if plan.audit_until
                 else datetime.now(UTC).date())
    except ValueError:
        return None
    return since, until


def _version_by_plan(job_id: str, email: InboundEmail, provider, plan: triage.Plan,
                     want: str) -> None:
    """A version request on attached documents, with the plan's document and,
    for a comparison, the baseline it named."""
    document = triage.attachment_named(email, plan.document)
    pair, previous = None, None
    if want in ("compare", "blackline"):
        if plan.baseline == "previous":
            previous = True
        else:
            base = triage.attachment_named(email, plan.baseline)
            others = [a for a in intake._reviewable(email) if a is not base]
            if base is not None and len(others) == 1:
                how = f"taking {base.filename} as the earlier version, as you said"
                pair = (base, others[0], how)
        want = "compare"
    _handle_version_request(job_id, email, provider, None, want=want, document=document,
                            pair=pair, previous=previous)


def _review_by_plan(job_id: str, email: InboundEmail, provider, plan: triage.Plan,
                    ctx: triage.Context, chosen: list[str]) -> _Review | None:
    """A review of the attachment the plan chose, read as whose paper the plan
    said. A name that matches no attachment falls back to intake's choice; a
    file intake would refuse is refused with intake's words."""
    user = identity.resolve_user(email)
    state = ctx.state
    thread_key = state.id if state is not None else thread.key(
        email.thread_id, email.message_id, user)
    if state is not None and state.muted:
        thread.mute(thread_key, False)  # a new document is a new request
    try:
        att = triage.attachment_named(email, plan.document)
        if att is None:
            log.info("triage named no attachment we have; intake chooses")
            att = intake.pick_document(email)
        else:
            # The one file, through intake's own gate: a logo, a vCard, an
            # unreadable format or an oversize file is refused as intake would.
            att = intake.pick_document(email.model_copy(update={"attachments": [att]}))
        try:
            requested = Mode(plan.review_mode)
        except ValueError:
            requested = intake.infer_mode(email)
        mode = intake.mode_for(att, requested)
        entry = identity.entry_mode(email)
    except intake.Rejection as e:
        provider.send(reply.rejection(email, str(e)))
        record(job_id, email.message_id, "rejected", email.from_address, {"reason": str(e)})
        return None
    job = _Review(job_id, email, att, mode, intake.extract_instructions(email), entry, user,
                  identity.resolve_matter(email), thread_key)
    job.recipients = chosen
    if plan.their_paper is not None:
        applies = entry is not identity.EntryMode.BCC_SENT and mode in their_paper.APPLIES_TO
        job.paper_override = (
            their_paper.Provenance(True, "triage", plan.their_paper_reason.strip().rstrip(".")
                                   or "of how it reached me")
            if plan.their_paper and applies else their_paper.OURS)
    return job


# The whole-message commands, as router.route_deterministic names them.
_COMMANDS = {
    router.Intent.REVOKE: "revoke", router.Intent.REMEMBER: "remember",
    router.Intent.FORGET: "forget", router.Intent.UNSUPPRESS: "flag_again",
    router.Intent.CONNECT: "connect", router.Intent.HELP: "help",
    router.Intent.SETUP: "setup", router.Intent.STOP: "stop",
}


def _no_ai_command(job_id: str, email: InboundEmail, provider, forbid: bool,
                   label: str) -> None:
    if playbook.is_admin(email.from_address):
        text = policy.answer_command(email, forbid, label)
    else:
        text = ("Only the firm's playbook admins can change which clients are "
                "excluded from AI, so nothing has changed.")
    provider.send(_confirmation(email, text, "noted"))
    record(job_id, email.message_id, "replied", email.from_address,
           {"kind": "no-ai", "forbid": forbid})


def _command(job_id: str, email: InboundEmail, provider, name: str) -> None:
    """One whole-message command: revoke, remember, forget, flag_again,
    connect, help, setup or stop. Named by the rules (`_COMMANDS`) or by a
    triage plan (TRIAGE=model)."""
    if name == "revoke":
        oauth.revoke(identity.resolve_user(email))
        # The grant is a vault credential; archiving it
        # purges the token at Anthropic. Raises rather than say "Disconnected"
        # while it still works.
        from secondeye import dms_mcp

        if dms_mcp.enabled():
            dms_mcp.revoke(identity.resolve_user(email))
        provider.send(_confirmation(
            email,
            "Disconnected. I no longer have access to the document system as you, "
            "and I will review documents on their own from now on. Reply "
            '"connect" whenever you want to turn it back on.',
            "disconnected",
        ))
        record(job_id, email.message_id, "revoked", email.from_address, {})
        return

    if name in ("remember", "forget"):
        # Only the lawyer's own reply turns a note the agent wrote into memory
        # every later review reads. The agent writing it directly meant a line
        # in a counterparty's draft could plant "this lawyer accepts uncapped
        # liability" in the lawyer's own profile.
        user = identity.resolve_user(email)
        matter = _thread_matter(email, user)
        if name == "remember":
            n = memory.confirm_notes(user, None, matter_id=matter)
            text = (f"Remembered. I will apply {'that' if n == 1 else f'those {n} notes'} "
                    "from now on." if n else "There was nothing waiting to be remembered.")
        else:
            n = memory.discard_notes(user, None, matter_id=matter)
            text = "Forgotten." if n else "There was nothing waiting to be forgotten."
        provider.send(_confirmation(email, text, name))
        record(job_id, email.message_id, "replied", email.from_address,
               {"kind": name, "notes": n})
        return

    if name == "flag_again":
        # "Flag it again", answering the line a review added about something
        # it had stopped flagging. "It" is the last thing it said so about.
        user = identity.resolve_user(email)
        try:
            lifted = memory.lift_last_announced(user, _thread_matter(email, user))
        except Exception:
            log.exception("could not lift a suppression")
            lifted = None
        text = (f"Done. I will raise {lifted} again from your next review on." if lifted
                else "There was nothing I had stopped flagging for you, so nothing "
                     "has changed.")
        provider.send(_confirmation(email, text, "noted"))
        record(job_id, email.message_id, "replied", email.from_address,
               {"kind": "unsuppression", "lifted": bool(lifted)})
        return

    if name == "connect":
        # Offering a link to a system that does not exist wastes the one moment
        # this product asks for a browser. With the shipped defaults there is no
        # authorize URL and no client id, so the "one-click link" is a relative
        # path with an empty client_id, and clicking it resolves to nothing.
        if settings().dms_provider.lower() == "none":
            provider.send(_confirmation(
                email,
                "There is no document system connected for this firm yet, so "
                "there is nothing for me to connect you to. I will keep "
                "reviewing documents on their own, and I will offer you the "
                "link as soon as one is set up.",
                "nothing to connect",
            ))
            record(job_id, email.message_id, "no_dms", email.from_address, {})
            return
        provider.send(consent.consent_email(email))
        record(job_id, email.message_id, "consent_sent", email.from_address, {})
        return

    if name in ("help", "setup"):
        agent = settings().mail_agent_address
        text = (reply.help_text(agent) if name == "help"
                else reply.setup_text(agent))
        out = _confirmation(email, text, name)
        out.html_body = reply.text_as_html(text)
        reply.attach_contact_card(out, settings().mail_agent_name, agent)
        provider.send(out)
        record(job_id, email.message_id, "replied", email.from_address,
               {"kind": name})
        return

    if name == "stop":
        # Remembered, not just promised. The thread stays silent until a new
        # document arrives on it; this confirmation is the last word.
        owner = identity.resolve_user(email)
        stopped_key = thread.resolve(
            owner, email.thread_id, email.message_id, email.in_reply_to, email.references,
            tap=identity.tap_token(email),
        ) or thread.key(email.thread_id, email.message_id, owner)
        thread.remember_stop(stopped_key, owner)
        try:
            thread.add_alias(stopped_key, email.message_id, email.thread_id)
        except Exception:
            log.exception("could not alias the stop request")
        provider.send(_confirmation(
            email,
            "Understood. I will not reply on this thread again. Send me a "
            "document whenever you want another review.",
            "stopped",
        ))
        record(job_id, email.message_id, "stopped", email.from_address, {})
        return


def _review_in_process(job: _Review, provider) -> None:
    """The review of one document, every stage in this process."""
    email = job.email
    # No "back in about 5 minutes" notice here: the Workflow sends that
    # (review-flow.ts). In one process there is nobody to send it for.

    # A failure AFTER the reply was delivered is not a failed review; nor,
    # in the recovery path below, is one after the review finished
    # (job.result).
    delivered = False
    try:
        if not _prepare(job, provider):
            return
        if policy.ai_forbidden():
            # A no-AI client: the checks already run are the review.
            result = no_ai_result(job)
        else:
            record(job.job_id, email.message_id, "reviewing", email.from_address, {})
            result = review.review(**review_job(job).review_kwargs())
        _merge_review(job, result)
        out = _compose(job)
        sent_id = _deliver(provider, out)
        delivered = True
        _after_delivery(job, sent_id)
    except Exception as e:  # noqa: BLE001
        log.error("job %s failed: %s", job.job_id, traceback.format_exc())
        record(job.job_id, email.message_id, "failed", email.from_address, {"error": str(e)})

        if delivered:
            # The lawyer already has the review. Whatever failed afterwards -
            # recording the job, aliasing the thread - must not produce a second
            # email telling them their document was never reviewed.
            return

        if job.result is not None:
            try:
                out, _ = _failure_reply(job)
                provider.send(out)
            except Exception:
                log.exception("could not deliver the review for %s", email.message_id)
            return

        out, _ = _failure_reply(job)
        sent_id = provider.send(out)
        _remember_degraded(job, sent_id)


def _prepare(job: _Review, provider) -> bool:
    """Everything the review needs before the model sees the document: the
    text, the Word copy, what changed since last time, the archive and the
    deterministic checks. False when the document could not be read, and the
    lawyer has been told so."""
    email, att, mode = job.email, job.att, job.mode
    record(job.job_id, email.message_id, "reviewing", email.from_address, {})
    try:
        doc = extract.extract(att)
    except extract.Unreadable as e:
        # A format we cannot read is a conversation, not a failure.
        provider.send(reply.rejection(email, str(e)))
        record(job.job_id, email.message_id, "rejected", email.from_address,
               {"reason": str(e)})
        return False
    job.doc = doc
    # Before anything that might call a model (a Word copy built in a session,
    # the comparison's assessment): a no-AI client named as a party.
    _check_no_ai(email, job.matter, doc=doc)

    # The Word copy the tracked changes go into. A .docx is its own; a PDF,
    # a legacy .doc or a text file is converted or rebuilt (reflow), and
    # the reply says how it was made. Decided before the review so the
    # mode the model is told is the mode the reply describes: a scan has
    # nothing to build from, and the review of one is notes only.
    redline_notes = job.redline_notes
    others = intake.set_aside(email, att)
    if others:
        # Always say which file was read. A lawyer who attached two and got
        # one verdict cannot otherwise tell which one it is about.
        redline_notes.append(
            f"I reviewed {att.filename}. Not reviewed: {', '.join(others)}."
        )
    working: bytes | None = att.content if intake.is_redlineable(att) else None
    job.working_is_original = working is not None
    if working is None and mode is not Mode.MEMO_ONLY and reflow.convertible(att):
        try:
            rebuilt = reflow.to_docx(att, doc)
            working = rebuilt.content
            if rebuilt.note:
                redline_notes.append(rebuilt.note)
        except reflow.CannotConvert as e:
            mode, job.explain_mode = Mode.MEMO_ONLY, False
            redline_notes.append(str(e))
        except Exception:
            # Never the review's problem. The findings still go out; only
            # the marked-up copy does not.
            log.exception("could not build a Word copy of %s", att.filename)
            mode, job.explain_mode = Mode.MEMO_ONLY, False
            redline_notes.append(
                "I could not build a copy to mark up this time, so these are "
                "notes only."
            )
    job.mode = mode
    job.working = working

    # What changed since the version of this document we reviewed before,
    # if there was one (ROADMAP item 8). PRODUCT.md: the most useful review
    # is almost never everything wrong with the contract, it is what moved
    # since the version the lawyer last saw. Never allowed to fail the
    # review: it leads the reply when it exists and is absent otherwise.
    try:
        job.since = _since_last_time(job.user, att, doc, working, job.instructions)
    except Exception:
        log.exception("could not work out what changed since last time")

    # Archive before reviewing, so a review that fails still leaves a record
    # of what arrived. Off unless the firm has switched it on.
    try:
        archive.store(
            email,
            owner=job.user,
            direction="sent" if job.entry is identity.EntryMode.BCC_SENT else "received",
            matter_id=job.matter,
            external=identity.external_recipients(email),
            attachment_text={att.filename: doc.as_prompt(limit=200_000)},
            client_id=_client_of(job),
        )
    except Exception:
        log.exception("archiving failed for %s", email.message_id)

    # Deterministic first. These are exact, free, and never hallucinated,
    # and telling the agent what they caught keeps it off the mechanics.
    # A scan has no text layer, so these find nothing and must not be
    # reported as "nothing wrong". The agent is told to say so instead.
    job.mechanical = checks.run_all(doc, att.content)
    if job.since is not None and job.since.drift:
        # A party renamed since the version reviewed before and still called
        # by its old name: said with the history, in place of the same
        # finding made without it.
        replaced = {f.anchor for f in job.since.drift}
        job.mechanical = [f for f in job.mechanical
                          if not (f.category == "party-name" and f.anchor in replaced)]
        job.mechanical += job.since.drift
    try:
        # The table of dates and time limits for the reply. Informational,
        # so a failure here costs the table and never the review.
        job.dates = deadlines.rows(doc)
    except Exception:
        log.exception("could not read the document's dates")
        job.dates = []

    # Checks only possible because we can see the message, not just the
    # document: wrong recipient, wrong attachment, and claims in the
    # covering email that the document does not actually support.
    job.mechanical += email_checks.run_all(
        email, doc, att, identity.external_recipients(email)
    )

    # The other side's draft is answered, not polished (their_paper.py).
    # A guess that fails is the firm's own paper, which is how it was.
    try:
        job.paper = job.paper_override or their_paper.detect(email, att.content, job.entry,
                                                             mode)
    except Exception:
        log.exception("could not tell whose paper this is")
        job.paper = their_paper.OURS
    return True


def _check_no_ai(email: InboundEmail, matter: str | None, content: bytes | None = None,
                 filename: str = "", doc=None) -> None:
    """Hold the rest of this message to no model calls if its matter, its
    recipients or its document's parties name a no-AI client (policy.py).
    `content` is a conversation's held document, read here if `doc` is not
    given; reading a .docx is code, not a model."""
    if policy.ai_forbidden():
        return
    parties: list[str] = []
    try:
        if doc is None and content and filename.lower().endswith(".docx"):
            doc = extract.extract(Attachment(filename=filename or "document.docx",
                                             content_type=DOCX_TYPE,
                                             size_bytes=len(content), content=content))
        if doc is not None:
            parties = [name for name, _ in checks._parties(doc)[0]]
    except Exception:
        log.exception("could not read the parties for the no-AI check")
    excluded = policy.excluded(email, matter, parties)
    if excluded:
        policy.forbid_ai(excluded)


def no_ai_result(job: _Review) -> ReviewResult:
    """The review of a no-AI client's document: the deterministic checks,
    which _merge_review puts in, and one line saying why that is all."""
    job.redline_notes.insert(0, policy.no_ai_line(policy.ai_forbidden() or "This client"))
    return ReviewResult(mode=job.mode, summary="")


def mechanical_only(job: _Review, provider) -> None:
    """A no-AI client's review, start to finish, with no session. For the
    Workflow, whose prepare step runs it in place of the session steps."""
    _merge_review(job, no_ai_result(job))
    _after_delivery(job, _deliver(provider, _compose(job)))


def _client_of(job: _Review) -> str | None:
    """Whose document this is, for the memory wall: the matter number first,
    then the parties (identity.resolve_client). None mounts no client memory."""
    text = "\n".join(b.text for b in job.doc.blocks) if job.doc else ""
    return identity.resolve_client(job.email, job.matter, text)


def review_job(job: _Review, stores: bool = True) -> sessions_api.ReviewJob:
    """What the review session is given: the same inputs, whichever way it runs.
    `stores=False` leaves the memory stores out, for collecting a session that
    has them already: looking them up may create them."""
    att, working = job.att, job.working
    # A version from the other side of a document with a ledger: our points,
    # the comparison against what we sent, their covering email. Built once,
    # when the session starts, and kept for the reply (negotiation.py).
    block, evidence = negotiation.evidence_block(job) if stores else ("", None)
    return sessions_api.ReviewJob(
        job_id=job.job_id,
        doc=job.doc,
        mode=job.mode,
        instructions=job.instructions,
        user_address=job.user,
        matter_id=job.matter,
        memory_stores=memory.stores_for(job.user, job.matter, _client_of(job)) if stores else None,
        checks_block=checks.as_prompt_block(job.mechanical),
        house_style=versions.house_style(),
        original=working,
        native=(att.filename, att.content) if not job.working_is_original else None,
        changes_block=job.since.block if job.since else "",
        their_paper=job.paper.theirs,
        negotiation_block=block,
        negotiation_evidence=evidence,
    )


def _merge_review(job: _Review, result: ReviewResult) -> None:
    """Take the session's result into the job. Mechanical findings lead:
    they are certain, and a placeholder left in the document outranks any
    judgment call the agent made."""
    result.findings = job.mechanical + result.findings
    job.result = result


def _compose(job: _Review, notice_sent_id: object = None) -> OutboundEmail:
    """The reply to a finished review: the redline written and checked, the
    conversation recorded so a reply can act on it, and the email built.
    Nothing is sent. `notice_sent_id` is the slow-review notice's message id,
    when one went out, so a reply to it lands in this conversation too."""
    email, att, mode, result = job.email, job.att, job.mode, job.result
    instructions, entry, user, matter = job.instructions, job.entry, job.user, job.matter
    working, redline_notes, since = job.working, job.redline_notes, job.since
    paper_notes, paper_files = their_paper.settle(
        result, job.paper, att.filename, reply.MAX_ATTACHMENT_BYTES)
    redline_notes.extend(paper_notes)
    # What they did with our points, and whether their email says so. Settled
    # before the conversation is recorded, which moves the ledger here.
    settled = None
    try:
        settled = negotiation.settle(job, result)
    except Exception:
        log.exception("could not settle the negotiation round")
    if settled is not None:
        redline_notes.extend(settled.notes)

    redlined, applied, skipped = None, [], result.findings
    commented: list = []
    if mode in (Mode.REDLINE, Mode.PROOFREAD) and working is not None:
        record(job.job_id, email.message_id, "redlining", email.from_address, {})
        out = _redline(working, result)
        # Only attach a document when an edit actually landed. Sending back
        # an unchanged copy labelled "(redline)" tells the lawyer edits were
        # made when none were, which is worse than sending nothing.
        redline_notes.extend(out.notes)
        # Attach when anything landed in the document: an edit, or a
        # comment asking a question beside the clause it is about.
        if (out.applied or out.commented) and redline.verify_opens(out.content):
            redlined, applied, skipped = out.content, out.applied, out.skipped
            commented = out.commented
        else:
            applied, skipped = [], result.findings
            if out.applied or out.commented:
                log.error("redline produced an unreadable document; sending memo only")

    # For a PDF, the findings on the PDF itself as well: a note beside the
    # text each is about, and the blockers highlighted. The Word copy has
    # lost the layout; this is the page the lawyer was actually sent.
    extra_attachments: list[Attachment] = []
    if since and since.attachment is not None:
        extra_attachments.append(since.attachment)
    # The dated deadlines as a calendar file, beside the table that lists
    # them, when there are two or more; the undated ones are named instead.
    try:
        found = ics.read(job.doc) if job.doc is not None else None
        calendar = ics.attachment(found, att.filename) if found else None
    except Exception:
        log.exception("could not build the deadlines calendar")
        calendar = None
    if calendar is not None:
        extra_attachments.append(calendar)
        redline_notes.append(ics.note(found, calendar.filename))
    extra_attachments.extend(paper_files)
    if (mode in (Mode.REDLINE, Mode.PROOFREAD) and result.findings
            and identify(att.content, att.filename) is Kind.PDF):
        marked = annotate.annotate(att.content, result.findings, instructions)
        redline_notes.extend(marked.notes)
        if marked.content and len(marked.content) <= reply.MAX_ATTACHMENT_BYTES:
            name = reply.annotated_name(att.filename)
            extra_attachments.append(Attachment(
                filename=name, content_type="application/pdf",
                size_bytes=len(marked.content), content=marked.content,
            ))
            redline_notes.append(
                f"{name} is your PDF with each finding as a note beside the text "
                "it concerns"
                + (", and the blockers highlighted." if marked.highlighted
                   else ". Nothing is changed in it.")
            )

    if entry is identity.EntryMode.BCC_SENT:
        # The version that actually went out. Read it against the redline
        # we gave them, record what was kept, and say so in one line.
        try:
            learned = reconcile.reconcile(job.job_id, user, att)
        except Exception:
            log.exception("could not reconcile the sent version")
            learned = None
        if learned is not None:
            redline_notes.append(learned.sentence())

    # Remember the conversation, so a reply can act on this document. The
    # document held is the Word copy: "undo" and "add a clause" rebuild
    # the redline from it, and a PDF cannot be rebuilt into.
    state = None
    try:
        state = _hold(job)
        thread.record_changes(state.id, state.round, applied, origin="check")
        thread.record_questions(state.id, state.round, result.findings)
        # A letter beside each point that is not a change: "B is fine".
        labels = thread.record_findings(state.id, state.round,
                                        reply.dismissable(result, applied))
        state = thread.load(state.id)
        numbers = [state.number_for(f.anchor, f.suggested_text) for f in applied]
        # What this review puts to the other side, for the next version of theirs.
        negotiation.record_review(job, result, applied)
        if isinstance(notice_sent_id, str):
            # A reply to the notice is a reply in this conversation too.
            thread.add_alias(state.id, notice_sent_id)
    except Exception:
        log.exception("could not record thread state")
        numbers = None
        labels = None
    # The address the reply's one-tap links write to. It carries this
    # conversation, since a mailto: link cannot set In-Reply-To. None for
    # a document already sent, or notes only: nothing there to answer.
    tap_to = None
    if (state is not None and entry is not identity.EntryMode.BCC_SENT
            and result.mode is not Mode.MEMO_ONLY and settings().one_tap_links):
        try:
            tap_to = identity.tap_address(thread.new_tap_token(state.id))
        except Exception:
            log.exception("could not issue a one-tap token")

    out = reply.compose(
        email,
        result,
        redlined,
        att.filename,
        applied,
        skipped,
        entry=entry,
        external=identity.external_recipients(email),
        notes=redline_notes,
        commented=commented,
        explain_mode=job.explain_mode,
        extra_attachments=extra_attachments,
        # The negotiation's list of their changes replaces this section; the
        # comparison stays attached.
        since=tuple(since.section) if since and not (settled and settled.outcome) else None,
        numbers=numbers,
        tap_address=tap_to,
        dates=job.dates,
        labels=labels,
        their_paper=result.their_paper,
    )
    if settled is not None:
        # Their claims first: the line a phone shows under the subject.
        negotiation.lead(out, settled)
    # Never block the review on setup. Do the work, then add what a first
    # timer needs to know, in BOTH bodies: most lawyers read the HTML one,
    # and the offer used to be invisible to all of them.
    postscripts: list[str] = []
    first_time = state is not None and _first_conversation(user, state.id)
    if first_time:
        # Once. Nothing is installed and nobody is trained, so the replies
        # that work are learned here or not at all.
        # One line, not the manual: the full list was longer than the
        # review on a clean document.
        postscripts.append('First time? Reply "help" to see everything I can do.')
        # And the card, so the address autocompletes next time.
        reply.attach_contact_card(out, settings().mail_agent_name,
                                  settings().mail_agent_address)
    # With the help line, once. It used to ride on every review until the
    # lawyer connected, and there was no way to say no.
    if first_time and consent.needs_consent(user):
        postscripts.append(_consent_offer(email))
    # What it has learned about this lawyer and not yet said, one line at
    # a time. A suppression nobody mentions reads, three weeks later, as
    # the review having got worse.
    learned = None
    try:
        learned = memory.next_announcement(user, matter)
    except Exception:
        log.exception("could not read what to announce")
    job.learned = learned
    if learned:
        postscripts.append(learned[1])
    try:
        pending = memory.pending_notes(user, matter)
    except Exception:
        log.exception("could not read pending notes")
        pending = []
    if pending:
        noted = "; ".join(f"“{e.statement.rstrip('.')}”" for e in pending[:3])
        postscripts.append(
            f"For next time, if you agree: {noted}. Reply \"remember that\" "
            "to keep it; otherwise I will not."
        )
    for offer in postscripts:
        out.text_body = out.text_body.rstrip("\n") + "\n\n" + offer + "\n"
        if out.html_body:
            out.html_body = out.html_body.replace(
                "</div>",
                f'<p style="margin:1em 0 0;color:#555;white-space:pre-line">'
                f"{reply.linked(offer, _POSTSCRIPT_REPLIES, tap_to, email.subject)}"
                "</p></div>",
            )
    # Addressed as a triage plan chose (TRIAGE=model), under REPLY_POLICY=model
    # only; policy.py has the last word either way.
    return triage.address(out, job.recipients)


def without_attachments(out: OutboundEmail) -> OutboundEmail:
    """The same reply with the files taken off, saying so. What goes out when
    the full reply cannot be sent, size being the usual reason."""
    return out.model_copy(update={
        "attachments": [],
        "text_body": (
            "I reviewed this but could not send the marked-up copy, so here "
            "are the findings on their own.\n\n" + out.text_body
        ),
    })


def _deliver(provider, out: OutboundEmail):
    """Send the reply; the message id it went out under."""
    try:
        return provider.send(out)
    except Exception:
        # The review succeeded; only delivery failed. Retrying the whole
        # thing would re-run the agent and bill for it again, and the
        # fallback below would tell the lawyer their document was never
        # reviewed, which is false and the most misleading thing we could
        # say. Try once more without the attachment, which is the usual
        # cause: size limits.
        log.exception("send failed; retrying without the attachment")
        return provider.send(without_attachments(out))


def _after_delivery(job: _Review, sent_id) -> None:
    """What only a delivered reply earns: the announcement counted as said,
    the reply's own id tied to the conversation, the job marked replied."""
    email = job.email
    if job.learned:
        # Said, so never said again. Only after delivery: a line that did
        # not arrive has not been heard.
        try:
            memory.mark_announced(job.learned[0])
        except Exception:
            log.exception("could not record an announcement")
    # The lawyer's reply will point at OUR message, not theirs, so that id
    # has to resolve back to this conversation too.
    try:
        thread.add_alias(job.thread_key, sent_id)
    except Exception:
        log.exception("could not alias the outbound message")
    record(job.job_id, email.message_id, "replied", email.from_address, {})


def _failure_reply(job: _Review) -> tuple[OutboundEmail, bool]:
    """The one email a review that failed still owes, and whether the
    conversation must then be kept as a degraded one (_remember_degraded)."""
    email, att = job.email, job.att
    if job.result is not None:
        # The review finished and only delivery failed. Falling through to
        # the mechanical-only message below would state, untruthfully, that
        # "nothing here speaks to the substance of the document", and a
        # lawyer who believes that sends a document whose substance we did
        # review and did find fault with. Carry the findings instead, with
        # no attachment, since size is the usual reason a send fails.
        retry = reply.compose(
            email, job.result, None, att.filename, [], job.result.findings
        )
        retry.text_body = (
            "I reviewed this but had trouble sending the reply, so here "
            "are the findings on their own.\n\n" + retry.text_body
        )
        return retry, False

    # The deterministic pass may already have found real blockers. Throwing
    # them away because the model call failed would be the worst possible
    # outcome: a lawyer told nothing is wrong when a placeholder is still in
    # the document. Send what we are certain of, and say what is missing.
    if job.mechanical:
        partial = ReviewResult(
            mode=Mode.MEMO_ONLY,
            summary=(
                "I could not complete the full review, so this covers only the "
                "mechanical checks. Treat it as incomplete: nothing here speaks "
                "to the substance of the document."
            ),
            findings=job.mechanical,
        )
        return reply.compose(email, partial, None, att.filename, [], job.mechanical,
                             explain_mode=False), True

    return reply.rejection(
        email,
        "Something went wrong on my side while reviewing that document. "
        "Nothing was changed and nothing was sent anywhere else.",
    ), True


# How long a model-backed review runs before the Workflow tells the lawyer it
# is under way. Most reviews are back inside it and say nothing extra.
SLOW_NOTICE_AFTER: float = 90.0


def _model_backed() -> bool:
    """Whether a review will call the model. Without the agents configured it
    fails at once and the mechanical findings go out; nothing to wait for."""
    return managed.configured()


def notice_delay(entry: identity.EntryMode) -> float | None:
    """Seconds before the slow-review notice is due, or None if none is owed:
    none without a model to wait for, and none for a document already sent,
    whose review is an audit nobody is waiting on."""
    delay: float | None = SLOW_NOTICE_AFTER
    if (delay is None or delay <= 0 or not _model_backed()
            or entry is identity.EntryMode.BCC_SENT):
        return None
    return delay


# The replies a postscript quotes, each a whole-message phrase the router acts
# on as it stands, so each is safe as a one-tap link.
_POSTSCRIPT_REPLIES = ["help", "connect", "remember that", "flag it again"]


def _redline(working: bytes, result: ReviewResult) -> redline.RedlineOutput:
    """The marked-up copy: the one the session wrote if it proves out, ours
    otherwise. Never fewer tracked changes than the local writer produces.

    DECISIONS 29: the writer runs in Anthropic's container as a skill, and the
    file it leaves in the session outputs is re-verified here before anything
    is attached. The local writer runs on the same findings regardless, for
    two reasons. Its record of what landed, what was skipped and why is the
    manifest the email is built from, and a manifest must come from code we
    ran, not from the model's account of a script it ran. And it is the
    fallback: if the session produced no file, or a file that fails
    `redline.verify`, or one whose revisions do not match what the findings
    call for, the lawyer gets the local result and nothing is lost.
    """
    local = redline.apply(working, result)
    produced = [(name, data) for name, data in (result.outputs or [])
                if "(redline)" in name.lower() and name.lower().endswith(".docx")]
    if not produced:
        if result.outputs is not None and (local.applied or local.commented):
            log.info("the session left no redline; using the local writer's")
        return local
    name, data = produced[-1]
    ok, why = redline.verify(data, expect_revisions=bool(local.applied))
    if not ok:
        log.error("the session's redline %s failed verification (%s); using ours", name, why)
        return local
    if redline.revision_authors(data) - redline.revision_authors(local.content):
        log.error("the session's redline carries revisions by an unexpected author; using ours")
        return local
    if compare._plain(data, body_only=False) != compare._plain(local.content, body_only=False):
        # Accept-all on both must read the same: the container ran the same
        # code on the same findings, so any difference is something else.
        log.error("the session's redline does not match the local writer's text; using ours")
        return local
    log.info("using the redline written in the session (%s, %d bytes)", name, len(data))
    return redline.RedlineOutput(content=data, applied=local.applied, skipped=local.skipped,
                                 notes=local.notes, commented=local.commented)


class _Since:
    """What changed since the version of this document reviewed last time."""

    def __init__(self, section: tuple, attachment: Attachment | None, block: str,
                 drift: list[Finding] | None = None) -> None:
        self.section = section
        self.attachment = attachment
        self.block = block
        # Parties renamed since then and still called by the old name
        # somewhere; read by _prepare in the same step, never stored.
        self.drift = drift or []


# How many of the changes the reply names. The comparison attached carries all
# of them; the email is for deciding whether to open it.
SINCE_LISTED = 5
# How much of the change list the review agent is shown. Enough to know where
# to dwell; not a second copy of the document.
SINCE_BLOCK_CHARS = 8000


def _since_last_time(user: str, att, doc, working: bytes | None,
                     instructions: str) -> _Since | None:
    """The comparison against the version of this document we reviewed before,
    or None when there is nothing honest to say: no earlier conversation, the
    same bytes again (a resend), the same words, or no Word copy to compare.

    The earlier conversation is found the way the learning loop finds it: by
    filename, then by how alike the texts are. A by-name match is then held to
    the same similarity bar, because two clients' NDAs are both called
    "NDA.docx" and a comparison between them would report a rewrite that never
    happened. A document renegotiated past that bar gets an ordinary review,
    which is the conservative direction to be wrong in.
    """
    if working is None:
        return None
    text = reconcile._norm(" ".join(b.text for b in doc.blocks))
    if not text:
        return None
    state = reconcile.conversation_for(user, att, text, require_changes=False)
    if state is None or not state.original:
        return None
    if state.original == att.content or state.original == working:
        return None
    held = Attachment(filename=state.filename, content_type="",
                      size_bytes=len(state.original), content=state.original)
    earlier_text = reconcile._text_of(held)
    if not earlier_text or earlier_text == text or not reconcile._similar(earlier_text, text):
        return None

    earlier = reflow.to_docx(held).content
    comparison = compare.compare(earlier, working)
    if comparison.identical:
        return None
    drift: list[Finding] = []
    try:
        earlier_doc = extract.extract(Attachment(
            filename=reflow.docx_name(state.filename), content_type=DOCX_TYPE,
            size_bytes=len(earlier), content=earlier))
        drift = checks.party_drift_since(
            earlier_doc, doc, f"since the version I reviewed on {state.seen_on}"
            if state.seen_on else "since the version I reviewed before")
    except Exception:
        log.exception("could not compare the parties with the earlier version")
    # The review is about to call the model anyway, so the assessment's cost is
    # one more call, and it is what turns "6 changes" into "6 changes, 2 worth
    # attention". explain() never raises.
    compare.explain(comparison, doc, instructions, versions.house_style())

    ranked = sorted(comparison.changes, key=lambda c: versions._RISK_ORDER.get(c.risk, 3))
    matter = [c for c in ranked if c.risk in ("high", "medium")]
    total = len(comparison.changes)
    line = f"{total} change{'s' if total != 1 else ''}"
    if any(c.risk or c.impact for c in comparison.changes):
        line += (f", {len(matter)} worth attention" if matter
                 else ", nothing that shifts the deal")
    line += "."
    heading = f"Since the version I reviewed on {state.seen_on or 'an earlier day'}"
    items = [_since_item(c) for c in ranked[:SINCE_LISTED]]
    if total > SINCE_LISTED:
        items.append(f"and {total - SINCE_LISTED} more, all marked in the attached comparison"
                     if comparison.content else f"and {total - SINCE_LISTED} more")

    attachment = None
    if comparison.content and len(comparison.content) <= reply.MAX_ATTACHMENT_BYTES:
        ok, reason = redline.verify(comparison.content, expect_revisions=True)
        if ok:
            attachment = Attachment(
                filename=_since_name(att.filename), content_type=DOCX_TYPE,
                size_bytes=len(comparison.content), content=comparison.content,
            )
        else:
            log.error("the since-last-time comparison failed verification: %s", reason)

    return _Since((heading, line, items), attachment, _since_block(comparison), drift)


def _since_item(change) -> str:
    label = {"replaced": "Changed", "inserted": "Added",
             "deleted": "Deleted", "moved": "Moved"}[change.kind]
    head = f"{label}, {change.where}"
    if change.risk:
        head = f"Key: {head}" if change.risk == "high" else head
    if change.kind == "inserted":
        return f"{head}: “{_clip(' '.join(change.after.split()), 90)}”"
    if change.kind == "deleted":
        return f"{head}: “{_clip(' '.join(change.before.split()), 90)}”"
    before, after = versions._differing(change.before, change.after)
    return f"{head}: “{_clip(before, 60)}” → “{_clip(after, 60)}”"


def _since_block(comparison) -> str:
    lines: list[str] = []
    used = 0
    for i, c in enumerate(comparison.changes):
        entry = f"[{i}] {c.kind} at {c.where}"
        if c.before:
            entry += "\nBEFORE: " + " ".join(c.before.split())
        if c.after:
            entry += "\nAFTER: " + " ".join(c.after.split())
        if used + len(entry) > SINCE_BLOCK_CHARS:
            lines.append(f"({len(comparison.changes) - i} further changes not listed)")
            break
        lines.append(entry)
        used += len(entry) + 2
    return "\n\n".join(lines)


def _since_name(filename: str) -> str:
    stem, _, _ = filename.rpartition(".")
    return f"{stem or filename} (changes since last time).docx"


def _hold(job: _Review) -> thread.ThreadState:
    """The conversation for this review, holding its document, with every way
    the lawyer's message can be referred back to. The document held is the
    Word copy: "undo" and "add a clause" rebuild the redline from it, and a
    PDF cannot be rebuilt into. A conversation this message already belongs
    to takes the new document as its current version (thread.start)."""
    att, email = job.att, job.email
    held_name = att.filename if job.working_is_original else reflow.docx_name(att.filename)
    state = thread.start(job.thread_key, job.user, held_name, job.working or att.content,
                         job.matter, client_id=_client_of(job))
    thread.add_alias(state.id, email.message_id, email.thread_id)
    return state


def _remember_degraded(job: _Review, *sent_ids) -> None:
    """Keep the conversation even though the review did not finish: the
    document held as a finished review holds it, and the mechanical findings'
    questions recorded, so "30" in reply answers one. `sent_ids` are the
    messages the lawyer may reply to (the apology, a slow-review notice).

    Thread state used to be written only after a successful review, so on a
    day the model was unreachable the lawyer got the mechanical findings, or an
    apology ending "reply to this email and I will pick it up from there", and
    their reply was answered with "I could not find a document to review". The
    document had arrived; we had simply not kept hold of it. The Workflow's
    failure path then kept it only for a conversation it had never seen, and
    only once the reply had been sent, and the canary's "30" got that answer
    again (2026-09-29).
    """
    try:
        state = _hold(job)
        thread.record_questions(state.id, state.round, job.mechanical)
        thread.add_alias(state.id, *[i for i in sent_ids if isinstance(i, str)])
    except Exception:
        log.exception("could not keep the conversation for a degraded review")


def _wants_repair(email: InboundEmail) -> bool:
    """Repair runs in a Managed Agents session, so it is only on offer when
    the agents are configured."""
    return managed.configured() and intake.wants_repair(email)


def _handle_version_request(job_id: str, email: InboundEmail, provider,
                            state: thread.ThreadState | None = None,
                            want: str | None = None, document: Attachment | None = None,
                            pair: tuple | None = None, previous: bool | None = None) -> None:
    """A comparison or a clean copy. One reply, and a true one if it fails.

    Which request it is comes from the sender's words (intake.wants_*), or,
    from a triage plan (TRIAGE=model), from `want`: one of sig_pages,
    deadlines_calendar, renumber, clean_copy, compare, repair_formatting,
    with the plan's `document` and, for a comparison, its (earlier, later,
    how) `pair` or `previous` for the version seen before."""
    def asks(name: str, rule) -> bool:
        return want == name if want is not None else rule(email)

    def picked() -> Attachment:
        return document or intake.pick_document(email)

    try:
        record(job_id, email.message_id, "reviewing", email.from_address, {})
        if asks("sig_pages", intake.wants_signature_pack):
            status = sigpack.handle(email, provider, state)
        elif asks("deadlines_calendar", intake.wants_calendar):
            status = ics.handle(email, provider, state)
        elif asks("renumber", lambda e: intake.wants_renumber(e)
                  and not intake.wants_clean_copy(e)):
            # Their attachment when there is one, else the document this
            # conversation holds, as they sent it.
            att = picked() if email.attachments else None
            status = versions.handle_renumber(
                email, provider, att.filename if att else state.filename,
                att.content if att else state.original,
                note="" if att else versions.RENUMBERED_ORIGINAL)
        elif state is not None and email.attachments:
            # Their version, as they left it in Word. The thread's copy knows
            # nothing about the changes they rejected or made themselves.
            att = picked()
            status = versions.handle_clean_copy(email, provider, att.filename, att.content)
        elif state is not None:
            # A reply on a review thread with nothing attached: the document
            # rebuilt from the changes still standing in this conversation.
            # That is not the file on their desktop, and a change they rejected
            # in Word comes back accepted, so say which copy this is.
            current, landed, _ = thread.rebuild(state)
            status = versions.handle_clean_copy(
                email, provider, state.filename, current, note=_rebuilt_note(landed),
                still_open=[q.question for q in state.open_questions])
        elif asks("compare", intake.wants_comparison):
            status = versions.handle_comparison(email, provider, pair=pair,
                                                previous=previous)
        elif asks("clean_copy", intake.wants_clean_copy):
            att = picked()
            status = versions.handle_clean_copy(email, provider, att.filename, att.content)
        else:
            att = picked()
            status = versions.handle_repair(email, provider, att.filename, att.content)
        record(job_id, email.message_id, "replied", email.from_address, {"kind": status})
    except (intake.Rejection, extract.Unreadable) as e:
        provider.send(reply.rejection(email, str(e)))
        record(job_id, email.message_id, "rejected", email.from_address,
               {"reason": str(e)})
    except Exception as e:  # noqa: BLE001
        log.error("job %s failed: %s", job_id, traceback.format_exc())
        record(job_id, email.message_id, "failed", email.from_address, {"error": str(e)})
        provider.send(reply.rejection(
            email,
            "Something went wrong on my side while working on that. Nothing was "
            "changed and nothing was sent anywhere else.",
        ))


class _InThread:
    """A provider whose every reply is registered against one conversation.

    Only the first review used to alias the message it sent. Everything sent
    later in a conversation -- the result of an instruction, an undo, an
    apology -- went out unregistered, so a reply to it could only be matched by
    a provider thread id. AgentMail supplies one; Postmark and a replayed .eml
    do not, and there the third message of any conversation got "I could not
    find a document to review".
    """

    def __init__(self, provider, thread_key: str) -> None:
        self._provider = provider
        self._thread_key = thread_key
        self.sent = False

    def __getattr__(self, name):
        return getattr(self._provider, name)

    def send(self, out):
        try:
            sent_id = self._provider.send(out)
        except Exception:
            if not out.attachments:
                raise
            # Almost always size: the provider refuses what it will not carry.
            # The work is done and recorded, so send the words without the file
            # rather than failing a change that has already been made.
            log.exception("follow-up send failed; retrying without the attachment")
            out.attachments = []
            out.text_body = (
                "I could not attach the document this time (it was too large to "
                "send), so this is what I did:\n\n" + out.text_body
            )
            out.html_body = reply.text_as_html(out.text_body)
            sent_id = self._provider.send(out)
        self.sent = True
        try:
            thread.add_alias(self._thread_key, sent_id)
        except Exception:
            log.exception("could not alias an outbound message")
        return sent_id


def _followup_guarded(job_id: str, email: InboundEmail, provider,
                      state: thread.ThreadState, plan: triage.Plan | None = None) -> None:
    """A reply about a document we hold, by the rules or by a triage plan,
    with one guard around it."""
    in_thread = _InThread(provider, state.id)
    try:
        _handle_followup(job_id, email, in_thread, state, plan)
    except Exception:
        # Never re-raised. A follow-up changes state before it replies (an
        # undo, an answer, a rejection counted towards suppression), so a
        # retry from the queue did all of it twice, and after three retries
        # the lawyer heard nothing at all.
        log.exception("follow-up %s failed", email.message_id)
        record(job_id, email.message_id, "failed", email.from_address,
               {"kind": "followup"})
        if not in_thread.sent:
            provider.send(reply.rejection(
                email,
                "Something went wrong on my side with that reply. Nothing was "
                "sent to anyone else. Send it again and I will pick it up.",
            ))


def _handle_followup(job_id: str, email: InboundEmail, provider,
                     state: thread.ThreadState, plan: triage.Plan | None = None) -> None:
    """Act on a reply about a document we already hold. With a triage plan
    (TRIAGE=model), as the plan says; otherwise as the rules read it."""
    if not isinstance(provider, _InThread):
        provider = _InThread(provider, state.id)
    # And the lawyer's own message, so a second reply in the same chain that
    # quotes this one as its parent resolves too.
    try:
        thread.add_alias(state.id, email.message_id, email.thread_id)
    except Exception:
        log.exception("could not alias an inbound follow-up")
    if plan is not None and _followup_by_plan(job_id, email, provider, state, plan):
        return

    # "30; 4; clean copy": several things this module does on its own, asked
    # for in one message. Before the clean-copy check, which on "30" and
    # "clean copy" on two lines cleaned the document and dropped the answer.
    plan = followup.plan_reply(state, email)
    if plan is not None and (plan.answers or plan.undo is not None
                             or plan.dismiss is not None):
        _carry_out_plan(job_id, email, provider, state, plan)
        return

    if intake.wants_signature_pack(email) or intake.wants_calendar(email) or (
            intake.wants_renumber(email) and not intake.wants_clean_copy(email)):
        _handle_version_request(job_id, email, provider, state)
        return

    if (plan is not None and plan.clean) or intake.wants_clean_copy(email):
        besides = followup.besides_clean_copy(email) if plan is None else ""
        if besides:
            # A change and a clean copy in one message. The change needs the
            # agent, and what the agent wrote is for the lawyer to read before
            # it is accepted, so the change is made and the clean copy offered
            # rather than made unseen; the change is never dropped.
            _carry_out_instruction(job_id, email, provider, state, besides, clean_later=True)
            return
        _handle_version_request(job_id, email, provider, state)
        return

    kind, payload = followup.classify(state, email)
    log.info("follow-up on %s classified as %s", state.filename, kind)

    if kind == "ack":
        # "Thanks." "All accepted." Nothing is owed, and a reply would teach
        # the lawyer that every message costs them another.
        record(job_id, email.message_id, "acknowledged", email.from_address, {})
        return

    if kind == "undo":
        match = payload["match"]
        ids, described = match.ids, match.described
        if not ids:
            text = (followup.undo_choices(state, described) if state.active_changes
                    else "Nothing to undo: none of my changes are left, so the "
                         "document is back to your original.")
            provider.send(followup.compose_note(email, text))
            record(job_id, email.message_id, "replied", email.from_address,
                   {"kind": "undo-asked"})
            return
        thread.undo(state.id, ids)
        try:
            memory.record_rejections(
                job_id, identity.resolve_user(email), state.matter_id,
                [c for c in state.changes if c.id in set(ids)],
            )
        except Exception:
            log.exception("could not record the rejected changes")
        state = thread.load(state.id)
        content, landed, _ = thread.rebuild(state)
        remain = len(landed)
        summary = (
            f"I reversed {described}. "
            + (f"{remain} change{'s' if remain != 1 else ''} of mine "
               f"{'remain' if remain != 1 else 'remains'} in the attached copy."
               if landed else "Nothing of mine remains, so this is your original.")
        )
        provider.send(followup.compose_update(
            email, state, content if landed else None, summary,
            [c.as_finding() for c in landed], numbers=[state.number(c) for c in landed],
        ))
        record(job_id, email.message_id, "replied", email.from_address, {"kind": "undo"})
        return

    if kind == "answer":
        question, value = payload["question"], payload["value"]
        thread.answer(state.id, question.id, value)
        change = followup.apply_answer(state, question, value)
        if change:
            thread.record_changes(state.id, state.round, [change], origin="instruction")
        state = thread.load(state.id)
        content, landed, _ = thread.rebuild(state)

        # Only claim it if it reached the document. An answer whose anchor
        # occurs twice is refused by the writer, and saying "I have made that
        # change" over an unchanged file is the most damaging sentence in the
        # product: the lawyer stops looking.
        made = change is not None and any(
            c.anchor == change.anchor and c.replacement == change.suggested_text
            for c in landed
        )
        if made:
            summary = f"Noted: {value}. I have made that change."
        elif change is not None:
            summary = (
                f"Noted: {value}. I could not place that change safely, so the "
                "document is unchanged and it is yours to make."
            )
        else:
            summary = f"Noted: {value}."
        provider.send(followup.compose_update(
            email, state, content if landed else None, summary,
            [c.as_finding() for c in landed], numbers=[state.number(c) for c in landed],
        ))
        record(job_id, email.message_id, "replied", email.from_address, {"kind": "answer"})
        return

    _followup_text(job_id, email, provider, state, payload.get("text", ""))


def _followup_by_plan(job_id: str, email: InboundEmail, provider,
                      state: thread.ThreadState, plan: triage.Plan) -> bool:
    """A reply on a conversation, as a triage plan read it, carried out by
    the rules' own steps: answers, undo and dismissals through
    `_carry_out_plan`, requests about the document through
    `_handle_version_request`, words to act on through `_followup_text`.
    False when the plan names nothing here, and the rules read it."""
    names = set(plan.names)
    words = router.strip_reply(email.body)
    if names <= {"ack", "ignore"}:
        record(job_id, email.message_id, "acknowledged", email.from_address, {})
        return True
    if "negotiation_status" in names:
        answered = negotiation.answer(email, state.id)
        if answered is not None:
            provider.send(answered)
            record(job_id, email.message_id, "replied", email.from_address,
                   {"kind": "negotiation_status"})
            return True

    reply_plan = followup.ReplyPlan(answers=[])
    open_questions = {q.id: q for q in state.open_questions}
    for a in plan.answers:
        if a.question_id in open_questions:
            reply_plan.answers.append((open_questions.pop(a.question_id), a.value))
    if "undo" in names:
        reply_plan.undo = followup.undo_by_numbers(state, plan.undo, plan.undo_all)
    letters = [x.strip().upper() for x in plan.dismiss if x.strip()]
    if "dismiss" in names and letters:
        reply_plan.dismiss = followup.resolve_dismiss(state, letters)
    clean = "clean_copy" in names
    text = plan.instruction.strip() or plan.question.strip() or words
    acts = names & {"instruction", "question", "stop_flagging", "flag_again"}

    if reply_plan.answers or reply_plan.undo is not None or reply_plan.dismiss is not None:
        reply_plan.clean = clean and not acts
        _carry_out_plan(job_id, email, provider, state, reply_plan)
        if acts and not (reply_plan.undo is not None and not reply_plan.undo.ids):
            # "Undo 5 and tighten the indemnity": the rest, once the ledger
            # has moved, as its own reply.
            _followup_text(job_id, email, provider, thread.load(state.id), text)
        return True
    version = next((n for n in ("sig_pages", "deadlines_calendar", "renumber")
                    if n in names), None)
    if version is not None:
        _handle_version_request(job_id, email, provider, state, want=version)
        return True
    if clean:
        if "instruction" in names and plan.instruction.strip():
            _carry_out_instruction(job_id, email, provider, state, plan.instruction,
                                   clean_later=True)
        else:
            _handle_version_request(job_id, email, provider, state, want="clean_copy")
        return True
    if names & {"stop_flagging", "flag_again"}:
        # The memory's own reading of the lawyer's words names the point.
        _followup_text(job_id, email, provider, state, words)
        return True
    if acts:
        _followup_text(job_id, email, provider, state, text)
        return True
    return False


def _followup_text(job_id: str, email: InboundEmail, provider,
                   state: thread.ThreadState, text: str) -> None:
    """A reply that is words to act on rather than an answer, an undo or a
    dismissal: "stop flagging X", "flag X again", or an instruction for the
    agent. The rules path and a triage plan (TRIAGE=model) both end here."""
    # "Stop flagging the Oxford comma." Remembered before anything else, so it
    # holds for every later review even if the rest of this reply fails.
    user = identity.resolve_user(email)
    asked = memory.suppression_request(text)
    if asked:
        # Alone, it is confirmed in the reply below, which counts as telling
        # them; alongside an instruction it is not, and the next review says it.
        only = memory.is_only_a_suppression(text)
        matter = state.matter_id
        walled_out = memory.suppression_home(asked, user, matter) is None
        try:
            memory.remember_suppression(user, asked, email.message_id, announced=only,
                                        matter_id=matter)
        except Exception:
            log.exception("could not store a suppression request")
        if only and walled_out:
            # It names something from a client's document and no matter is
            # known, so there is nowhere walled to keep it. Saying so beats
            # a promise the next client's review would break or keep wrongly.
            provider.send(_confirmation(
                email,
                "I can only keep that for a matter: it names something from this "
                "client's document, and I don't know which matter this is, so I "
                "can't keep it apart from your other clients' work. Put the matter "
                "number in the subject and say it again, or name the drafting point "
                "itself (\"stop flagging the Oxford comma\").",
                "noted",
            ))
            record(job_id, email.message_id, "replied", email.from_address,
                   {"kind": "suppression", "kept": False})
            return
        if only:
            # Said back, so the lawyer sees it learn. This used to fall
            # through to the instruction agent, which read "stop flagging
            # passive voice" as an edit to make and tried to make it.
            subject = memory.suppression_subject(text) or "that"
            provider.send(_confirmation(
                email,
                f"Noted. I will not raise {subject} with you again, from your next "
                f'review on. Say "flag {subject} again" if you change your mind.',
                "noted",
            ))
            record(job_id, email.message_id, "replied", email.from_address,
                   {"kind": "suppression"})
            return

    lifted = memory.unsuppression_request(text)
    if lifted is not None:
        try:
            count = memory.forget_suppression(user, lifted, state.matter_id)
        except Exception:
            log.exception("could not lift a suppression")
            count = 0
        provider.send(_confirmation(
            email,
            (f"Done. I will raise {lifted} again from your next review on." if count
             else f"I had nothing stored about {lifted}, so there was nothing to lift. "
                  "It will be raised as normal."),
            "noted",
        ))
        record(job_id, email.message_id, "replied", email.from_address,
               {"kind": "unsuppression"})
        return

    # A free-form instruction: "tighten the indemnity", "add a force majeure
    # clause after 7", "call them the Purchaser throughout". The agent gets the
    # thread summary too, so it knows what it has already done and what it was
    # told to undo.
    _carry_out_instruction(job_id, email, provider, state, text)


def _rebuilt_note(landed: list) -> str:
    """Which copy a clean copy was made from, when it is ours rather than theirs."""
    if not landed:
        return ""
    return (
        f"This is your original with my {len(landed)} change"
        f"{'s' if len(landed) != 1 else ''} accepted. If you accepted, "
        "rejected or edited anything in Word, attach your copy and I "
        "will clean that one instead."
    )


def _carry_out_plan(job_id: str, email: InboundEmail, provider,
                    state: thread.ThreadState, plan: followup.ReplyPlan) -> None:
    """Several requests from one reply, done in one pass and answered once:
    the undo, then the answers, then the clean copy of the result.

    The undo keeps its one rule. If it names nothing for certain, nothing in
    the reply is done and the lawyer is asked, exactly as a lone "undo" would
    be; doing the answers and a clean copy around an undo we could not place
    would hand them a document that is not the one they asked for.
    """
    if plan.undo is not None and not plan.undo.ids:
        if state.active_changes:
            text = (followup.undo_choices(state, plan.undo.described)
                    + "\n\nI have not done the rest of your reply either. Send it "
                      "again with the number.")
        else:
            text = ("Nothing to undo: none of my changes are left, so the document "
                    "is back to your original. I have not done the rest of your "
                    "reply either. Send it again without the undo.")
        provider.send(followup.compose_note(email, text))
        record(job_id, email.message_id, "replied", email.from_address,
               {"kind": "combined-asked"})
        return
    if plan.dismiss is not None and plan.dismiss.ask:
        # The same rule as an undo: a letter that names nothing stops it all.
        text = followup.dismiss_choices(state, plan.dismiss.described)
        if plan.parts > 1:
            text += "\n\nI have not done the rest of your reply either. Send it again."
        provider.send(followup.compose_note(email, text))
        record(job_id, email.message_id, "replied", email.from_address,
               {"kind": "dismiss-asked"})
        return

    said: list[str] = []
    if plan.undo is not None:
        ids = set(plan.undo.ids)
        thread.undo(state.id, list(ids))
        try:
            memory.record_rejections(job_id, identity.resolve_user(email), state.matter_id,
                                     [c for c in state.changes if c.id in ids])
        except Exception:
            log.exception("could not record the rejected changes")
        said.append(f"I reversed {plan.undo.described}.")

    answered: list[tuple[str, Finding | None]] = []
    for question, value in plan.answers:
        thread.answer(state.id, question.id, value)
        change = followup.apply_answer(state, question, value)
        if change:
            thread.record_changes(state.id, state.round, [change], origin="instruction")
        answered.append((value, change))

    if plan.dismiss is not None:
        said.append(followup.dismissed_said(state, plan.dismiss))
        _dismiss(job_id, email, state, plan.dismiss)
        if plan.parts == 1:
            # "Noted: B is fine." One line, nothing attached, nothing rebuilt.
            provider.send(followup.compose_note(email, said[-1]))
            record(job_id, email.message_id, "replied", email.from_address,
                   {"kind": "dismiss"})
            return

    state = thread.load(state.id)
    content, landed, _ = thread.rebuild(state)
    if answered:
        said.append(_answers_said(answered, landed))

    if plan.clean:
        note = " ".join(said + [_rebuilt_note(landed)]).strip()
        try:
            versions.handle_clean_copy(
                email, provider, state.filename, content, note=note,
                still_open=[q.question for q in state.open_questions])
        except intake.Rejection as e:
            provider.send(reply.rejection(email, str(e)))
        record(job_id, email.message_id, "replied", email.from_address,
               {"kind": "combined", "clean": True})
        return

    provider.send(followup.compose_update(
        email, state, content if landed else None, " ".join(said),
        [c.as_finding() for c in landed], numbers=[state.number(c) for c in landed],
    ))
    record(job_id, email.message_id, "replied", email.from_address, {"kind": "combined"})


def _dismiss(job_id: str, email: InboundEmail, state: thread.ThreadState,
             match: followup.DismissMatch) -> None:
    """Dismissed on the thread, and each one counted towards not raising that
    kind of point for this lawyer again."""
    thread.dismiss(state.id, match.ids)
    try:
        memory.record_dismissals(job_id, identity.resolve_user(email), state.matter_id,
                                 [f for f in state.flagged if f.id in set(match.ids)])
    except Exception:
        log.exception("could not record the dismissed findings")


def _answers_said(answered: list[tuple[str, Finding | None]], landed: list) -> str:
    """ "Noted: 30 and 4. I have made those changes." Only what reached the
    document is claimed; an answer the writer could not place is named."""
    placed = {(c.anchor, c.replacement) for c in landed}
    values = [v for v, _ in answered]
    heard = (", ".join(values[:-1]) + " and " + values[-1]) if len(values) > 1 else values[0]
    made = [v for v, ch in answered if ch and (ch.anchor, ch.suggested_text) in placed]
    missed = [v for v, ch in answered if ch and (ch.anchor, ch.suggested_text) not in placed]
    out = f"Noted: {heard}."
    if made and not missed:
        out += " I have made that change." if len(made) == 1 else " I have made those changes."
    elif made:
        out += f" I have made the change for {', '.join(made)}."
    for value in missed:
        out += (f" I could not place the change for {value} safely, so that one "
                "is yours to make.")
    return out


# A confirmation verdict this agent appended on an earlier round. Stripped
# before composing a new subject so three rounds cannot stack into
# "Re: SPA - disconnected - stopped - Looks good"; reply._subject does the same
# job for the review verdicts, and the two compose.
_PRIOR_CONFIRMATION = re.compile(
    r"\s+-\s+(?:disconnected|stopped|nothing to connect)$", re.IGNORECASE
)


def _confirmation(inbound: InboundEmail, body: str, verdict: str) -> OutboundEmail:
    """A reply that confirms something we did, rather than a review that failed.

    These used to go out through `reply.rejection`, which hard-codes the subject
    verdict "Could not review" and a footer promising to "pick it up from
    there". A lawyer who typed "revoke" got back "Re: Falcon NDA - Could not
    review", which at a glance says their document was not looked at, and an
    invitation to reply that had nothing to pick up.
    """
    base = _PRIOR_CONFIRMATION.sub("", inbound.subject or "")
    return OutboundEmail(
        to=[inbound.from_address],
        subject=reply._subject(base, verdict),
        text_body=body.rstrip() + "\n",
        # Rendered by the same composer as every other reply, so this arrives
        # looking like the rest of the conversation in an HTML client.
        html_body=reply._as_html([("summary", body)]),
        in_reply_to=inbound.message_id,
        thread_id=inbound.thread_id,
    )


def _without_the_model(state: thread.ThreadState) -> str:
    """The reply to an instruction when there is no model: what I could not
    read, and the replies that work without one, from this conversation."""
    lines = [("I can't read that one: free-form instructions need the review "
              "model, which isn't switched on. These work now:")]
    for q in state.open_questions[:3]:
        options = f" ({' or '.join(q.options)})" if q.options else ""
        lines.append(f"  - an answer to: {q.question}{options}")
    active = state.active_changes
    if active:
        lines.append(f'  - "undo {state.number(active[0])}" to reverse a change')
    lines.append('  - "clean copy", "renumber", "sig pages", or "help" for the rest')
    return "\n".join(lines)


def _carry_out_instruction(job_id: str, email: InboundEmail, provider,
                           state: thread.ThreadState, text: str,
                           clean_later: bool = False) -> None:
    """Do what the lawyer asked, and say what we think of it once."""
    excluded = policy.ai_forbidden()
    if excluded:
        # Carrying out a free-form instruction is the model's work. Say so
        # rather than try: the deterministic replies (answers, undo, clean
        # copy) never reach here.
        provider.send(followup.compose_note(
            email, policy.no_ai_line(excluded) + " I can't carry out a free-form "
            "instruction for this client. Answers to my questions, undo, clean copy, "
            "compare, renumber and sig pages still work."))
        record(job_id, email.message_id, "replied", email.from_address,
               {"kind": "instruction-no-ai"})
        return
    user = identity.resolve_user(email)

    # Act on the document as it currently stands, so "tighten that further"
    # means further than the change already made, not further than the original.
    current, _, _ = thread.rebuild(state)
    att = Attachment(
        filename=state.filename,
        content_type=DOCX_TYPE,
        size_bytes=len(current),
        content=current,
    )

    try:
        doc = extract.extract(att)
        outcome = instruct.carry_out(
            doc,
            instruction=text,
            user_address=user,
            matter_id=state.matter_id,
            history=state.summary(),
            memory_stores=memory.stores_for(user, state.matter_id),
            original=current,
        )
    except extract.Unreadable as e:
        provider.send(reply.rejection(email, str(e)))
        return
    except managed.NotConfigured:
        # No model to hand it to. "Something went wrong ... try me again" was
        # false twice over: nothing went wrong, and trying again cannot work.
        # Say what did not happen and what will.
        provider.send(followup.compose_note(email, _without_the_model(state)))
        record(job_id, email.message_id, "replied", email.from_address,
               {"kind": "instruction-unavailable"})
        return
    except Exception:
        log.exception("could not carry out the instruction")
        provider.send(reply.rejection(
            email,
            "Something went wrong while I was making that change. Nothing in the "
            "document was altered, so you have not lost anything. Try me again.",
        ))
        # The shape of what was read as an instruction, never its words: a
        # one-word answer that lands here means the reply was not stripped
        # or matched the way the tests expect.
        record(job_id, email.message_id, "failed", email.from_address,
               {"kind": "instruction", "words": len(text.split()),
                "lines": text.count("\n") + 1, "quoted": " wrote:" in text,
                "html_only": not (email.text_body or "").strip(),
                "open_questions": len(state.open_questions)})
        return

    try:
        _apply_and_reply(job_id, email, provider, state, outcome, clean_later)
    except Exception:
        # Everything after the model call used to sit outside any guard, so a
        # malformed field in the model's result raised AFTER the ledger write
        # and the lawyer got no email at all, while the change was persisted and
        # the concern was lost. Failing here must still say something true.
        log.exception("could not apply the instruction result")
        provider.send(reply.rejection(
            email,
            "I worked out what to change but could not finish applying it. "
            "Reply and I will try again; nothing has been sent anywhere.",
        ))
        record(job_id, email.message_id, "failed", email.from_address,
               {"kind": "instruction-apply"})


def _apply_and_reply(job_id: str, email: InboundEmail, provider,
                     state: thread.ThreadState, outcome, clean_later: bool = False) -> None:
    """Record what the agent decided, rebuild, and reply."""

    findings = outcome.as_findings()
    recorded: list[int] = []
    if findings:
        # Through the ledger like everything else, so "undo that" works on an
        # instruction-driven change exactly as it does on a review finding.
        recorded = thread.record_changes(state.id, state.round, findings,
                                         origin="instruction")
        state = thread.load(state.id)

    content, landed, notes = thread.rebuild(state)
    landed_pairs = {(c.anchor, c.replacement) for c in landed}
    unlanded = [f for f in findings
                if (f.anchor, f.suggested_text) not in landed_pairs]

    # A change the writer refused must not stay active in the ledger. Left
    # active it is retried on every later rebuild, and once the text it
    # collided with is undone it resolves and applies silently, to a clause
    # nobody discussed. The lawyer undid one thing and got a new edit.
    if unlanded and recorded:
        by_pair = {(f.anchor, f.suggested_text): row
                   for f, row in zip(findings, recorded, strict=False)}
        stale = [by_pair[(f.anchor, f.suggested_text)] for f in unlanded
                 if (f.anchor, f.suggested_text) in by_pair]
        if thread.deactivate_unlanded(state.id, stale):
            state = thread.load(state.id)

    provider.send(_instruction_reply(
        email, state, outcome, content if landed else None, landed, unlanded, notes,
        clean_later=clean_later,
    ))
    record(job_id, email.message_id, "replied", email.from_address,
           {"kind": "instruction", "changes": len(findings)})


def _instruction_reply(email: InboundEmail, state: thread.ThreadState, outcome,
                       content: bytes | None, landed: list, unlanded: list,
                       notes: list[str], clean_later: bool = False):
    """The reply after carrying out an instruction.

    Ordered by what the lawyer needs to act on: what it understood, anything it
    thinks is a mistake, then what it did. The concern comes before the change
    list deliberately -- it is the one thing they might want to reverse, and
    burying it under a list of edits would be a way of technically disclosing it.
    """
    # The verdict first, as in every other reply. It used to live only in the
    # subject line, which no longer carries one (reply._subject), so without
    # this an instruction reply was the one email that did not open by saying
    # what happened.
    answer = getattr(outcome, "answer", "") or ""
    # A question gets its answer as the first line. It used to get "Nothing
    # to change", because the only thing the agent could report was edits.
    verdict = (answer if answer and not landed
               else _instruction_verdict(outcome, landed).rstrip(".") + ".")
    lines = [verdict, ""]
    if answer and landed:
        lines += [answer, ""]

    concerns = [t for t in (_concern_text(c) for c in outcome.concerns or []) if t]
    if concerns:
        lines += [f"  - {t.rstrip('.')}." for t in concerns]
        lines.append("")
        if not landed:
            lines += ["So I have not changed anything.", ""]

    if outcome.understood and landed:
        lines += [outcome.understood.strip(), ""]

    # Only what this round did. Every earlier change used to be listed again
    # under each reply, so the one edit just made was hard to find.
    asked = {(f.anchor, f.suggested_text) for f in outcome.as_findings()}
    new = [c for c in landed if (c.anchor, c.replacement) in asked]
    earlier = [c for c in landed if (c.anchor, c.replacement) not in asked]
    if new:
        lines.append("Tracked in the attached copy:")
        for change in new:
            mark = "new wording: " if change.category == "drafting" else ""
            lines.append(f"  {state.number(change)}. {mark}{change.title.rstrip('.')}.")
            # The title is written by the model. Quoting the text underneath it
            # is what makes the list a manifest rather than a claim: a change
            # labelled "Consistent use of shall" that actually flips an
            # indemnity reads as exactly what it is.
            for line in _change_evidence(change):
                lines.append(f"      {line}")
        lines.append("")
    if earlier:
        numbers = ", ".join(str(state.number(c)) for c in earlier)
        plural = len(earlier) != 1
        lines += [(f"Your earlier change{'s' if plural else ''} ({numbers}) "
                   f"{'are' if plural else 'is'} still in the copy."), ""]

    if unlanded:
        lines.append("I could not place these, so they are yours to make:")
        lines += [f"  - {f.title.rstrip('.')}." for f in unlanded]
        lines.append("")

    if outcome.questions:
        lines.append("I need you to tell me:")
        lines += [f"  - {q}" for q in outcome.questions]
        lines.append("")

    produced = getattr(outcome, "artefacts", []) or []
    if produced:
        lines.append(
            f"I have also attached {len(produced)} file"
            f"{'s' if len(produced) != 1 else ''} I made from the document:"
        )
        lines += [f"  - {name}" for name, _ in produced]
        lines.append("")

    if outcome.declined:
        for item in outcome.declined:
            lines.append(f"I have not done this: {item.get('what', '')}. "
                         f"{item.get('why', '')}".strip())
        lines.append("")

    lines += list(notes)
    if clean_later:
        # They asked for the clean copy too. It waits for them to read what
        # the agent wrote; accepting a model's edit unseen is not ours to do.
        lines.append('You asked for a clean copy as well. It comes once you have '
                     'checked this: reply "clean copy", or "undo" and the number.'
                     if landed else 'Reply "clean copy" if you still want one.')
    elif landed:
        lines.append('Reply "clean copy" to accept, or "undo" and the number.')

    attachments = []
    if content and landed:
        attachments.append(Attachment(
            filename=reply._redline_name(state.filename),
            content_type=DOCX_TYPE,
            size_bytes=len(content),
            content=content,
        ))
    for name, data in produced:
        attachments.append(Attachment(
            filename=name,
            content_type=_content_type_for(name),
            size_bytes=len(data),
            content=data,
        ))

    text = "\n".join(lines).rstrip() + "\n"
    return OutboundEmail(
        to=[email.from_address],
        subject=reply._subject(email.subject, _instruction_verdict(outcome, landed)),
        text_body=text,
        html_body=reply.text_as_html(text),
        in_reply_to=email.message_id,
        thread_id=email.thread_id,
        attachments=attachments,
    )


_CONTENT_TYPES = {
    "docx": DOCX_TYPE,
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "pdf": "application/pdf",
    "csv": "text/csv",
    "txt": "text/plain",
    "md": "text/markdown",
}


def _content_type_for(filename: str) -> str:
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return _CONTENT_TYPES.get(extension, "application/octet-stream")


def _change_evidence(change, limit: int = 150) -> list[str]:
    """The words that actually changed, under the title that claims to describe
    them.

    Decision 17 says the email is a complete honest manifest of what was
    written. A model-authored title alone is not that: it is the model's
    account of its own edit, and an edit steered by a poisoned document arrives
    with an innocuous title by construction. Quoting the text removes the need
    to trust the label.
    """
    anchor = " ".join((change.anchor or "").split())
    replacement = " ".join((change.replacement or "").split())

    if change.edit_kind == "insert_after":
        return [f"added: “{_clip(replacement, limit)}”"] if replacement else []
    if not anchor or not replacement:
        return []

    before, after = _differing_span(anchor, replacement)
    if before and after and max(len(before), len(after)) <= limit:
        return [f"“{before}” → “{after}”"]
    return [f"was: “{_clip(anchor, limit)}”", f"now: “{_clip(replacement, limit)}”"]


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _differing_span(before: str, after: str) -> tuple[str, str]:
    """Trim the common prefix and suffix so only the change is quoted."""
    start = 0
    while start < min(len(before), len(after)) and before[start] == after[start]:
        start += 1
    end = 0
    while (
        end < min(len(before), len(after)) - start
        and before[len(before) - 1 - end] == after[len(after) - 1 - end]
    ):
        end += 1
    # Widen to token boundaries on both sides. Trimming to the raw common
    # prefix and suffix cuts mid-token, so "twelve (12) months" against
    # "twenty-four (24) months" quotes «twelve (12» and loses the bracket.
    while start > 0 and not before[start - 1].isspace():
        start -= 1
    def _closes(text: str, cut: int) -> int:
        while cut > 0 and cut < len(text) and not text[len(text) - cut].isspace():
            cut -= 1
        return cut

    end = min(_closes(before, end), _closes(after, end))
    return before[start: len(before) - end].strip(), after[start: len(after) - end].strip()


def _concern_text(concern) -> str:
    """Whatever the model put in a concern, rendered as a sentence.

    The tool takes concerns as free-form dicts, so the keys are not guaranteed
    to be `about` and `why`. Reading only those produced an empty bullet under
    a heading that still announced a concern, so the lawyer was told something
    was worth saying and then shown nothing. Anything with words in it is
    better than that.
    """
    if isinstance(concern, str):
        return concern.strip()
    if not isinstance(concern, dict):
        return str(concern).strip()
    about = str(concern.get("about") or "").strip()
    why = str(concern.get("why") or "").strip()
    if about or why:
        return f"{about}. {why}".strip(". ").strip() if about else why
    # Unexpected keys: print what is there rather than nothing.
    parts = [str(v).strip() for v in concern.values() if str(v).strip()]
    return ". ".join(parts)


def _instruction_verdict(outcome, landed: list) -> str:
    if outcome.questions and not landed:
        return "One question before I make this change"
    if outcome.concerns:
        return ("Done, but check this first" if landed
                else "I have not made this change, for this reason")
    if landed:
        return "Done"
    if getattr(outcome, "artefacts", None):
        n = len(outcome.artefacts)
        return f"Done. {n} file{'s' if n != 1 else ''} attached"
    return "Nothing to change"


def _as_attachment(state: thread.ThreadState) -> Attachment:
    """The document a conversation holds, shaped as if it had just arrived."""
    name = state.filename or "document.docx"
    # The pipeline identifies the format from the bytes (filetype.identify), so
    # the declared type only has to be plausible.
    content_type = reply.DOCX_TYPE if name.lower().endswith(".docx") else "application/octet-stream"
    return Attachment(filename=name, content_type=content_type,
                      size_bytes=len(state.original), content=state.original)


def _first_conversation(user: str, thread_key: str) -> bool:
    try:
        return thread.is_first_conversation(user, thread_key)
    except Exception:
        log.exception("could not tell whether this is a first review")
        return False


def _thread_matter(email: InboundEmail, user: str) -> str | None:
    """The matter of the conversation this reply belongs to, if any."""
    try:
        key = thread.resolve(user, email.thread_id, email.message_id,
                             email.in_reply_to, email.references,
                             tap=identity.tap_token(email))
        return thread.load(key).matter_id if key else None
    except Exception:
        log.exception("could not resolve the thread for a memory reply")
        return None


def _consent_offer(email: InboundEmail) -> str:
    """One paragraph appended to a first review, offering the DMS connection."""
    return (
        "This review looked at the document on its own. If you connect your "
        "document system account, I can also check it against the firm's "
        "precedents and the other drafts on this matter. Reply \"connect\" and "
        "I will send you a one-click link."
    )
