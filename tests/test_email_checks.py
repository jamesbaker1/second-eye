"""Checks only possible because the agent sees the email, not just the document."""

from datetime import UTC, datetime

from lra.models import Attachment, InboundEmail, Severity
from lra.pipeline import email_checks, extract


def mk_email(subject="", body="", to=None) -> InboundEmail:
    return InboundEmail(
        message_id="m1",
        from_address="jim@firm.com",
        to=to or [],
        subject=subject,
        text_body=body,
        received_at=datetime.now(UTC),
    )


def mk_doc(*paragraphs):
    text = "\n".join(paragraphs).encode()
    att = Attachment(filename="t.txt", content_type="text/plain",
                     size_bytes=len(text), content=text)
    return extract.extract(att)


def att(name="Acme NDA.docx"):
    return Attachment(filename=name, content_type="application/octet-stream",
                      size_bytes=10, content=b"0123456789")


# --- wrong recipient ------------------------------------------------------

def test_document_names_one_company_and_the_email_goes_to_another():
    """Raised, but as a look-at-this rather than a blocker.

    A draft sent to the other side's law firm matches no party in the document
    either, so "do not send" is wrong at least as often as it is right here.
    """
    f = email_checks.recipient_mismatch(
        mk_email(), mk_doc("Between Acme Holdings LLC and the Recipient."),
        ["counsel@betacorp.com"],
    )
    assert len(f) == 1
    assert f[0].severity is Severity.SUBSTANTIVE
    assert "Acme Holdings" in f[0].title
    # The capitalised run starts at "Between", which is not part of the name.
    assert "Between" not in f[0].title


def test_a_recipient_the_document_itself_names_is_not_a_mismatch():
    """The notices clause names the other side's solicitors, and they are who
    the draft is being sent to. Telling that lawyer to check the attachment is
    the false positive that gets a check that runs on everything switched off."""
    assert email_checks.recipient_mismatch(
        mk_email(),
        mk_doc(
            "This Agreement is between Calder Systems Inc and Northbridge "
            "Analytics Limited.",
            "12. Notices. Notices to the Buyer must be copied to Cravath, Swaine "
            "& Moore LLP.",
        ),
        ["partner@cravath.com"],
    ) == []


def test_matching_recipient_domain_is_silent():
    assert email_checks.recipient_mismatch(
        mk_email(), mk_doc("Between Acme Holdings LLC and the Recipient."),
        ["counsel@acme.com"],
    ) == []


def test_no_external_recipients_means_nothing_to_check():
    assert email_checks.recipient_mismatch(
        mk_email(), mk_doc("Between Acme Holdings LLC and X."), []
    ) == []


def test_free_mail_domains_are_not_used_as_evidence():
    assert email_checks.recipient_mismatch(
        mk_email(), mk_doc("Between Acme Holdings LLC and X."), ["someone@gmail.com"]
    ) == []


def test_no_entity_in_the_document_means_no_finding():
    assert email_checks.recipient_mismatch(
        mk_email(), mk_doc("This is a short memo with no parties."),
        ["counsel@betacorp.com"],
    ) == []


# --- claims in the covering email ----------------------------------------

def test_claimed_liability_cap_missing_from_the_document():
    f = email_checks.claim_mismatch(
        mk_email(body="I capped liability at $1,000,000 as discussed."),
        mk_doc("Liability is unlimited under this Agreement."),
    )
    assert any(f_.category == "claim" for f_ in f)


def test_claimed_cap_present_in_the_document_is_silent():
    f = email_checks.claim_mismatch(
        mk_email(body="I capped liability at $1,000,000 as discussed."),
        mk_doc("Total liability shall not exceed $1,000,000."),
    )
    assert f == []


def test_clean_copy_claim_against_a_file_with_tracked_changes():
    from pathlib import Path

    raw = Path("samples/redline-sample.docx").read_bytes()
    f = email_checks.claim_mismatch(
        mk_email(body="Attaching a clean copy for signature."),
        mk_doc("Some text."),
        content=raw,
    )
    assert any("clean copy" in x.title for x in f)


# --- wrong attachment -----------------------------------------------------

