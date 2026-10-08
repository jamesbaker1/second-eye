"""Mail adapter tests.

The AgentMail and iManage adapters were written from documentation nobody could
test against a live account, so these tests pin the things that would otherwise
fail silently in production: a webhook that authenticates when it should not, a
reply that quietly starts a new conversation, a size cap enforced against a
number the provider made up, and a replay parser that dies on a real mailbox
export. Nothing here reaches the network -- respx serves the vendor APIs.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging

import httpx
import pytest
import respx

from secondeye.config import settings
from secondeye.mail import get_provider
from secondeye.mail.agentmail import AgentMailProvider
from secondeye.mail.console import ConsoleProvider, UnparseableMessage
from secondeye.mail.postmark import PostmarkProvider
from secondeye.models import Attachment, OutboundEmail
from secondeye.pipeline.identity import EntryMode, entry_mode

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.fixture
def env(monkeypatch):
    """Configure the mail settings, with the cache cleared either side."""

    def _set(**values):
        for key, value in values.items():
            monkeypatch.setenv(key, value)
        settings.cache_clear()
        return settings()

    settings.cache_clear()
    yield _set
    settings.cache_clear()


# --- provider selection ----------------------------------------------------


def test_unknown_provider_name_refuses_to_start(env):
    env(MAIL_PROVIDER="postmarkk")
    with pytest.raises(RuntimeError) as e:
        get_provider()
    assert "postmarkk" in str(e.value)


def test_console_provider_requires_an_explicit_opt_in(env):
    """The no-auth dev provider is never a fallback, only a choice."""
    env(MAIL_PROVIDER="")
    with pytest.raises(RuntimeError):
        get_provider()
    env(MAIL_PROVIDER="console")
    assert get_provider().name == "console"


def test_vendor_provider_without_a_webhook_secret_refuses_to_start(env):
    env(MAIL_PROVIDER="postmark", POSTMARK_INBOUND_SECRET="", POSTMARK_SERVER_TOKEN="t")
    with pytest.raises(RuntimeError) as e:
        get_provider()
    assert "POSTMARK_INBOUND_SECRET" in str(e.value)

    env(
        MAIL_PROVIDER="agentmail",
        AGENTMAIL_WEBHOOK_SECRET="",
        AGENTMAIL_API_KEY="k",
        AGENTMAIL_INBOX_ID="i",
    )
    with pytest.raises(RuntimeError) as e:
        get_provider()
    assert "AGENTMAIL_WEBHOOK_SECRET" in str(e.value)


def test_vendor_provider_without_send_credentials_refuses_to_start(env):
    """Accepting mail and failing to send it is invisible from /health."""
    env(MAIL_PROVIDER="postmark", POSTMARK_INBOUND_SECRET="s", POSTMARK_SERVER_TOKEN="")
    with pytest.raises(RuntimeError) as e:
        get_provider()
    assert "POSTMARK_SERVER_TOKEN" in str(e.value)


# --- webhook authentication ------------------------------------------------


def test_no_provider_authenticates_an_unsigned_request_when_unconfigured(env):
    """An unset secret must reject, never compare "" against "" and pass."""
    env(MAIL_PROVIDER="postmark", POSTMARK_INBOUND_SECRET="", POSTMARK_SERVER_TOKEN="t")
    assert PostmarkProvider().verify({}, b'{"From":"partner@firm.com"}') is False

    env(MAIL_PROVIDER="agentmail", AGENTMAIL_WEBHOOK_SECRET="")
    assert AgentMailProvider().verify({}, b'{"from":"partner@firm.com"}') is False


def test_postmark_verify_accepts_only_the_configured_secret(env):
    env(MAIL_PROVIDER="postmark", POSTMARK_INBOUND_SECRET="shh", POSTMARK_SERVER_TOKEN="t")
    p = PostmarkProvider()
    assert p.verify({"x-inbound-secret": "shh"}, b"{}") is True
    assert p.verify({"x-inbound-secret": "wrong"}, b"{}") is False
    assert p.verify({}, b"{}") is False


def test_agentmail_verify_accepts_hex_base64_and_prefixed_signatures(env):
    """Same secret, same body, three encodings vendors actually ship."""
    env(AGENTMAIL_WEBHOOK_SECRET="topsecret")
    p = AgentMailProvider()
    body = b'{"message":{"message_id":"m1"}}'
    digest = hmac.new(b"topsecret", body, hashlib.sha256)
    hex_sig = digest.hexdigest()
    b64_sig = base64.b64encode(digest.digest()).decode()

    assert p.verify({"x-agentmail-signature": hex_sig}, body) is True
    assert p.verify({"x-agentmail-signature": f"sha256={hex_sig}"}, body) is True
    assert p.verify({"x-agentmail-signature": f"v1,{b64_sig}"}, body) is True
    assert p.verify({"agentmail-signature": hex_sig}, body) is True
    assert p.verify({"x-agentmail-signature": hex_sig}, body + b" ") is False
    assert p.verify({"x-agentmail-signature": "é" * 64}, body) is False


def test_agentmail_verify_failure_names_the_headers_it_received(env, caplog):
    """A silent 401 is indistinguishable from an attack; this one is not."""
    env(AGENTMAIL_WEBHOOK_SECRET="topsecret")
    with caplog.at_level(logging.ERROR):
        assert AgentMailProvider().verify({"svix-signature": "v1,abc"}, b"{}") is False
    assert "svix-signature" in caplog.text
    assert "x-agentmail-signature" in caplog.text


# --- AgentMail inbound and outbound ----------------------------------------


def agentmail_payload(**over):
    msg = {
        "message_id": "<rfc@client.com>",
        "from": "Jane Partner <jane@firm.com>",
        "to": ["review@example.com"],
        "subject": "Draft NDA",
        "text": "Have a look before I send this.",
        "attachments": [{"filename": "nda.docx", "content_type": DOCX,
                         "attachment_id": "a1"}],
    }
    msg.update(over)
    return {"message": msg}


@respx.mock
def test_agentmail_attachment_size_is_measured_not_claimed(env):
    """The provider's own size field is a guess; MAX_ATTACHMENT_MB rides on it."""
    env(AGENTMAIL_API_KEY="k", AGENTMAIL_INBOX_ID="inbox1")
    respx.get(
        "https://api.agentmail.to/v0/inboxes/inbox1/messages/<rfc@client.com>"
        "/attachments/a1"
    ).mock(return_value=httpx.Response(200, content=b"x" * 5000))

    # "size": 0 is what a renamed field looks like from here.
    payload = agentmail_payload(
        attachments=[{"filename": "nda.docx", "content_type": DOCX,
                      "attachment_id": "a1", "size": 0}]
    )
    email = AgentMailProvider().parse_inbound(payload)
    assert email.attachments[0].size_bytes == 5000


