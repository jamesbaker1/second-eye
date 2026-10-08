from datetime import UTC, datetime

from lra.models import (
    Attachment,
    Finding,
    InboundEmail,
    Mode,
    OutboundEmail,
    ReviewResult,
    Severity,
)
from lra.pipeline import reply


def inbound() -> InboundEmail:
    return InboundEmail(
        message_id="m1",
        from_address="jim@example.com",
        subject="SPA draft",
        received_at=datetime.now(UTC),
    )


def blocker() -> Finding:
    return Finding(
        severity=Severity.BLOCKER,
        category="placeholder",
        title="[PARTY NAME] left in section 1",
        explanation="The counterparty name was never filled in.",
        anchor="[PARTY NAME]",
    )


def test_verdict_leads_the_body_and_stays_out_of_the_subject():
    out = reply.compose(inbound(), ReviewResult(mode=Mode.REDLINE, summary="s",
                                                findings=[blocker()]), None, "a.docx", [], [blocker()])
    assert out.text_body.startswith("Don't send yet")
    # The subject is the lawyer's own, so the reply threads under their email.
    assert out.subject == "Re: " + inbound().subject.removeprefix("Re: ")


def test_clean_document_says_so_plainly():
    out = reply.compose(inbound(), ReviewResult(mode=Mode.REDLINE, summary="Clean."),
                        None, "a.docx", [], [])
    assert out.text_body.startswith("Ready to send. Nothing to flag. Clean draft.\n")


def test_only_a_draft_with_nothing_in_it_is_called_clean():
    """Minor points, or a document already sent, do not earn the compliment."""
    from lra.pipeline.identity import EntryMode

    nits = [nit("Double space")]
    out = reply.compose(inbound(), ReviewResult(mode=Mode.REDLINE, summary="", findings=nits),
                        None, "a.docx", [], nits)
    assert out.text_body.startswith("Ready to send. A few optional tidy-ups below.")
    assert "Clean draft" not in out.text_body

    sent = reply.compose(inbound(), ReviewResult(mode=Mode.REDLINE, summary="Clean."),
                         None, "a.docx", [], [], entry=EntryMode.BCC_SENT)
    assert sent.text_body.startswith("Already sent. Nothing in it needs correcting.")
    assert "Clean draft" not in sent.text_body


def test_memo_mode_never_attaches_a_document():
    out = reply.compose(inbound(), ReviewResult(mode=Mode.MEMO_ONLY, summary="s"),
                        b"docxbytes", "a.docx", [], [])
    assert out.attachments == []


def test_reply_stays_in_thread():
    out = reply.compose(inbound(), ReviewResult(mode=Mode.REDLINE, summary="s"),
                        None, "a.docx", [], [])
    assert out.in_reply_to == "m1"


def test_bcc_on_a_sent_message_reads_as_an_incident_not_a_redline():
    from lra.pipeline.identity import EntryMode

    out = reply.compose(
        inbound(),
        ReviewResult(mode=Mode.REDLINE, summary="s", findings=[blocker()]),
        b"docxbytes",
        "a.docx",
        [],
        [blocker()],
        entry=EntryMode.BCC_SENT,
        external=["client@acme.com"],
    )
    assert out.text_body.startswith("Already sent")
    assert "client@acme.com" in out.text_body
    assert "This already went to client@acme.com." in out.text_body


# --- the verdict describes the email it is sitting on top of --------------

def substantive(title="The indemnity is uncapped", **kw) -> Finding:
    return Finding(
        severity=Severity.SUBSTANTIVE,
        category="indemnity",
        title=title,
        explanation="The seller carries unlimited exposure.",
        anchor=title,
        **kw,
    )


def nit(title) -> Finding:
    return Finding(severity=Severity.STYLE, category="style", title=title,
                   explanation="", anchor=title)


def test_the_verdict_never_points_at_a_section_that_is_not_there():
    """The one substantive finding was written as a tracked change, so the
    "Worth your judgment" section is not rendered. Pointing at it sent the
    lawyer scrolling for a section that does not exist."""
    applied = substantive(suggested_text="capped at the purchase price",
                          auto_apply=True)
    out = reply.compose(
        inbound(),
        ReviewResult(mode=Mode.REDLINE, summary="", findings=[applied]),
        b"docxbytes", "a.docx", [applied], [],
    )
    assert "substantive notes" not in out.text_body
    assert "Worth your judgment" not in out.text_body
    assert out.text_body.startswith("Ready to send once you accept my 1 tracked change.")


def test_a_substantive_finding_that_is_still_yours_to_read_is_announced():
    f = substantive()
    out = reply.compose(
        inbound(), ReviewResult(mode=Mode.REDLINE, summary="", findings=[f]),
        None, "a.docx", [], [f],
    )
    assert out.text_body.startswith("Nearly ready: 1 point for your call.")
    # The verdict names the heading the reader then scrolls to.
    assert "Your call" in out.text_body
    assert "substantive notes" not in out.text_body


