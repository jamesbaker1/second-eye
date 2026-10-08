"""The handler's degradation paths. These matter more than the happy path."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from secondeye import handler
from secondeye.mail.console import ConsoleProvider
from secondeye.models import Attachment, InboundEmail
from tests.conftest import documents

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class Captured(ConsoleProvider):
    def __init__(self):
        self.sent = []

    def send(self, email_out):
        self.sent.append(email_out)
        return "captured"


@pytest.fixture
def captured(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.sqlite3")
    from secondeye.config import settings

    settings.cache_clear()
    provider = Captured()
    monkeypatch.setattr(handler, "get_provider", lambda: provider)
    return provider


def email_with_sample(subject="NDA for Acme") -> InboundEmail:
    raw = Path("samples/simple.docx").read_bytes()
    return InboundEmail(
        message_id=f"m-{subject}",
        from_address="jim@firm.com",
        to=["review@firm.com"],
        subject=subject,
        text_body="Quick look before I send this?",
        attachments=[
            Attachment(filename="Acme NDA.docx", content_type=DOCX,
                       size_bytes=len(raw), content=raw)
        ],
        received_at=datetime.now(UTC),
    )


def test_a_failed_model_call_still_sends_the_mechanical_findings(captured, monkeypatch):
    """The worst outcome would be telling a lawyer nothing is wrong because the
    model call failed, when a placeholder is still in the document."""
    monkeypatch.setattr(
        handler.review, "review", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
    )
    handler.handle(email_with_sample("model down"))

    assert len(captured.sent) == 1
    body = captured.sent[0].text_body
    assert "Don't send yet" in body
    assert "[COUNTERPARTY NAME]" in body
    assert "only the mechanical checks" in body


def test_a_failed_model_call_with_a_clean_document_says_so_honestly(captured, monkeypatch):
    monkeypatch.setattr(
        handler.review, "review", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
    )
    monkeypatch.setattr(handler.checks, "run_all", lambda *a, **k: [])
    monkeypatch.setattr(handler.email_checks, "run_all", lambda *a, **k: [])
    handler.handle(email_with_sample("clean and down"))

    assert "Something went wrong" in captured.sent[0].text_body


def test_no_attachment_gets_a_sentence_not_a_stack_trace(captured):
    handler.handle(
        InboundEmail(
            message_id="m-empty",
            from_address="jim@firm.com",
            subject="help",
            text_body="can you look at this",
            received_at=datetime.now(UTC),
        )
    )
    assert ".docx" in captured.sent[0].text_body


def test_a_duplicate_webhook_delivery_is_not_reviewed_twice(captured, monkeypatch):
    monkeypatch.setattr(
        handler.review, "review", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
    )
    e = email_with_sample("dupe")
    handler.handle(e)
    handler.handle(e)
    assert len(captured.sent) == 1


def test_revoke_disconnects_without_reviewing_anything(captured):
    e = email_with_sample("revoke me")
    e.text_body = "revoke"
    handler.handle(e)
    assert "Disconnected" in captured.sent[0].text_body


# --- mail that must never be answered ------------------------------------

def test_an_out_of_office_reply_is_not_answered(captured):
    """Without this, an auto-responder and this agent talk to each other until
    somebody notices."""
    e = email_with_sample("ooo")
    e.subject = "Automatic reply: NDA for Acme"
    handler.handle(e)
    assert captured.sent == []


def test_a_bounce_is_not_answered(captured):
    e = email_with_sample("bounce")
    e.from_address = "MAILER-DAEMON@firm.com"
    handler.handle(e)
    assert captured.sent == []


def test_an_auto_submitted_header_is_respected(captured):
    e = email_with_sample("autosub")
    e.headers = {"auto-submitted": "auto-replied"}
    handler.handle(e)
    assert captured.sent == []


def test_a_mailing_list_message_is_not_answered(captured):
    e = email_with_sample("list")
    e.headers = {"list-id": "<updates.firm.com>"}
    handler.handle(e)
    assert captured.sent == []


# --- the prefix-matching bug ---------------------------------------------

def test_a_forwarded_mail_beginning_with_revoke_does_not_disconnect(captured, monkeypatch):
    """The old prefix match would silently cut a lawyer off from their
    documents because their sentence happened to start with the word."""
    calls = []
    monkeypatch.setattr(handler.oauth, "revoke", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(
        handler.review, "review", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
    )
    e = email_with_sample("fwd-revoke")
    e.text_body = "Revoke the licence agreement we discussed - does clause 9 allow it?"
    handler.handle(e)
    assert calls == []
    assert "Disconnected" not in captured.sent[0].text_body


def test_a_bare_revoke_still_disconnects(captured, monkeypatch):
    calls = []
    monkeypatch.setattr(handler.oauth, "revoke", lambda *a, **k: calls.append(a))
    e = email_with_sample("bare-revoke")
    e.text_body = "revoke"
    handler.handle(e)
    assert calls
    assert "Disconnected" in captured.sent[0].text_body


def test_a_sentence_beginning_with_connect_does_not_send_a_setup_link(captured, monkeypatch):
    monkeypatch.setattr(
        handler.review, "review", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
    )
    e = email_with_sample("fwd-connect")
    e.text_body = "Connecting you with Sarah who is handling diligence on this."
    handler.handle(e)
    assert "one-time setup" not in captured.sent[0].subject


def test_stop_ends_the_conversation_politely(captured):
    e = email_with_sample("stop")
    e.text_body = "stop"
    handler.handle(e)
    assert "will not reply on this thread again" in captured.sent[0].text_body


# --- authorisation ordering ----------------------------------------------

def test_an_unauthorised_sender_cannot_revoke_a_lawyers_access(captured, monkeypatch):
    """A From header is forgeable. revoke used to run before any authorisation
    check, so one forged email destroyed a partner's document-system grant."""
    monkeypatch.setenv("ALLOWED_SENDERS", "firm.com")
    from secondeye.config import settings

    settings.cache_clear()
    try:
        calls = []
        monkeypatch.setattr(handler.oauth, "revoke", lambda *a, **k: calls.append(a))
        e = email_with_sample("forged")
        e.from_address = "attacker@evil.example"
        e.text_body = "revoke"
        handler.handle(e)
        assert calls == []
        # An outsider is never written to: usually it is opposing counsel
        # hitting reply-all on a message the agent was CC'd on.
        assert captured.sent == []
    finally:
        settings.cache_clear()


