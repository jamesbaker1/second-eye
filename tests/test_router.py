"""Reply routing, quote stripping, and auto-mail detection.

Most of these exist because the previous behaviour was genuinely dangerous:
the handler matched `text_body.startswith("revoke")`, which fires on any mail
whose first word happens to be "revoke", including a forwarded thread.
"""

from __future__ import annotations

import pytest

from secondeye.pipeline.router import (
    Intent,
    is_automated,
    looks_like_an_answer,
    route_deterministic,
    strip_reply,
)

# --- the bug this module exists to fix -----------------------------------

def test_a_whole_message_of_revoke_disconnects():
    assert route_deterministic("revoke").intent is Intent.REVOKE


def test_a_forwarded_mail_beginning_with_revoke_does_not_disconnect():
    """The old behaviour would silently cut a lawyer off from their documents."""
    body = (
        "Revoke the licence agreement we discussed - can you check whether clause "
        "9 lets us do that unilaterally?"
    )
    routed = route_deterministic(body)
    assert routed is None or routed.intent is not Intent.REVOKE


def test_a_sentence_beginning_with_connect_does_not_trigger_setup():
    body = "Connecting you with Sarah who is handling the diligence on this one."
    routed = route_deterministic(body)
    assert routed is None or routed.intent is not Intent.CONNECT


def test_a_whole_message_of_connect_does_trigger_setup():
    assert route_deterministic("connect").intent is Intent.CONNECT


@pytest.mark.parametrize("body,expected", [
    ("undo", Intent.UNDO),
    ("Undo everything.", Intent.UNDO),
    ("start over", Intent.UNDO),
    ("revert", Intent.UNDO),
    ("stop", Intent.STOP),
    ("no thanks", Intent.STOP),
    ("disconnect", Intent.REVOKE),
    # A phrase written with an apostrophe, typed straight or curly.
    ("don't remember that", Intent.FORGET),
    ("Don\u2019t remember that.", Intent.FORGET),
])
def test_short_unambiguous_replies_route_directly(body, expected):
    assert route_deterministic(body).intent is expected


def test_a_long_instruction_defers_to_the_model():
    body = "Please change the notice period to 60 days and tighten the indemnity."
    assert route_deterministic(body) is None


# --- quote stripping ------------------------------------------------------

def test_quoted_history_is_stripped():
    body = "Make it 30 days.\n\nOn Tue, 3 March 2026, Legal Review wrote:\n> Should this be 30 or 13?"
    assert strip_reply(body) == "Make it 30 days."


def test_angle_quoted_lines_are_stripped():
    body = "Yes please.\n> previous message\n> more of it"
    assert strip_reply(body) == "Yes please."


def test_a_signature_block_is_stripped():
    body = "Undo that.\n\n--\nJim Baker\nPartner\nFirm LLP"
    assert strip_reply(body) == "Undo that."


def test_a_confidentiality_footer_is_stripped():
    body = (
        "Looks right, thanks.\n\n"
        "This email and any attachments are confidential and may be privileged. "
        "If you are not the intended recipient please delete it."
    )
    assert strip_reply(body) == "Looks right, thanks."


def test_an_outlook_forward_header_is_stripped():
    body = "Please review.\n\nFrom: someone@acme.com\nSent: Tuesday\nSubject: Draft"
    assert strip_reply(body) == "Please review."


def test_an_instruction_inside_quoted_history_is_not_obeyed():
    """Reading an instruction out of a quoted thread is how an agent makes a
    change nobody asked for."""
    body = "Thanks.\n\nOn Monday someone wrote:\n> undo everything"
    routed = route_deterministic(body)
    assert routed is None or routed.intent is not Intent.UNDO


# --- automated mail -------------------------------------------------------

def test_an_out_of_office_is_not_answered():
    assert is_automated("partner@firm.com", "Automatic reply: Acme NDA", {})


def test_an_auto_submitted_header_is_respected():
    assert is_automated("x@y.com", "Re: NDA", {"Auto-Submitted": "auto-replied"})


def test_a_bounce_is_not_answered():
    assert is_automated("MAILER-DAEMON@firm.com", "Undeliverable", {})


def test_a_mailing_list_is_not_answered():
    assert is_automated("list@firm.com", "Digest", {"List-Id": "<updates.firm.com>"})


def test_a_noreply_sender_is_not_answered():
    assert is_automated("no-reply@docusign.net", "Completed", {})


def test_a_real_person_is_answered():
    assert is_automated("jim@firm.com", "Re: Acme NDA", {"Auto-Submitted": "no"}) is None


def test_a_normal_reply_is_answered():
    assert is_automated("jim@firm.com", "Re: Acme NDA", {}) is None


# --- answering a question the agent asked --------------------------------

def test_a_one_word_answer_matches_an_offered_option():
    assert looks_like_an_answer("30", ["30", "13"]) == "30"


def test_an_answer_with_punctuation_still_matches():
    assert looks_like_an_answer("30.", ["30", "13"]) == "30"


def test_a_sentence_is_not_treated_as_a_bare_answer():
    assert looks_like_an_answer(
        "I think 30 is right but check clause 9 too", ["30", "13"]) is None


def test_an_answer_that_matches_nothing_returns_none():
    assert looks_like_an_answer("60", ["30", "13"]) is None


def test_an_answer_is_read_past_quoted_history():
    assert looks_like_an_answer(
        "30\n\nOn Tue someone wrote:\n> Should this be 30 or 13?", ["30", "13"]) == "30"


# --- attachments ----------------------------------------------------------

def test_an_attachment_with_no_words_is_a_new_version():
    assert route_deterministic("", has_attachment=True).intent is Intent.NEW_VERSION


def test_an_empty_mail_with_no_attachment_is_a_review_request():
    assert route_deterministic("").intent is Intent.REVIEW


@pytest.mark.parametrize("attribution", [
    "On Tue, Sep 29, 2026 07:44 PM, Second Eye <review@legal.example.com>\nwrote:",
    "On Tue, Sep 29, 2026 at 7:44 PM Second Eye <\nreview@legal.example.com> wrote:",
    "On Tue, Sep 29, 2026 07:44 PM, Second Eye <review@legal.example.com> wrote:",
])
def test_a_wrapped_attribution_is_still_quoted_history(attribution):
    """Production: a bare "30" arrived as twelve words over four lines because
    Gmail had wrapped the attribution before "wrote:"."""
    body = f"30\n\n{attribution}\n\n> Should this be 30 or 13?\n"
    assert strip_reply(body) == "30"


# --- curly apostrophes ------------------------------------------------------


def test_the_words_a_lawyer_wrote_come_back_with_straight_apostrophes():
    assert strip_reply("Here’s the one I‘d like.") == "Here's the one I'd like."