def test_a_question_is_named_in_the_verdict_rather_than_called_a_cleanup():
    f = substantive(question="Should the cap be 1x or 2x?", options=["1x", "2x"])
    out = reply.compose(
        inbound(), ReviewResult(mode=Mode.REDLINE, summary="", findings=[f]),
        None, "a.docx", [], [f],
    )
    assert out.text_body.startswith("Nearly ready: 1 quick question.")
    assert "Quick questions" in out.text_body


# --- what has already gone out --------------------------------------------

def test_an_already_sent_document_is_not_told_to_hold_the_send():
    """"Do not send until these are resolved" printed directly under "this
    already went to counsel@betacorp.com". The next action is a correction to
    the client, not holding a send that has happened."""
    from lra.pipeline.identity import EntryMode

    out = reply.compose(
        inbound(),
        ReviewResult(mode=Mode.REDLINE, summary="", findings=[blocker()]),
        None, "a.docx", [], [blocker()],
        entry=EntryMode.BCC_SENT, external=["counsel@betacorp.com"],
    )
    assert "Do not send" not in out.text_body
    assert "Wrong in what you sent" in out.text_body


# --- minor findings are counted, in every mail client ---------------------

def test_minor_points_are_counted_not_enumerated():
    """<details> was the only thing keeping fourteen style nits from printing
    beside one blocker, and Outlook on Windows does not implement it."""
    nits = [nit(f"Nit number {i}") for i in range(14)]
    out = reply.compose(
        inbound(), ReviewResult(mode=Mode.REDLINE, summary="", findings=nits),
        None, "a.docx", [], nits,
    )
    assert "14 minor points" in out.text_body
    assert "and 11 more" in out.text_body
    assert "Nit number 13" not in out.text_body
    assert "<details" not in out.html_body
    assert "and 11 more" in out.html_body
    assert "Nit number 13" not in out.html_body


# --- the attachment, and its absence --------------------------------------

def test_a_redline_too_large_to_send_is_explained_not_attached():
    """Postmark's outbound cap is below the inbound cap this service accepts,
    so attaching it anyway failed the send and the lawyer was told the review
    had not run."""
    huge = b"x" * (reply.MAX_ATTACHMENT_BYTES + 1)
    out = reply.compose(
        inbound(), ReviewResult(mode=Mode.REDLINE, summary="", findings=[blocker()]),
        huge, "a.docx", [blocker()], [],
    )
    assert out.attachments == []
    assert "more than I can attach" in out.text_body


def test_a_redline_that_could_not_be_written_says_so():
    """Silence about a missing attachment reads as the firm's mail gateway
    having stripped it."""
    out = reply.compose(
        inbound(), ReviewResult(mode=Mode.REDLINE, summary="", findings=[blocker()]),
        None, "a.docx", [], [blocker()],
    )
    assert out.attachments == []
    assert "did not attach an edited copy" in out.text_body


def test_a_clean_document_is_not_told_why_nothing_is_attached():
    out = reply.compose(
        inbound(), ReviewResult(mode=Mode.REDLINE, summary="Clean."),
        None, "a.docx", [], [],
    )
    assert "did not attach" not in out.text_body


# --- which review ran ------------------------------------------------------

def test_a_memo_only_review_says_it_did_not_touch_the_document():
    out = reply.compose(
        inbound(), ReviewResult(mode=Mode.MEMO_ONLY, summary="", findings=[blocker()]),
        None, "a.docx", [], [blocker()],
    )
    assert "Notes only" in out.text_body


def test_a_deck_is_told_why_it_cannot_have_tracked_changes():
    out = reply.compose(
        inbound(), ReviewResult(mode=Mode.MEMO_ONLY, summary="", findings=[blocker()]),
        None, "deck.pptx", [], [blocker()],
    )
    assert "cannot put tracked changes into a .pptx" in out.text_body


def test_a_pdf_in_memo_mode_is_not_told_it_cannot_be_redlined():
    """It can be, in a Word copy built from its text. Memo mode on a PDF now
    means the lawyer asked for notes, or the handler has its own note saying
    why no copy could be built (a scan); this function must not contradict
    either with the old sentence."""
    out = reply.compose(
        inbound(), ReviewResult(mode=Mode.MEMO_ONLY, summary="", findings=[blocker()]),
        None, "brief.pdf", [], [blocker()],
    )
    assert "cannot put tracked changes" not in out.text_body
    assert "Notes only" in out.text_body