def test_an_authorised_sender_can_still_revoke(captured, monkeypatch):
    monkeypatch.setenv("ALLOWED_SENDERS", "firm.com")
    from secondeye.config import settings

    settings.cache_clear()
    try:
        calls = []
        monkeypatch.setattr(handler.oauth, "revoke", lambda *a, **k: calls.append(a))
        e = email_with_sample("legit-revoke")
        e.text_body = "revoke"
        handler.handle(e)
        assert calls
    finally:
        settings.cache_clear()


# --- duplicate delivery ---------------------------------------------------



def test_a_second_delivery_of_the_same_message_is_refused(captured, monkeypatch):
    from secondeye.store import claim

    assert claim("job-1", "msg-x", "jim@firm.com") is True
    assert claim("job-2", "msg-x", "jim@firm.com") is False


# --- confirmations are not failures ---------------------------------------

def test_a_disconnect_confirmation_is_not_titled_could_not_review(captured):
    """Revoking is something that worked. It used to come back through
    reply.rejection, so the lawyer read "Could not review" on a thread they
    were expecting a verdict on."""
    e = email_with_sample("Falcon NDA")
    e.text_body = "revoke"
    handler.handle(e)

    out = captured.sent[0]
    assert "Could not review" not in out.subject
    assert "disconnected" in out.text_body.lower()
    assert "pick it up from there" not in out.text_body
    assert "Disconnected" in out.text_body


def test_a_stop_confirmation_is_not_titled_could_not_review(captured):
    e = email_with_sample("Falcon NDA")
    e.text_body = "stop"
    handler.handle(e)

    assert "Could not review" not in captured.sent[0].subject
    assert "will not reply on this thread again" in captured.sent[0].text_body


def test_a_confirmation_is_rendered_for_html_clients_too(captured):
    """Most lawyers read the HTML part. A confirmation with no html_body
    arrives as bare text in the middle of an otherwise formatted thread."""
    e = email_with_sample("Falcon NDA")
    e.text_body = "revoke"
    handler.handle(e)

    assert "Disconnected" in captured.sent[0].html_body


def test_a_confirmation_verdict_does_not_stack_in_the_subject(captured):
    """Three rounds must not produce "Re: SPA - disconnected - stopped -
    Looks good", which also breaks conversation grouping in every client."""
    e = email_with_sample("round two")
    e.subject = "Re: Falcon NDA - disconnected"
    e.text_body = "stop"
    handler.handle(e)

    assert captured.sent[0].subject == "Re: Falcon NDA"


# --- connect with nothing to connect to -----------------------------------

def test_connect_with_no_document_system_does_not_send_a_dead_link(captured):
    """With the shipped defaults there is no authorize URL and no client id,
    so the "one-click link" resolves to nothing and a pending grant is written
    that can never complete."""
    e = email_with_sample("connect me")
    e.text_body = "connect"
    handler.handle(e)

    out = captured.sent[0]
    assert "one-time setup" not in out.subject
    assert "no document system" in out.text_body.lower()
    assert "authorize" not in out.text_body.lower()


