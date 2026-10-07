from datetime import UTC, datetime

import pytest

from lra.config import settings
from lra.models import InboundEmail
from lra.pipeline.identity import (
    EntryMode,
    bcc_only,
    entry_mode,
    external_recipients,
    is_agent,
    resolve_matter,
    resolve_user,
)

AGENT = settings().mail_agent_address


def mk(to=None, cc=None, in_reply_to=None, subject="", body="",
       from_address="jim@example.com") -> InboundEmail:
    return InboundEmail(
        message_id="m1",
        in_reply_to=in_reply_to,
        from_address=from_address,
        to=to or [],
        cc=cc or [],
        subject=subject,
        text_body=body,
        received_at=datetime.now(UTC),
    )


def test_direct_forward():
    assert entry_mode(mk(to=[AGENT])) is EntryMode.FORWARD


def test_cc_on_a_self_addressed_draft():
    assert entry_mode(mk(to=["jim@example.com"], cc=[AGENT])) is EntryMode.CC_DRAFT


def test_cc_alongside_a_client_is_treated_as_already_sent():
    assert entry_mode(mk(to=["client@acme.com"], cc=[AGENT])) is EntryMode.BCC_SENT


def test_cc_alongside_only_colleagues_is_still_a_draft(monkeypatch):
    """Circulating internally is not the same as sending to a client."""
    monkeypatch.setenv("MAIL_AGENT_ADDRESS", "review@legal.firm.com")
    monkeypatch.setenv("FIRM_DOMAINS", "firm.com")
    settings.cache_clear()
    try:
        e = mk(to=["partner@firm.com"], cc=["review@legal.firm.com"])
        assert entry_mode(e) is EntryMode.CC_DRAFT
    finally:
        settings.cache_clear()


def test_invisible_on_headers_means_bcc():
    assert entry_mode(mk(to=["client@acme.com"])) is EntryMode.BCC_SENT


def test_reply_in_thread_is_detected_first():
    assert entry_mode(mk(to=[AGENT], in_reply_to="<prev@x>")) is EntryMode.REPLY


def test_external_recipients_exclude_the_agent():
    assert external_recipients(mk(to=["client@acme.com"], cc=[AGENT])) == ["client@acme.com"]


# --- being BCC'd is being an observer ---------------------------------------

def test_a_plus_tag_on_our_own_address_is_still_us():
    """review+M123@ is how a lawyer names the matter. Comparing whole strings
    put that message on no visible header, which reads as a BCC."""
    local, domain = AGENT.split("@")
    tagged = f"{local}+M123@{domain}"
    assert is_agent(tagged)
    assert entry_mode(mk(to=[tagged])) is EntryMode.FORWARD
    assert not bcc_only(mk(to=[tagged]))


def test_bcc_only_means_written_to_someone_else():
    assert bcc_only(mk(to=["client@acme.com"]))
    assert bcc_only(mk(to=["partner@example.com"], cc=["assoc@example.com"]))
    assert not bcc_only(mk(to=[AGENT]))
    assert not bcc_only(mk(to=["client@acme.com"], cc=[AGENT]))


def test_no_visible_recipients_at_all_is_not_a_bcc():
    """We cannot tell how such a message reached us, so it is treated as
    addressed and gets an answer rather than silence."""
    assert not bcc_only(mk())


def test_matter_number_pulled_from_subject():
    assert resolve_matter(mk(subject="Matter 10482-ACME - NDA")) == "10482-ACME"


def test_no_matter_returns_none_rather_than_guessing():
    assert resolve_matter(mk(subject="quick look?")) is None


def test_colleagues_are_not_external_when_the_agent_runs_on_a_subdomain(monkeypatch):
    """The deployment plan puts the agent on a subdomain to protect the firm's
    sending reputation. That must not make every colleague look like an
    outside party, which would misfire the already-sent warning on every email."""
    monkeypatch.setenv("MAIL_AGENT_ADDRESS", "review@legal.firm.com")
    monkeypatch.setenv("FIRM_DOMAINS", "firm.com")
    settings.cache_clear()
    try:
        e = mk(to=["colleague@firm.com"], cc=["review@legal.firm.com"])
        assert external_recipients(e) == []
        assert entry_mode(e) is EntryMode.CC_DRAFT
    finally:
        settings.cache_clear()


def test_a_real_client_is_still_external_on_a_subdomain_setup(monkeypatch):
    monkeypatch.setenv("MAIL_AGENT_ADDRESS", "review@legal.firm.com")
    monkeypatch.setenv("FIRM_DOMAINS", "firm.com")
    settings.cache_clear()
    try:
        e = mk(to=["gc@acme.com"], cc=["review@legal.firm.com"])
        assert external_recipients(e) == ["gc@acme.com"]
    finally:
        settings.cache_clear()