def test_agentmail_thread_id_is_read_under_any_of_its_spellings(env):
    env(AGENTMAIL_API_KEY="k", AGENTMAIL_INBOX_ID="inbox1")
    p = AgentMailProvider()
    for payload in (
        agentmail_payload(attachments=[], thread_id="t1"),
        agentmail_payload(attachments=[], threadId="t1"),
        agentmail_payload(attachments=[], thread="t1"),
        agentmail_payload(attachments=[], thread={"id": "t1"}),
    ):
        assert p.parse_inbound(payload).thread_id == "t1"


def test_agentmail_payload_without_a_sender_says_which_keys_it_had(env):
    env(AGENTMAIL_API_KEY="k", AGENTMAIL_INBOX_ID="inbox1")
    with pytest.raises(ValueError) as e:
        AgentMailProvider().parse_inbound({"message": {"message_id": "m1",
                                                       "sender": "jane@firm.com"}})
    assert "sender" in str(e.value)


@respx.mock
def test_agentmail_reply_carries_threading_headers_on_both_paths(env):
    """Threading must not depend on the thread-id field name being right."""
    env(AGENTMAIL_API_KEY="k", AGENTMAIL_INBOX_ID="inbox1")
    p = AgentMailProvider()

    threaded = respx.post(
        "https://api.agentmail.to/v0/inboxes/inbox1/threads/t1/reply"
    ).mock(return_value=httpx.Response(200, json={"message_id": "out1"}))
    standalone = respx.post(
        "https://api.agentmail.to/v0/inboxes/inbox1/messages/send"
    ).mock(return_value=httpx.Response(200, json={"message_id": "out2"}))

    p.send(OutboundEmail(to=["jane@firm.com"], subject="Re: Draft NDA",
                         text_body="Approved.", in_reply_to="<rfc@client.com>",
                         thread_id="t1"))
    body = threaded.calls[0].request.content.decode()
    assert "<rfc@client.com>" in body
    assert "in_reply_to" in body and "references" in body

    p.send(OutboundEmail(to=["jane@firm.com"], subject="Re: Draft NDA",
                         text_body="Approved.", in_reply_to="<rfc@client.com>"))
    body = standalone.calls[0].request.content.decode()
    assert "in_reply_to" in body and "references" in body