def test_connect_with_a_document_system_configured_still_sends_the_link(
    captured, monkeypatch
):
    monkeypatch.setenv("DMS_PROVIDER", "imanage")
    from secondeye.config import settings
    from secondeye.models import OutboundEmail

    settings.cache_clear()
    try:
        sent_consent = []

        def fake_consent(inbound, reason=""):
            sent_consent.append(inbound)
            return OutboundEmail(
                to=[inbound.from_address], subject="Re: x - one-time setup",
                text_body="link",
            )

        monkeypatch.setattr(handler.consent, "consent_email", fake_consent)
        e = email_with_sample("connect me for real")
        e.text_body = "connect"
        handler.handle(e)

        assert sent_consent
        assert "one-time setup" in captured.sent[0].subject
    finally:
        settings.cache_clear()


# --- a forged From on a state-changing command ----------------------------

FORGED = {
    "authentication-results": (
        "mx.firm.com; spf=fail smtp.mailfrom=evil.example; "
        "dkim=none; dmarc=fail header.from=firm.com"
    )
}


def test_a_revoke_that_failed_dmarc_is_ignored_silently(captured, monkeypatch):
    """The allowlist checks the domain in the From header, which is the one
    field a spoofer controls. Replying would also mail the spoofed lawyer."""
    calls = []
    monkeypatch.setattr(handler.oauth, "revoke", lambda *a, **k: calls.append(a))
    e = email_with_sample("forged revoke")
    e.text_body = "revoke"
    e.headers = dict(FORGED)
    handler.handle(e)

    assert calls == []
    assert captured.sent == []


def test_a_connect_that_failed_dmarc_does_not_mail_a_consent_link(captured):
    e = email_with_sample("forged connect")
    e.text_body = "connect"
    e.headers = dict(FORGED)
    handler.handle(e)

    assert captured.sent == []


def test_a_revoke_that_passed_dmarc_still_disconnects(captured, monkeypatch):
    """Revocation stays one word for the lawyer it belongs to."""
    calls = []
    monkeypatch.setattr(handler.oauth, "revoke", lambda *a, **k: calls.append(a))
    e = email_with_sample("authenticated revoke")
    e.text_body = "revoke"
    e.headers = {
        "authentication-results": "mx.firm.com; spf=pass; dkim=pass; dmarc=pass"
    }
    handler.handle(e)

    assert calls
    assert "Disconnected" in captured.sent[0].text_body


def test_a_document_from_a_domain_that_failed_dmarc_is_not_acted_on(
    captured, monkeypatch
):
    """A counterparty sent a draft with the agent BCC'd can forge the lawyer's
    address. A forged "document" used to be reviewed, stored, and become the
    baseline every later comparison read against. DMARC survives ordinary
    forwarding through DKIM, so an explicit fail is a forgery, not a forward."""
    monkeypatch.setattr(handler.review, "review", stub_review())
    e = email_with_sample("forwarded document")
    e.headers = dict(FORGED)
    handler.handle(e)

    assert captured.sent == []


def test_a_forged_pass_below_the_gateways_fail_does_not_hide_it():
    """The gateway prepends its verdict; a sender can add one underneath. Last
    copy used to win, so the forged "pass" replaced the real "fail"."""
    from secondeye.mail.console import ConsoleProvider
    from secondeye.pipeline import router

    raw = (
        b"Authentication-Results: mx.cloudflare.net; dmarc=fail header.from=firm.com\r\n"
        b"Authentication-Results: x; dmarc=pass\r\n"
        b"From: jim@firm.com\r\nTo: review@firm.com\r\nSubject: t\r\n"
        b"Message-ID: <m@x>\r\n\r\nundo everything\r\n"
    )
    e = ConsoleProvider().parse_eml(raw)
    assert router.authentication_failed(e.headers)


# --- a review that finished but could not be delivered ---------------------

def stub_review(summary="One problem worth fixing."):
    from secondeye.models import Finding, ReviewResult, Severity

    finding = Finding(
        severity=Severity.BLOCKER, category="indemnity",
        title="The indemnity is uncapped",
        explanation="Clause 8 has no cap, so exposure is unlimited.",
        anchor="indemnify and hold harmless", auto_apply=False,
    )

    def _review(doc, mode, instructions, **kwargs):
        return ReviewResult(mode=mode, summary=summary, findings=[finding])

    return _review


class FlakyProvider(Captured):
    """Fails the first `failures` sends, as an oversized or throttled API does."""

    def __init__(self, failures: int):
        super().__init__()
        self.failures = failures
        self.attempts = 0

    def send(self, email_out):
        self.attempts += 1
        if self.attempts <= self.failures:
            raise RuntimeError("422 message too large")
        return super().send(email_out)


