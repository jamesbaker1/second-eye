"""One-tap answers: the review's HTML body offers each reply it asks for as a
mailto: link, addressed so the reply finds its conversation without headers."""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from io import BytesIO
from urllib.parse import unquote

from docx import Document

from lra import handler, thread
from lra.models import InboundEmail
from lra.pipeline import identity
from tests import test_end_to_end as e2e
from tests.conftest import documents
from tests.test_end_to_end import TYPO, inbound, stub_agent

captured = e2e.captured  # the end-to-end fixture: a provider that keeps what is sent

_HREF = re.compile(r'href="(mailto:[^"]+)"')


def links(out) -> dict[str, str]:
    """{reply body: address} for every one-tap link in a reply's HTML."""
    found = {}
    for href in _HREF.findall(out.html_body):
        href = html.unescape(href)
        address, _, query = href.removeprefix("mailto:").partition("?")
        params = dict(p.split("=", 1) for p in query.split("&"))
        assert unquote(params["subject"]) == "Re: Acme / Beta NDA"
        found[unquote(params["body"])] = address
    return found


def tapped(address: str, body: str, sender: str = "jim@firm.com",
           headers: dict | None = None) -> InboundEmail:
    """What a mail client sends from a mailto: link: a new message, no
    In-Reply-To, no References, to the address in the link."""
    return InboundEmail(
        message_id=f"tap-{body}-{sender}", from_address=sender, to=[address],
        subject="Re: Acme / Beta NDA", text_body=body, headers=headers or {},
        received_at=datetime.now(UTC),
    )


def test_each_answer_is_a_link_that_writes_it(captured, monkeypatch):
    monkeypatch.setattr(handler.review, "review", stub_agent())
    handler.handle(inbound())
    out = captured.sent[0]
    found = links(out)
    for body in ("30", "13", "30; 4; clean copy"):
        assert body in found, (body, found)
    [address] = set(found.values())
    assert re.fullmatch(r"review\+t[0-9a-f]{16}@legal\.firm\.com", address)
    # Plain <a> links, nothing Outlook drops.
    assert "<button" not in out.html_body and "<script" not in out.html_body
    # The text body is exactly as it was.
    assert "mailto:" not in out.text_body


def test_a_numbered_change_has_its_undo_beside_it(captured, monkeypatch):
    monkeypatch.setattr(handler.review, "review", stub_agent(TYPO))
    handler.handle(inbound())
    found = links(captured.sent[0])
    assert "undo 1" in found
    # A bare "undo" reverses everything, so it is never a link.
    assert "undo" not in found


def test_tapping_the_footer_gets_the_clean_copy(captured, monkeypatch):
    monkeypatch.setattr(handler.review, "review", stub_agent())

    def no_model(*a, **k):
        raise AssertionError("the model was called")

    monkeypatch.setattr(handler.instruct, "carry_out", no_model)
    handler.handle(inbound())
    body = "30; 4; clean copy"
    address = links(captured.sent[0])[body]

    handler.handle(tapped(address, body))
    assert len(captured.sent) == 2
    out = captured.sent[1]
    assert out.to == ["jim@firm.com"]
    assert out.text_body.splitlines()[0] == "Clean copy attached."
    assert out.in_reply_to == f"tap-{body}-jim@firm.com"
    [copy] = documents(out)
    text = " ".join(p.text for p in Document(BytesIO(copy.content)).paragraphs)
    assert "thirty (30) months" in text


def test_a_token_does_nothing_for_anyone_but_the_owner(captured, monkeypatch):
    """A colleague on the firm's domain who is sent the link, or guesses one,
    is a different lawyer: the thread is not theirs to answer."""
    monkeypatch.setattr(handler.review, "review", stub_agent())
    handler.handle(inbound())
    address = links(captured.sent[0])["30"]

    handler.handle(tapped(address, "30", sender="bob@firm.com"))
    [key] = [k for k, _ in thread.recent_for_owner("jim@firm.com")]
    state = thread.load(key)
    assert all(q.answered_with is None for q in state.questions)
    assert thread.recent_for_owner("bob@firm.com") == []
    assert all(not documents(o) for o in captured.sent[1:])


def test_a_forged_owner_is_still_stopped_at_authentication(captured, monkeypatch):
    monkeypatch.setattr(handler.review, "review", stub_agent())
    handler.handle(inbound())
    address = links(captured.sent[0])["30"]

    handler.handle(tapped(address, "30", headers={
        "Authentication-Results": "mx.firm.com; dmarc=fail header.from=firm.com"}))
    assert len(captured.sent) == 1
    [key] = [k for k, _ in thread.recent_for_owner("jim@firm.com")]
    assert all(q.answered_with is None for q in thread.load(key).questions)


def test_a_document_already_sent_gets_no_links(captured, monkeypatch):
    monkeypatch.setattr(handler.review, "review", stub_agent())
    email = inbound().model_copy(update={"to": ["client@acme.com"],
                                         "cc": ["review@legal.firm.com"]})
    handler.handle(email)
    assert "mailto:" not in captured.sent[0].html_body


def test_a_tap_tag_is_not_a_matter_and_a_matter_tag_is_not_a_tap(captured):
    tap = tapped("review+t0123456789abcdef@legal.firm.com", "30")
    assert identity.tap_token(tap) == "0123456789abcdef"
    assert identity.resolve_matter(tap) is None
    assert identity.is_agent("review+t0123456789abcdef@legal.firm.com")

    matter = tapped("review+M123@legal.firm.com", "30")
    assert identity.tap_token(matter) is None
    assert identity.resolve_matter(matter) == "M123"
    # A matter that happens to start with "t" is still a matter.
    assert identity.tap_token(tapped("review+tender7@legal.firm.com", "x")) is None