def test_the_redline_is_always_a_docx_whatever_arrived():
    """A PDF's marked-up copy used to be named "brief (redline).pdf": a Word
    file with an extension nothing would open it under."""
    assert reply._redline_name("brief.pdf") == "brief (redline).docx"
    assert reply._redline_name("SPA.docx") == "SPA (redline).docx"
    assert reply.annotated_name("brief.pdf") == "brief (annotated).pdf"


def test_a_proofread_says_the_substance_was_left_alone():
    """A downgraded review used to look exactly like a full one."""
    out = reply.compose(
        inbound(), ReviewResult(mode=Mode.PROOFREAD, summary="", findings=[blocker()]),
        None, "a.docx", [], [blocker()],
    )
    assert "not substance" in out.text_body


# --- a review that ran out of time ------------------------------------------

def test_a_review_cut_short_never_reads_as_clean():
    """The agent hit its time budget and reported what it had. "Looks good.
    Nothing to flag" is a claim about the whole document, which it cannot make."""
    out = reply.compose(inbound(), ReviewResult(mode=Mode.REDLINE, summary="s", cut_short=True),
                        None, "a.docx", [], [])
    assert not out.text_body.startswith("Ready to send")
    assert out.text_body.startswith("Partial review")
    assert "not a full review" in out.text_body


def test_a_blocker_in_a_review_cut_short_still_blocks():
    out = reply.compose(
        inbound(), ReviewResult(mode=Mode.REDLINE, summary="s", findings=[blocker()],
                                cut_short=True),
        None, "a.docx", [], [blocker()],
    )
    assert out.text_body.startswith("Don't send yet: 1 thing to fix first.")
    assert "not a full review" in out.text_body


def test_a_complete_review_says_nothing_about_time():
    out = reply.compose(inbound(), ReviewResult(mode=Mode.REDLINE, summary="Clean."),
                        None, "a.docx", [], [])
    assert "ran out of time" not in out.text_body


def test_a_full_review_does_not_explain_itself():
    out = reply.compose(
        inbound(), ReviewResult(mode=Mode.REDLINE, summary="", findings=[blocker()]),
        b"docxbytes", "a.docx", [blocker()], [],
    )
    assert "notes only" not in out.text_body
    assert "proofread" not in out.text_body.lower()


# --- an edit that was attempted and abandoned ------------------------------

def test_an_edit_that_could_not_be_placed_is_not_silently_a_plain_finding():
    """Otherwise a blocker the redliner tried and failed to anchor reads
    exactly like one nobody ever intended to fix."""
    unplaced = Finding(
        severity=Severity.BLOCKER, category="placeholder",
        title="[PARTY NAME] left in section 1",
        explanation="The counterparty name was never filled in.",
        anchor="[PARTY NAME]", suggested_text="Beta Corp", auto_apply=True,
    )
    out = reply.compose(
        inbound(), ReviewResult(mode=Mode.REDLINE, summary="", findings=[unplaced]),
        None, "a.docx", [], [unplaced],
    )
    assert "could not give you this one as a tracked change" in out.text_body
    assert "could not give you an edited copy" in out.text_body


# --- the follow-up replies arrive in the same face as the first --------------

def test_a_line_composed_reply_renders_as_html_with_its_lists():
    text = ("I reversed the clause 7 change.\n\nThe attached copy has 2 tracked changes:\n"
            "  - Party name aligned.\n  - Date format.\n\nReply again if you want anything else.\n")
    out = reply.text_as_html(text)
    assert out.count("<li>") == 2 and "<b>" not in out
    assert 'font-weight:600;margin:0 0 .8em">I reversed the clause 7 change.</p>' in out
    assert "<p style=\"margin:0 0 .3em;font-weight:600\"></p>" not in out


# --- forty tracked changes -----------------------------------------------------

def applied_nit(i, category="defined-term"):
    return Finding(severity=Severity.STYLE, category=category, title=f"Corrected term {i}",
                   explanation="", anchor=f"t{i}", suggested_text="x", auto_apply=True)


def test_many_tracked_changes_are_counted_by_kind_and_the_substantive_ones_named():
    applied = [applied_nit(i) for i in range(23)] + [applied_nit(i, "date") for i in range(6)]
    applied.append(substantive("Capped the indemnity", suggested_text="x", auto_apply=True))
    out = reply.compose(
        inbound(), ReviewResult(mode=Mode.REDLINE, summary="", findings=applied),
        b"docxbytes", "a.docx", applied, [],
    )
    body = out.text_body
    assert "accept my 30 tracked changes" in body
    assert "23 defined-term corrections" in body
    assert "6 date-format corrections" in body
    assert "Capped the indemnity" in body
    assert "Corrected term 5" not in body
    assert 'Reply "clean copy"' in body