def test_a_send_failure_never_claims_the_document_was_not_reviewed(
    captured, monkeypatch
):
    """The recovery path assumed an exception meant the review failed. A 422 on
    the outbound message told a lawyer "nothing here speaks to the substance of
    the document" about a document whose substance had been reviewed, and
    dropped the blocker the agent had just found."""
    flaky = FlakyProvider(failures=2)
    monkeypatch.setattr(handler, "get_provider", lambda: flaky)
    monkeypatch.setattr(handler.review, "review", stub_review())

    e = email_with_sample("undeliverable")
    e.text_body = "Memo only please, no redline."
    handler.handle(e)

    assert flaky.sent, "the findings never reached the lawyer"
    body = flaky.sent[-1].text_body
    assert "only the mechanical checks" not in body
    assert "The indemnity is uncapped" in body


def test_a_failure_after_delivery_does_not_send_a_second_email(captured, monkeypatch):
    """Recording the job or aliasing the thread can fail after the reply is
    out. Telling the lawyer the review failed at that point is simply false."""
    monkeypatch.setattr(handler.review, "review", stub_review())
    real_record = handler.record

    def flaky_record(job_id, message_id, status, sender, payload):
        if status == "replied":
            raise RuntimeError("database is locked")
        return real_record(job_id, message_id, status, sender, payload)

    monkeypatch.setattr(handler, "record", flaky_record)

    e = email_with_sample("bookkeeping fails")
    e.text_body = "Memo only please, no redline."
    handler.handle(e)

    assert len(captured.sent) == 1


def test_a_failed_review_does_not_claim_the_lawyer_asked_for_notes(captured, monkeypatch):
    """The failure path sets memo mode purely to suppress the attachment. It
    must not then tell the lawyer "you asked me not to edit the document",
    which puts words in their mouth about a request they never made."""
    monkeypatch.setattr(
        handler.review, "review",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("model down")),
    )
    handler.handle(email_with_sample("mode-claim"))
    body = captured.sent[0].text_body
    assert "you asked me not to edit" not in body
    assert "could not complete the full review" in body


def test_a_real_memo_request_is_still_explained(captured, monkeypatch):
    """The note is valuable when it is true: a lawyer who asked for notes
    should be told that is what they got."""
    monkeypatch.setattr(
        handler.review, "review",
        lambda doc, mode, instructions, **k: handler.ReviewResult(
            mode=mode, summary="Looked it over.", findings=[]),
    )
    e = email_with_sample("real-memo")
    e.text_body = "Don't edit it, just tell me what's wrong."
    handler.handle(e)
    assert "Notes only, as you asked" in captured.sent[0].text_body


# --- being BCC'd: the coverage habit, and the one unforgivable reply --------
#
# The lawyer's mail rule copies the agent on everything they send. Most of
# everything is not a document, and the one thing the agent must never do on
# any of it is write to the person the lawyer was writing to.


def bcc_to_client(subject, attachments=None) -> InboundEmail:
    """A message to a client that reached us on no visible header."""
    return InboundEmail(
        message_id=f"m-{subject}",
        from_address="jim@firm.com",
        to=["client@acme.com"],
        cc=["partner@firm.com"],
        subject=subject,
        text_body="Thanks both, see you Tuesday.",
        attachments=attachments or [],
        received_at=datetime.now(UTC),
    )


def test_bcc_on_a_message_with_no_document_gets_silence_not_a_could_not_review(captured):
    handler.handle(bcc_to_client("Tuesday"))
    assert captured.sent == []


def test_bcc_on_a_thread_we_reviewed_is_still_not_answered(captured, monkeypatch):
    """The follow-up branch used to catch this and treat "see you Tuesday" as
    an instruction about the document."""
    monkeypatch.setattr(handler.review, "review", stub_review())
    first = email_with_sample("SPA")
    first.attachments[0].filename = "SPA.docx"
    handler.handle(first)
    assert len(captured.sent) == 1

    later = bcc_to_client("Re: SPA")
    later.in_reply_to = first.message_id
    handler.handle(later)
    assert len(captured.sent) == 1


def test_being_written_to_with_no_document_still_gets_an_answer(captured):
    """Silence is for being an observer. Someone who put us on the To line and
    forgot the attachment should be told."""
    handler.handle(InboundEmail(
        message_id="m-forgot", from_address="jim@firm.com",
        to=["review@example.com"], subject="NDA", text_body="Quick look?",
        received_at=datetime.now(UTC),
    ))
    assert len(captured.sent) == 1
    assert ".docx" in captured.sent[0].text_body


