"""The one moment the product asks for a browser.

`consent` had no test of its own. Everything it decides is user-visible and
one-way: offer a link, or do not. Offering one when there is no document system
configured spends the single browser moment this product gets on a page that
resolves to nothing, and the audit found exactly that path reachable.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from lra import consent, oauth
from lra.config import settings
from lra.models import InboundEmail


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'lra.sqlite3'}")
    monkeypatch.setenv("MAIL_AGENT_ADDRESS", "review@example.com")
    settings.cache_clear()
    yield
    settings.cache_clear()


def _email() -> InboundEmail:
    return InboundEmail(
        message_id="m1",
        thread_id="t1",
        from_address="jim@firm.com",
        to=["review@example.com"],
        subject="Falcon NDA",
        text_body="Have a look before I send this.",
        received_at=datetime.now(UTC),
    )


def test_no_document_system_means_nothing_to_consent_to(monkeypatch):
    monkeypatch.setenv("DMS_PROVIDER", "none")
    settings.cache_clear()
    assert consent.needs_consent("jim@firm.com") is False


def test_a_lawyer_with_no_token_is_asked_once(monkeypatch):
    # The capability binding: the token is ours to hold (dms_mcp.py).
    monkeypatch.setenv("DMS_MCP_BINDING", "capability")
    monkeypatch.setenv("DMS_PROVIDER", "imanage")
    monkeypatch.setenv("DMS_AUTHORIZE_URL", "https://dms.example.com/authorize")
    monkeypatch.setenv("DMS_CLIENT_ID", "client-123")
    settings.cache_clear()

    assert consent.needs_consent("jim@firm.com") is True

    oauth.save(oauth.Token(
        user_address="jim@firm.com",
        system="imanage",
        access_token="tok",
        refresh_token=None,
        scopes="read",
        expires_at=None,
    ))
    assert consent.needs_consent("jim@firm.com") is False


def test_the_consent_email_goes_to_the_sender_and_nobody_else(monkeypatch):
    """The reply-to-sender-only rule is not only for review replies.

    This email carries a link that grants access to the firm's document system
    as the person who clicks it. A CC'd client on the original thread must never
    receive it.
    """
    monkeypatch.setenv("DMS_PROVIDER", "imanage")
    monkeypatch.setenv("DMS_AUTHORIZE_URL", "https://dms.example.com/authorize")
    monkeypatch.setenv("DMS_CLIENT_ID", "client-123")
    settings.cache_clear()

    inbound = _email()
    inbound.cc = ["client@acme.com"]
    out = consent.consent_email(inbound)

    assert out.to == ["jim@firm.com"]
    assert not out.cc
    assert "https://dms.example.com/authorize" in out.text_body
    # It threads onto the document they sent, which is what makes it expected
    # rather than an unsolicited request for credentials.
    assert out.in_reply_to == "m1"
    assert out.subject.startswith("Re: Falcon NDA")