def test_a_few_tracked_changes_are_still_named_one_by_one():
    applied = [applied_nit(i) for i in range(3)]
    out = reply.compose(
        inbound(), ReviewResult(mode=Mode.REDLINE, summary="", findings=applied),
        b"docxbytes", "a.docx", applied, [],
    )
    assert "\u201ct2\u201d \u2192 \u201cx\u201d" in out.text_body
    assert "corrections" not in out.text_body


def test_no_clean_copy_hint_when_nothing_was_changed():
    out = reply.compose(inbound(), ReviewResult(mode=Mode.REDLINE, summary="Clean."),
                        None, "a.docx", [], [])
    assert "clean copy" not in out.text_body


# --- the contact card ---------------------------------------------------------


def test_the_contact_card_is_a_vcard_a_phone_will_import():
    card = reply.contact_card("Second Eye", "review@legal.firm.com")
    assert card.filename == "Second Eye.vcf"
    assert card.content_type == "text/vcard"
    assert card.size_bytes == len(card.content) < 1024
    lines = card.content.decode().split("\r\n")
    assert lines[:2] == ["BEGIN:VCARD", "VERSION:3.0"]
    assert "FN:Second Eye" in lines
    assert "ORG:Second Eye" in lines
    assert "EMAIL;TYPE=INTERNET,PREF:review@legal.firm.com" in lines
    assert "NOTE:Forward or BCC documents here for a review before they go out." in lines
    assert lines[-2:] == ["END:VCARD", ""]
    assert all(len(line.encode()) <= 75 for line in lines)


def test_the_contact_card_escapes_and_folds_what_an_operator_might_name_it():
    card = reply.contact_card("Smith, Jones; Review/Desk " + "x" * 80, "r@firm.com")
    assert card.filename.startswith("Smith, Jones; Review Desk ")
    text = card.content.decode()
    assert "FN:Smith\\, Jones\\; Review/Desk " in text
    assert all(len(line.encode()) <= 75 for line in text.split("\r\n"))
    # Unfolded, the name is whole again.
    assert "x" * 80 in text.replace("\r\n ", "")


def test_the_card_never_costs_the_lawyer_their_redline():
    near_limit = reply.MAX_ATTACHMENT_BYTES - 10
    out = OutboundEmail(to=["jim@firm.com"], subject="Re: SPA", text_body="x", attachments=[
        Attachment(filename="SPA (redline).docx", content_type=reply.DOCX_TYPE,
                   size_bytes=near_limit, content=b"")])
    reply.attach_contact_card(out, "Second Eye", "review@firm.com")
    assert [a.filename for a in out.attachments] == ["SPA (redline).docx"]

    out.attachments[0].size_bytes = 1000
    reply.attach_contact_card(out, "Second Eye", "review@firm.com")
    assert [a.filename for a in out.attachments] == ["SPA (redline).docx", "Second Eye.vcf"]


def test_setup_points_at_the_attached_card():
    text = reply.setup_text("review@firm.com")
    assert "Tap the attached card to save me as a contact; then BCC autocompletes." in text
    assert "so it autocompletes" not in text


# --- the footer offers the whole job in one reply, only when it is ---------------

def _ask(title, options, severity=Severity.BLOCKER):
    return Finding(severity=severity, category="amount", title=title, explanation="",
                   anchor=title, question=f"{title}?", options=options)


def _footer_of(findings, applied=()):
    out = reply.compose(inbound(), ReviewResult(mode=Mode.REDLINE, summary="",
                                                findings=list(findings) + list(applied)),
                        b"docx" if applied else None, "a.docx", list(applied), [],
                        numbers=[i + 1 for i in range(len(applied))])
    return out.text_body.rstrip().splitlines()[-1]


def test_answers_and_clean_copy_are_offered_as_one_reply():
    assert _footer_of([_ask("thirty (13)", ["30", "13"])]) == (
        'Reply "30; clean copy" for a clean copy with your answers in, '
        'or "30" to see them tracked first.')


def test_with_changes_made_the_undo_goes_in_the_same_reply():
    fix = Finding(severity=Severity.FORMATTING, category="typo", title="Reciever",
                  explanation="", anchor="Reciever", suggested_text="Recipient",
                  auto_apply=True)
    assert _footer_of([_ask("thirty (13)", ["30", "13"])], [fix]) == (
        'Reply "30; clean copy" for a clean copy with your answers in. '
        'Put "undo 1" first to drop one of my changes.')


def test_no_one_reply_offer_when_answers_would_not_make_it_sendable():
    """A blocker no answer settles, or a question with no options to type,
    means a clean copy after the answers is still not ready to send."""
    unanswerable = blocker()
    assert "with your answers in" not in _footer_of([_ask("thirty (13)", ["30", "13"]),
                                                unanswerable])
    assert "with your answers in" not in _footer_of([_ask("which bank", [])])