def test_a_reply_about_a_sent_document_goes_to_the_lawyer_and_nobody_else(captured, monkeypatch):
    monkeypatch.setattr(handler.review, "review", stub_review())
    e = bcc_to_client("Executed SPA", attachments=email_with_sample().attachments)
    handler.handle(e)

    [out] = captured.sent
    assert out.to == ["jim@firm.com"]
    assert out.cc == []
    assert "Already sent" in out.text_body


# --- "stop" is remembered, not just promised ---------------------------------


def test_stop_silences_the_thread_until_a_new_document_arrives(captured, monkeypatch):
    monkeypatch.setattr(handler.review, "review", stub_review())
    first = email_with_sample("SPA")
    handler.handle(first)
    assert len(captured.sent) == 1
    reviewed = captured.sent[0]

    stop = InboundEmail(
        message_id="m-stop", from_address="jim@firm.com", to=["review@example.com"],
        subject="Re: SPA", text_body="stop", in_reply_to=first.message_id,
        references=[first.message_id], received_at=datetime.now(UTC),
    )
    handler.handle(stop)
    assert len(captured.sent) == 2
    assert "will not reply on this thread again" in captured.sent[1].text_body

    # A follow-up that would otherwise be an instruction gets nothing.
    later = InboundEmail(
        message_id="m-later", from_address="jim@firm.com", to=["review@example.com"],
        subject="Re: SPA", text_body="undo everything", in_reply_to="m-stop",
        references=[first.message_id, "m-stop"], received_at=datetime.now(UTC),
    )
    handler.handle(later)
    assert len(captured.sent) == 2, "a muted thread was answered"
    assert reviewed is captured.sent[0]

    # A new document on the same thread is a new request, and is reviewed.
    again = email_with_sample("SPA v2")
    again.subject = "Re: SPA"
    again.in_reply_to = "m-later"
    again.references = [first.message_id, "m-stop", "m-later"]
    handler.handle(again)
    assert len(captured.sent) == 3


def test_stop_before_any_review_is_still_remembered(captured):
    """A reply saying "stop" to a rejection: there is no thread state, and the
    promise still has to hold."""
    first = InboundEmail(
        message_id="m-empty", from_address="jim@firm.com", to=["review@example.com"],
        subject="help", text_body="can you look at this", received_at=datetime.now(UTC),
    )
    handler.handle(first)
    assert len(captured.sent) == 1  # "attach a .docx"

    handler.handle(InboundEmail(
        message_id="m-stop2", from_address="jim@firm.com", to=["review@example.com"],
        subject="Re: help", text_body="stop", in_reply_to="m-empty",
        references=["m-empty"], received_at=datetime.now(UTC),
    ))
    assert len(captured.sent) == 2

    handler.handle(InboundEmail(
        message_id="m-again", from_address="jim@firm.com", to=["review@example.com"],
        subject="Re: help", text_body="hello?", in_reply_to="m-stop2",
        references=["m-empty", "m-stop2"], received_at=datetime.now(UTC),
    ))
    assert len(captured.sent) == 2


# --- closing the loop costs nothing --------------------------------------------


def followup_to(first, text, message_id="m-follow") -> InboundEmail:
    return InboundEmail(
        message_id=message_id, from_address="jim@firm.com", to=["review@example.com"],
        subject="Re: " + first.subject, text_body=text, in_reply_to=first.message_id,
        references=[first.message_id], received_at=datetime.now(UTC),
    )


def test_thanks_gets_no_reply(captured, monkeypatch):
    """"Thanks" used to reach the instruction agent, which ran the model over
    the contract and answered "Nothing to change"."""
    monkeypatch.setattr(handler.review, "review", stub_review())
    monkeypatch.setattr(handler, "_carry_out_instruction",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("agent was called")))
    first = email_with_sample("SPA")
    handler.handle(first)
    handler.handle(followup_to(first, "Many thanks,\nJim"))
    handler.handle(followup_to(first, "All accepted.", "m-follow2"))
    assert len(captured.sent) == 1


def test_thanks_to_a_rejection_gets_no_reply_either(captured):
    first = InboundEmail(
        message_id="m-empty", from_address="jim@firm.com", to=["review@example.com"],
        subject="help", text_body="can you look at this", received_at=datetime.now(UTC),
    )
    handler.handle(first)
    handler.handle(followup_to(first, "ok thanks"))
    assert len(captured.sent) == 1


def test_stop_flagging_is_confirmed_and_not_carried_out_as_an_edit(captured, monkeypatch):
    monkeypatch.setattr(handler.review, "review", stub_review())
    monkeypatch.setattr(handler, "_carry_out_instruction",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("agent was called")))
    first = email_with_sample("SPA")
    handler.handle(first)
    handler.handle(followup_to(first, "Please stop flagging passive voice."))
    assert len(captured.sent) == 2
    body = captured.sent[1].text_body
    assert "will not raise passive voice" in body
    assert "flag passive voice again" in body

    handler.handle(followup_to(first, "flag passive voice again", "m-follow2"))
    assert "will raise passive voice again" in captured.sent[2].text_body