@respx.mock
def test_agentmail_warns_when_a_reply_has_no_thread_to_land_in(env, caplog):
    """Otherwise every reply silently arrives as a new conversation."""
    env(AGENTMAIL_API_KEY="k", AGENTMAIL_INBOX_ID="inbox1")
    respx.post("https://api.agentmail.to/v0/inboxes/inbox1/messages/send").mock(
        return_value=httpx.Response(200, json={"message_id": "out2"})
    )
    with caplog.at_level(logging.WARNING):
        AgentMailProvider().send(
            OutboundEmail(to=["jane@firm.com"], subject="Re: NDA",
                          text_body="Approved.", in_reply_to="<rfc@client.com>")
        )
    assert "thread" in caplog.text.lower()


# --- Postmark inbound and outbound -----------------------------------------


def postmark_payload(**over):
    payload = {
        "MessageID": "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0",
        "From": "Jane Partner <jane@firm.com>",
        "FromName": "Jane Partner",
        "ToFull": [{"Email": "review@example.com"}],
        "CcFull": [],
        "Subject": "Draft NDA",
        "TextBody": "Have a look.",
        "Headers": [
            {"Name": "Message-ID", "Value": "<rfc-real@client.com>"},
            {"Name": "In-Reply-To", "Value": "<parent@client.com>"},
        ],
        "Attachments": [
            {
                "Name": "nda.docx",
                "ContentType": DOCX,
                "ContentLength": 3,
                "Content": base64.b64encode(b"y" * 4096).decode(),
            }
        ],
    }
    payload.update(over)
    return payload


def test_postmark_threads_on_the_senders_message_id(env):
    env(POSTMARK_INBOUND_SECRET="s", POSTMARK_SERVER_TOKEN="t")
    email = PostmarkProvider().parse_inbound(postmark_payload())
    assert email.message_id == "<rfc-real@client.com>"
    assert email.in_reply_to == "<parent@client.com>"


def test_postmark_attachment_size_is_measured_not_claimed(env):
    env(POSTMARK_INBOUND_SECRET="s", POSTMARK_SERVER_TOKEN="t")
    email = PostmarkProvider().parse_inbound(postmark_payload())
    assert email.attachments[0].size_bytes == 4096


@respx.mock
def test_postmark_reply_sets_in_reply_to_and_references(env):
    env(POSTMARK_INBOUND_SECRET="s", POSTMARK_SERVER_TOKEN="t")
    route = respx.post("https://api.postmarkapp.com/email").mock(
        return_value=httpx.Response(200, json={"MessageID": "out"})
    )
    PostmarkProvider().send(
        OutboundEmail(
            to=["jane@firm.com"],
            subject="Re: Draft NDA",
            text_body="Approved.",
            in_reply_to="<rfc-real@client.com>",
            attachments=[Attachment(filename="nda.docx", content_type=DOCX,
                                    size_bytes=3, content=b"abc")],
        )
    )
    sent = route.calls[0].request.content.decode()
    assert '"In-Reply-To"' in sent
    assert '"References"' in sent


# --- console replay parser -------------------------------------------------