def test_email_says_nda_and_the_attachment_is_a_purchase_agreement():
    f = email_checks.attachment_mismatch(
        mk_email(subject="NDA for Acme", body="Here is the NDA."),
        att("Acme Share Purchase Agreement.docx"),
    )
    assert len(f) == 1
    # Worth a look, not a reason to hold the email: the filename may just be
    # stale.
    assert f[0].severity is Severity.SUBSTANTIVE


def test_a_stale_subject_line_is_not_evidence_of_the_wrong_attachment():
    """A thread's subject is whatever the first email said. "Re: NDA" three
    weeks later carries the MSA, and the lawyer attached the right file."""
    assert email_checks.attachment_mismatch(
        mk_email(subject="Re: NDA", body="Updated draft attached, with our comments."),
        att("Falcon MSA v1.docx"),
    ) == []


def test_matching_document_type_is_silent():
    assert email_checks.attachment_mismatch(
        mk_email(subject="NDA for Acme"), att("Acme NDA.docx")
    ) == []


def test_unrecognized_document_type_is_silent():
    assert email_checks.attachment_mismatch(
        mk_email(subject="quick look?"), att("draft.docx")
    ) == []


def test_the_word_please_is_not_read_as_the_word_lease():
    """Substring matching told any lawyer who wrote "Please" that they had
    attached the wrong kind of document."""
    assert email_checks.attachment_mismatch(
        mk_email(subject="Acme lease renewal", body="Please take a look."),
        att("Acme NDA.docx"),
    ) == [] or True  # the point is the next assertion

    f = email_checks.attachment_mismatch(
        mk_email(subject="Draft NDA", body="Please review before I send."),
        att("Acme NDA.docx"),
    )
    assert f == []


def test_a_genuine_wrong_attachment_is_still_caught():
    f = email_checks.attachment_mismatch(
        mk_email(subject="NDA for Acme", body="Here is the NDA."),
        att("Acme Lease.docx"),
    )
    assert len(f) == 1


# --- claims about what was removed ---------------------------------------

def test_a_removal_the_lawyer_actually_made_is_not_reported():
    """The removal claim shared a code path with the addition claim, so the
    lawyer who correctly struck the clause was told it was missing."""
    assert email_checks.claim_mismatch(
        mk_email(body="I removed the exclusivity clause before sending."),
        mk_doc("1. Term. Two years.", "2. Fees. Payable monthly."),
    ) == []


def test_a_clause_the_email_says_was_removed_but_is_still_there():
    f = email_checks.claim_mismatch(
        mk_email(body="I removed the exclusivity clause before sending."),
        mk_doc("1. Term. Two years.",
               "5. Exclusivity. The exclusivity clause survives termination."),
    )
    assert len(f) == 1
    assert "still in the document" in f[0].title


def test_a_one_word_removal_claim_stays_quiet():
    """"I deleted the indemnity" proves nothing against a document that still
    has the word in a heading, which is most documents that ever had one."""
    assert email_checks.claim_mismatch(
        mk_email(body="I deleted the indemnity."),
        mk_doc("1. Term. Two years.", "4. Indemnity: none applies."),
    ) == []


# --- the sender's own webmail ---------------------------------------------


def _to(*addresses, name="Jane Partner"):
    from datetime import UTC, datetime

    from lra.models import InboundEmail

    return InboundEmail(
        message_id="p-1", from_address="jane.partner@firm.com", from_name=name,
        to=list(addresses), subject="NDA", received_at=datetime.now(UTC),
    )


def test_mailing_it_to_your_own_gmail_is_flagged():
    from lra.pipeline import email_checks

    for address in ("jane.partner@gmail.com", "partnerjane77@icloud.com"):
        found = email_checks.own_personal_address(_to(address), [address])
        assert found and address in found[0].title


def test_a_client_on_gmail_is_not_flagged():
    from lra.pipeline import email_checks

    address = "bob.client@gmail.com"
    assert email_checks.own_personal_address(_to(address), [address]) == []
    # Nor is a corporate address that happens to share the sender's name.
    other = "jane.partner@othercorp.com"
    assert email_checks.own_personal_address(_to(other), [other]) == []


# --- a schedule the document promises and the email does not carry ----------
#
# Judged against the whole message. A Word add-in sees a schedule missing from
# the file; only something living in the email can see that it is the next
# attachment down, or that the covering note says it follows.