def test_a_suppression_with_an_instruction_still_reaches_the_agent(captured, monkeypatch):
    monkeypatch.setattr(handler.review, "review", stub_review())
    called = []
    monkeypatch.setattr(handler, "_carry_out_instruction",
                        lambda job, email, provider, state, text: called.append(text))
    first = email_with_sample("SPA")
    handler.handle(first)
    handler.handle(followup_to(first, "Stop flagging shall, and cap the indemnity at 12 months."))
    assert called and "cap the indemnity" in called[0]


def test_follow_up_replies_carry_an_html_body(captured, monkeypatch):
    """Round two used to arrive text-only, in Outlook's plain-text face."""
    monkeypatch.setattr(handler.review, "review", stub_review())
    first = email_with_sample("SPA")
    first.text_body = "Memo only please."
    handler.handle(first)
    handler.handle(followup_to(first, "undo everything"))
    assert len(captured.sent) == 2
    assert "<div" in captured.sent[1].html_body


# --- nothing is installed, so this is where the replies that work are learned --


def test_help_lists_what_it_can_do(captured):
    handler.handle(InboundEmail(
        message_id="m-help", from_address="jim@firm.com", to=["review@example.com"],
        subject="hi", text_body="help", received_at=datetime.now(UTC),
    ))
    [out] = captured.sent
    for phrase in ("compare", "clean copy", "undo", "Stop flagging", "BCC", "setup"):
        assert phrase in out.text_body
    assert "<li>" in out.html_body
    assert out.to == ["jim@firm.com"]


def test_setup_explains_the_mail_rule_and_the_two_guarantees(captured):
    handler.handle(InboundEmail(
        message_id="m-setup", from_address="jim@firm.com", to=["review@example.com"],
        subject="hi", text_body="How do I set this up?", received_at=datetime.now(UTC),
    ))
    [out] = captured.sent
    assert "can only Cc, not Bcc" in out.text_body      # no false promise of a Bcc rule
    assert "never to anyone the message was addressed to" in out.text_body
    assert "nothing attached gets no reply" in out.text_body


def test_the_primer_rides_on_the_first_review_and_only_the_first(captured, monkeypatch):
    monkeypatch.setattr(handler.review, "review", stub_review())
    handler.handle(email_with_sample("first"))
    assert 'First time? Reply "help"' in captured.sent[0].text_body
    assert "First time?" in captured.sent[0].html_body

    handler.handle(email_with_sample("second"))
    assert "What I can do" not in captured.sent[1].text_body


def _cards(out) -> list:
    return [a for a in out.attachments if a.content_type == "text/vcard"]


def test_the_first_review_brings_the_contact_card_and_the_second_does_not(
        captured, monkeypatch):
    """So the address autocompletes the next time a document goes out."""
    monkeypatch.setattr(handler.review, "review", stub_review())
    handler.handle(email_with_sample("first"))
    [card] = _cards(captured.sent[0])
    assert card.filename == "Second Eye.vcf"
    assert b"EMAIL;TYPE=INTERNET,PREF:review@example.com" in card.content
    assert documents(captured.sent[0]), "the card came instead of the redline"

    handler.handle(email_with_sample("second"))
    assert _cards(captured.sent[1]) == []


def test_help_and_setup_bring_the_contact_card(captured):
    for message_id, text in (("m-help", "help"), ("m-setup", "setup")):
        handler.handle(InboundEmail(
            message_id=message_id, from_address="jim@firm.com", to=["review@example.com"],
            subject="hi", text_body=text, received_at=datetime.now(UTC),
        ))
    assert [len(_cards(out)) for out in captured.sent] == [1, 1]
    assert "Tap the attached card to save me as a contact" in captured.sent[1].text_body


def test_the_no_document_refusal_points_at_help(captured):
    handler.handle(InboundEmail(
        message_id="m-none", from_address="jim@firm.com", to=["review@example.com"],
        subject="NDA", text_body="have a look?", received_at=datetime.now(UTC),
    ))
    assert 'reply "help"' in captured.sent[0].text_body


# --- the version that actually went out ------------------------------------------
#
# The other half of the learning loop. The BCC'd sent version is read against
# the redline we gave, and what was kept or dropped is recorded and said back.


def a_docx(*paragraphs) -> bytes:
    from io import BytesIO

    from docx import Document
    d = Document()
    for p in paragraphs:
        d.add_paragraph(p)
    buf = BytesIO()
    d.save(buf)
    return buf.getvalue()