def eml(body_charset="utf-8", from_header="Jane Partner <jane@firm.com>",
        extra_headers="", body="Have a look before I send this.",
        content_type="text/plain") -> bytes:
    headers = [
        f"From: {from_header}" if from_header else None,
        "To: review@example.com",
        "Subject: Draft NDA",
        "Message-ID: <m1@client.com>",
        extra_headers or None,
        f'Content-Type: {content_type}; charset="{body_charset}"',
    ]
    head = "\r\n".join(h for h in headers if h)
    return (head + "\r\n\r\n" + body + "\r\n").encode("utf-8")


def test_replay_reads_a_body_in_a_charset_python_has_never_heard_of():
    """Real mailbox exports carry charsets like "ansi"; a traceback is not an answer."""
    email = ConsoleProvider().parse_eml(eml(body_charset="ansi"))
    assert "Have a look" in email.body


def test_replay_populates_in_reply_to_so_a_reply_is_recognised_as_one():
    email = ConsoleProvider().parse_eml(
        eml(extra_headers="In-Reply-To: <parent@client.com>")
    )
    assert email.in_reply_to == "<parent@client.com>"
    assert entry_mode(email) is EntryMode.REPLY


def test_replay_falls_back_to_the_references_chain_for_the_parent():
    email = ConsoleProvider().parse_eml(
        eml(extra_headers="References: <a@client.com> <b@client.com>")
    )
    assert email.in_reply_to == "<b@client.com>"


def test_replay_refuses_a_message_with_no_usable_sender():
    """An empty From means reviewing as nobody and replying to nobody."""
    with pytest.raises(UnparseableMessage):
        ConsoleProvider().parse_eml(eml(from_header=""))


def test_replay_keeps_an_html_only_body():
    """HTML-only mail is ordinary; dropping it left the covering email empty."""
    email = ConsoleProvider().parse_eml(
        eml(content_type="text/html", body="<p>Clean copy, please check.</p>")
    )
    assert "Clean copy" in email.body


def test_a_signature_header_we_cannot_compare_is_a_rejection_not_a_crash(env):
    """One accented character used to 500 where a wrong secret 401s."""
    env(MAIL_PROVIDER="postmark", POSTMARK_INBOUND_SECRET="shh", POSTMARK_SERVER_TOKEN="t")
    assert PostmarkProvider().verify({"x-inbound-secret": "shé"}, b"{}") is False

    env(AGENTMAIL_WEBHOOK_SECRET="topsecret")
    assert AgentMailProvider().verify({"x-agentmail-signature": "abcé"}, b"{}") is False


# --- the name on the From line ------------------------------------------------


def test_the_agent_goes_by_the_name_on_the_landing_page():
    """One name. The landing page says Second Eye; the inbox said "Legal
    Review", so a lawyer met two products."""
    from secondeye.config import Settings

    assert Settings.model_fields["mail_agent_name"].default == "Second Eye"


@respx.mock
def test_postmark_sends_from_the_agent_name_not_a_bare_address(env):
    import json

    env(POSTMARK_INBOUND_SECRET="s", POSTMARK_SERVER_TOKEN="t",
        MAIL_AGENT_ADDRESS="review@legal.firm.com", FIRM_DOMAINS="firm.com",
        MAIL_AGENT_NAME="Second Eye")
    route = respx.post("https://api.postmarkapp.com/email").mock(
        return_value=httpx.Response(200, json={"MessageID": "out"})
    )
    PostmarkProvider().send(OutboundEmail(to=["jane@firm.com"], subject="Re: NDA",
                                          text_body="Fine."))
    sent = json.loads(route.calls[0].request.content)
    assert sent["From"] == "Second Eye <review@legal.firm.com>"


def test_console_shows_who_the_reply_is_from(env, capsys):
    env(MAIL_AGENT_ADDRESS="review@legal.firm.com", FIRM_DOMAINS="firm.com",
        MAIL_AGENT_NAME="Second Eye")
    ConsoleProvider().send(OutboundEmail(to=["jane@firm.com"], subject="Re: NDA",
                                         text_body="Fine."))
    assert "From: Second Eye <review@legal.firm.com>" in capsys.readouterr().out