def test_replying_on_a_client_thread_with_the_document_is_already_sent(monkeypatch):
    """Hitting Reply on the client's own thread, attaching the revised draft and
    BCC'ing the agent is how client mail is actually sent. It used to be
    classified as an ordinary follow-up, so the warning never fired."""
    from lra.models import Attachment

    monkeypatch.setenv("MAIL_AGENT_ADDRESS", "review@legal.firm.com")
    monkeypatch.setenv("FIRM_DOMAINS", "firm.com")
    settings.cache_clear()
    try:
        e = mk(to=["counsel@betacorp.com"], in_reply_to="<prev@betacorp.com>")
        e.attachments = [Attachment(filename="SPA.docx", content_type="x",
                                    size_bytes=10, content=b"0123456789")]
        assert entry_mode(e) is EntryMode.BCC_SENT
    finally:
        settings.cache_clear()


def test_a_plain_follow_up_with_no_attachment_is_still_a_reply(monkeypatch):
    monkeypatch.setenv("MAIL_AGENT_ADDRESS", "review@legal.firm.com")
    monkeypatch.setenv("FIRM_DOMAINS", "firm.com")
    settings.cache_clear()
    try:
        e = mk(to=["review@legal.firm.com"], in_reply_to="<prev@x>")
        assert entry_mode(e) is EntryMode.REPLY
    finally:
        settings.cache_clear()


# --- address parsing: the field the whole ethical wall keys off ----------

def test_a_display_name_does_not_break_the_allowlist(monkeypatch):
    """Mail arrives as "Jane Partner <jane@firm.com>". That whole string used
    to become from_address, which is what the allowlist, is_internal, identity
    resolution and the thread key all key off, so every lawyer using a normal
    mail client was rejected."""
    from lra.pipeline import intake

    monkeypatch.setenv("ALLOWED_SENDERS", "firm.com")
    settings.cache_clear()
    try:
        e = mk(from_address="Jane Partner <jane@firm.com>")
        assert e.from_address == "jane@firm.com"
        intake.check_sender(e)          # must not raise
    finally:
        settings.cache_clear()


def test_a_quoted_display_name_with_a_comma_is_handled():
    e = mk(from_address='"Partner, Jane" <jane@firm.com>')
    assert e.from_address == "jane@firm.com"


def test_recipients_are_normalised_too(monkeypatch):
    monkeypatch.setenv("MAIL_AGENT_ADDRESS", "review@legal.firm.com")
    monkeypatch.setenv("FIRM_DOMAINS", "firm.com")
    settings.cache_clear()
    try:
        e = mk(to=["Review Agent <review@legal.firm.com>",
                   "GC Beta <gc@betacorp.com>"])
        assert e.to == ["review@legal.firm.com", "gc@betacorp.com"]
        assert external_recipients(e) == ["gc@betacorp.com"]
    finally:
        settings.cache_clear()


def test_the_same_person_resolves_identically_with_and_without_a_name():
    """Otherwise they get a different conversation depending on whether their
    mail client included a display name."""
    from lra import thread

    plain = mk(from_address="jane@firm.com")
    named = mk(from_address="Jane Partner <jane@firm.com>")
    assert resolve_user(plain) == resolve_user(named)
    assert thread.key("t1", "m1", resolve_user(plain)) == thread.key(
        "t1", "m1", resolve_user(named)
    )


def test_an_outsider_with_a_friendly_display_name_is_still_rejected(monkeypatch):
    from lra.pipeline import intake

    monkeypatch.setenv("ALLOWED_SENDERS", "firm.com")
    settings.cache_clear()
    try:
        e = mk(from_address="Jane Partner <attacker@evil.example>")
        assert e.from_address == "attacker@evil.example"
        with pytest.raises(intake.Rejection):
            intake.check_sender(e)
    finally:
        settings.cache_clear()


def test_an_alias_that_forwards_to_the_agent_is_still_us(monkeypatch):
    monkeypatch.setenv("MAIL_AGENT_ALIASES", "legal@example.com, Reviews@Example.com")
    settings.cache_clear()
    try:
        assert is_agent("legal@example.com")
        assert not bcc_only(mk(to=["reviews@example.com"]))
        assert entry_mode(mk(to=["legal@example.com"])) is EntryMode.FORWARD
    finally:
        settings.cache_clear()


def test_the_refusal_says_who_to_ask(monkeypatch):
    """"Not on the list yet" with no next step is where a colleague gives up."""
    from lra.pipeline import intake

    monkeypatch.setenv("ALLOWED_SENDERS", "jim@firm.com")
    monkeypatch.setenv("ALLOWLIST_CONTACT", "Jim Baker")
    settings.cache_clear()
    try:
        with pytest.raises(intake.Rejection, match="Ask Jim Baker to add you"):
            intake.check_sender(mk(from_address="assoc@firm.com"))
    finally:
        settings.cache_clear()