def review_fixing_the_term():
    from secondeye.models import Finding, ReviewResult, Severity

    def _review(doc, mode, instructions, **kwargs):
        return ReviewResult(mode=mode, summary="One slip.", findings=[Finding(
            severity=Severity.SUBSTANTIVE, category="amount", title="Words and numerals disagree",
            explanation="", anchor="thirty (13) days", suggested_text="thirty (30) days",
            auto_apply=True,
        )])
    return _review


def draft_email(subject, content, filename="Supply Agreement v3.docx", **kw) -> InboundEmail:
    return InboundEmail(
        message_id=f"m-{subject}", from_address="jim@firm.com", subject=subject,
        text_body="", attachments=[Attachment(filename=filename, content_type=DOCX,
                                              size_bytes=len(content), content=content)],
        received_at=datetime.now(UTC), **kw,
    )


def test_the_sent_version_is_read_against_our_redline(captured, monkeypatch, learning):
    from secondeye import memory

    monkeypatch.setattr(handler.review, "review", review_fixing_the_term())
    original = a_docx("1. Term. The term is thirty (13) days.", "2. Law. English law.")
    handler.handle(draft_email("draft", original, to=["review@example.com"]))
    assert "Tracked in the attached copy" in captured.sent[0].text_body

    # The lawyer accepted it and sent the document to the client, BCC'ing us.
    sent = a_docx("1. Term. The term is thirty (30) days.", "2. Law. English law.")
    handler.handle(draft_email("sent", sent, filename="Supply Agreement final.docx",
                               to=["counsel@acme.com"]))
    body = captured.sent[1].text_body
    assert body.startswith("Already sent")
    assert "you kept 1" in body
    rate, total = memory.rejection_rate("jim@firm.com", "amount")
    assert (rate, total) == (0.0, 1)


def test_a_change_the_lawyer_dropped_is_recorded_as_a_rejection(captured, monkeypatch, learning):
    from secondeye import memory

    monkeypatch.setattr(handler.review, "review", review_fixing_the_term())
    original = a_docx("1. Term. The term is thirty (13) days.", "2. Law. English law.")
    handler.handle(draft_email("draft", original, to=["review@example.com"]))

    # Sent with the original wording: our change was rejected.
    handler.handle(draft_email("sent", original, filename="Supply Agreement (final).docx",
                               to=["counsel@acme.com"]))
    body = captured.sent[1].text_body
    assert "dropped 1 (amount)" in body
    rate, total = memory.rejection_rate("jim@firm.com", "amount")
    assert (rate, total) == (1.0, 1)


def test_by_default_the_sent_version_is_reported_and_nothing_is_learned(captured, monkeypatch):
    """LEARN_FROM_OUTCOMES off, the default: the lawyer still hears what was
    kept and dropped, the reply does not claim to learn from it, and nothing
    is recorded."""
    from secondeye import memory

    monkeypatch.setattr(handler.review, "review", review_fixing_the_term())
    original = a_docx("1. Term. The term is thirty (13) days.", "2. Law. English law.")
    handler.handle(draft_email("draft", original, to=["review@example.com"]))
    handler.handle(draft_email("sent", original, filename="Supply Agreement (final).docx",
                               to=["counsel@acme.com"]))
    body = captured.sent[1].text_body
    assert "dropped 1 (amount)" in body and "I learn" not in body
    assert memory.rejection_rate("jim@firm.com", "amount") == (0.0, 0)


def test_a_sent_document_we_never_reviewed_says_nothing_about_learning(captured, monkeypatch):
    monkeypatch.setattr(handler.review, "review", stub_review())
    handler.handle(draft_email("sent", a_docx("Something else entirely."),
                               filename="Other.docx", to=["counsel@acme.com"]))
    assert "I suggested" not in captured.sent[0].text_body


# --- the redline the session wrote is proved here before it is attached ------
#
# DECISIONS 28: the writer runs in Anthropic's container as a skill, and the
# file it leaves behind is re-verified locally. The local writer runs on the
# same findings regardless; a lawyer never gets fewer changes than it makes.


def _typo_review(outputs):
    from secondeye.models import Finding, ReviewResult, Severity

    fix = Finding(severity=Severity.FORMATTING, category="typo", title="Typo",
                  explanation="", anchor="shall remain in effect for three (3) years",
                  suggested_text="will remain in effect for three (3) years", auto_apply=True)

    def _review(doc, mode, instructions, **kwargs):
        result = ReviewResult(mode=mode, summary="One typo.", findings=[fix])
        result.outputs = outputs
        return result

    return _review