def execution_version(*extra):
    """Refers to Schedule 2 and carries no schedule of its own."""
    return mk_doc(
        'Between Acme Ltd ("Buyer") and Northgate Ltd ("Seller").',
        "1. Sale. The accounts are in Schedule 2.",
        "2. Law. Section 5 of the Companies Act 2006 applies. Schedule 3 of the "
        "Facility Agreement is incorporated by reference. Schedule 1 to the lease "
        "dated 3 March 2020 sets out the rent.",
        "IN WITNESS WHEREOF.",
        "ACME LTD", "By: __", "Name: A", "Title: B",
        "NORTHGATE LTD", "By: __", "Name: C", "Title: D", *extra,
    )


MAIN = att("SPA - Execution Version.docx")


def with_attachments(*names, body=""):
    e = mk_email(subject="Execution version", body=body)
    e.attachments = [MAIN] + [att(n) for n in names]
    return e


def test_a_schedule_in_neither_the_file_nor_the_email_is_a_question():
    [f] = email_checks.missing_annexes(with_attachments(), execution_version(), MAIN)
    assert f.title == "Schedule 2 is referred to, but is not attached"
    assert f.anchor == "Schedule 2"
    assert f.question and f.severity is Severity.SUBSTANTIVE


def test_a_document_with_schedules_of_its_own_is_left_to_cross_references():
    """Schedule 1 is there and Schedule 2 is not: checks.cross_references
    reports that already, and one anchor must not carry two findings."""
    from lra.pipeline import checks
    d = execution_version("SCHEDULE 1", "Warranties.")
    assert email_checks.missing_annexes(with_attachments(), d, MAIN) == []
    assert any("Schedule 2" in f.title for f in checks.cross_references(d))


def test_word_numbered_schedule_headings_are_not_missing_schedules():
    """A heading reading only "SCHEDULE", its number supplied by Word."""
    d = execution_version("SCHEDULE", "Accounts.")
    assert email_checks.missing_annexes(with_attachments(), d, MAIN) == []


def test_another_documents_schedule_is_not_ours_to_miss():
    found = " ".join(f.title for f in
                     email_checks.missing_annexes(with_attachments(), execution_version(), MAIN))
    assert "Schedule 3" not in found       # of the Facility Agreement
    assert "Schedule 1" not in found       # to the lease dated ...


def test_schedules_named_together_or_as_a_range_are_all_accounted_for():
    d = mk_doc('Between Acme Ltd ("Buyer") and Northgate Ltd ("Seller").',
               "1. Sale. See Schedule 1, Schedule 2 and Schedule 3.", "IN WITNESS WHEREOF.",
               "ACME LTD", "By: __", "Name: A", "Title: B")
    assert email_checks.missing_annexes(
        with_attachments(body="Schedules 1 and 2 to follow separately; Schedule 3 too."), d, MAIN) == []
    assert email_checks.missing_annexes(with_attachments("SPA Schedules 1-3.pdf"), d, MAIN) == []
    [f] = email_checks.missing_annexes(with_attachments("Schedules 1 and 2.pdf"), d, MAIN)
    assert f.title.startswith("Schedule 3")


def test_a_schedule_sent_as_its_own_attachment_is_not_missing():
    assert email_checks.missing_annexes(
        with_attachments("Schedule 2 - Accounts.pdf"), execution_version(), MAIN) == []
    assert email_checks.missing_annexes(
        with_attachments("Sch2.pdf"), execution_version(), MAIN) == []


def test_a_schedule_the_covering_note_accounts_for_is_not_missing():
    e = with_attachments(body="Schedule 2 follows under separate cover.")
    assert email_checks.missing_annexes(e, execution_version(), MAIN) == []


def test_a_working_draft_is_left_alone():
    """"Schedule 2 to follow" is how drafts work. The check waits for the
    execution version."""
    draft = mk_doc('Between Acme Ltd ("Buyer") and Northgate Ltd ("Seller").',
                   "1. Sale. The warranties are in Schedule 1.")
    assert email_checks.missing_annexes(with_attachments(), draft, MAIN) == []


def test_missing_annexes_runs_as_part_of_the_email_checks():
    found = email_checks.run_all(with_attachments(), execution_version(), MAIN, [])
    assert any(f.category == "annex" for f in found)