def _redline_of(review_fn):
    """What the local writer produces for that review's findings."""
    from secondeye.models import Mode, ReviewResult
    from secondeye.pipeline import redline

    raw = Path("samples/simple.docx").read_bytes()
    result = review_fn(None, Mode.REDLINE, "")
    local = redline.apply(raw, ReviewResult(mode=Mode.REDLINE, summary="",
                                            findings=result.findings))
    assert local.applied, "the fixture's anchor must land"
    return local.content


def _words(content: bytes) -> list[str]:
    from secondeye.pipeline import compare

    return compare._plain(content, body_only=False)


def test_a_verified_redline_from_the_session_is_the_one_attached(captured, monkeypatch):
    local = _redline_of(_typo_review([]))
    # The session ran the same writer on the same findings: same text, its bytes.
    monkeypatch.setattr(handler.review, "review",
                        _typo_review([("Acme NDA (redline).docx", local)]))
    handler.handle(email_with_sample("from the session"))
    out = captured.sent[0]
    assert [a.filename for a in documents(out)] == ["Acme NDA (redline).docx"]
    assert out.attachments[0].content == local
    assert "\u2192" in out.text_body


def test_a_session_redline_that_fails_verification_is_replaced_by_ours(captured, monkeypatch):
    review_fn = _typo_review([("Acme NDA (redline).docx", b"PK\x03\x04 not a document")])
    monkeypatch.setattr(handler.review, "review", review_fn)
    handler.handle(email_with_sample("broken output"))
    out = captured.sent[0]
    assert out.attachments, "the lawyer got no redline at all"
    from secondeye.pipeline import redline

    ok, why = redline.verify(out.attachments[0].content, expect_revisions=True)
    assert ok, why
    assert "\u2192" in out.text_body


def test_a_session_redline_with_different_text_is_replaced_by_ours(captured, monkeypatch):
    """The container ran the same code on the same findings, so a file whose
    words differ from the local writer's is something else, and is not sent."""
    from secondeye.models import Finding, Mode, ReviewResult, Severity
    from secondeye.pipeline import redline

    raw = Path("samples/simple.docx").read_bytes()
    other = redline.apply(raw, ReviewResult(mode=Mode.REDLINE, summary="", findings=[
        Finding(severity=Severity.FORMATTING, category="x", title="Something else",
                explanation="", anchor="shall remain in effect for three (3) years",
                suggested_text="shall remain in effect for thirty (30) years", auto_apply=True)]))
    monkeypatch.setattr(handler.review, "review",
                        _typo_review([("Acme NDA (redline).docx", other.content)]))
    handler.handle(email_with_sample("tampered output"))
    sent = captured.sent[0].attachments[0].content
    assert _words(sent) == _words(_redline_of(_typo_review([])))
    assert _words(sent) != _words(other.content)


def test_no_redline_from_the_session_means_the_local_writers(captured, monkeypatch):
    monkeypatch.setattr(handler.review, "review", _typo_review([]))
    handler.handle(email_with_sample("nothing produced"))
    assert _words(captured.sent[0].attachments[0].content) == _words(_redline_of(_typo_review([])))


# --- notes the agent proposes are kept only on the lawyer's word -------------

def test_a_proposed_note_is_offered_and_kept_only_when_the_lawyer_says_so(
    captured, monkeypatch
):
    from secondeye import memory

    def review_that_notes(doc, mode, instructions, **kw):
        memory.remember(memory.MemoryEntry(
            scope=memory.Scope.PERSONAL, scope_key="jim@firm.com",
            kind=memory.Kind.PREFERENCE, statement="Jim prefers 'will' to 'shall'",
            confidence=0.6, status=memory.Status.PENDING,
        ))
        return stub_review()(doc, mode, instructions, **kw)

    monkeypatch.setattr(handler.review, "review", review_that_notes)
    handler.handle(email_with_sample("notes"))
    assert 'Reply "remember that"' in captured.sent[0].text_body
    assert memory.pending_notes("jim@firm.com")

    handler.handle(InboundEmail(
        message_id="m-remember", from_address="jim@firm.com", to=["review@example.com"],
        subject="Re: notes", text_body="remember that", received_at=datetime.now(UTC),
    ))
    assert captured.sent[1].text_body.startswith("Remembered.")
    assert not memory.pending_notes("jim@firm.com")


def test_one_tap_links_can_be_switched_off(captured, monkeypatch):
    """Without plus addressing on the mail domain a tapped reply is dropped,
    so production keeps them off until it is."""
    from secondeye.config import settings

    monkeypatch.setenv("ONE_TAP_LINKS", "false")
    settings.cache_clear()
    monkeypatch.setattr(handler.review, "review", stub_review())
    handler.handle(email_with_sample("no taps"))
    assert "mailto:" not in captured.sent[0].html_body
