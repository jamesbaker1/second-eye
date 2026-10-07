"""What leaves the firm: the file, its name and the note, checked together.

Half of these assert silence. The checks on what is sent run on every message
a lawyer BCCs, so the ordinary version of each mistake (a blackline beside
the clean copy, a draft that says DRAFT, a letter "to follow") has to pass
without a word.
"""

from __future__ import annotations

from datetime import UTC, datetime

from evals import outbound as build
from evals.outbound import NDA, SPA
from lra.models import Attachment, InboundEmail, Severity
from lra.pipeline import checks, email_checks, extract, outbound


def message(body: str, *files: tuple[str, bytes], to=("legal@acme.com",), cc=()):
    atts = [Attachment(filename=n, content_type="application/octet-stream",
                       size_bytes=len(c), content=c) for n, c in files]
    email = InboundEmail(message_id="m1", from_address="jim@firm.com", to=list(to),
                         cc=list(cc), subject="", text_body=body, attachments=atts,
                         received_at=datetime.now(UTC))
    return email, atts[0], extract.extract(atts[0])


def titles(findings) -> list[str]:
    return [f.title for f in findings]


def everything(body: str, *files, to=("legal@acme.com",)):
    email, att, doc = message(body, *files, to=to)
    return checks.run_all(doc, att.content) + email_checks.run_all(email, doc, att, list(to))


# --- what travels in a Word file -----------------------------------------

def test_properties_naming_another_client_are_reported():
    content = build.docx(NDA, title="Bluewater Shipping LLC - Mutual NDA",
                         custom={"ClientName": "Bluewater Shipping LLC"})
    found = outbound.package(content, "NDA.docx", "\n".join(NDA))
    assert titles(found) == [
        "The file's properties name Bluewater Shipping LLC, which the document never mentions"]
    assert "title property" in found[0].explanation
    assert found[0].severity is Severity.SUBSTANTIVE


def test_properties_naming_the_parties_or_the_firm_are_silent():
    content = build.docx(NDA, title="Acme Holdings LLC / Beta Industries Inc. - NDA",
                         custom={"ClientName": "Acme Holdings LLC", "Matter": "40412-0007",
                                 "Author": "Smith & Jones LLP"})
    assert outbound.package(content, "NDA.docx", "\n".join(NDA)) == []


def test_an_embedded_workbook_and_a_linked_file_are_reported():
    embedded = outbound.package(build.docx(NDA, embedded=True), "NDA.docx", "")
    assert titles(embedded) == ["The file carries an embedded spreadsheet"]
    linked = outbound.package(
        build.docx(NDA, linked="\\\\fileserver\\Clients\\Bluewater\\fees.xlsx"), "NDA.docx", "")
    assert titles(linked) == [
        "The file links to a file on your network: \\\\fileserver\\Clients\\Bluewater\\fees.xlsx"]


def test_a_web_link_is_not_a_linked_file():
    assert outbound.package(build.docx(NDA, hyperlink="https://www.acme.com/terms"),
                            "NDA.docx", "\n".join(NDA)) == []


def test_the_company_property_joins_the_author_line():
    found = checks.leftovers(build.docx(NDA, author="Priya Associate", company="Smith LLP"))
    assert titles(found) == [
        "Author metadata identifies Priya Associate, Smith LLP (as company)"]
    assert found[0].severity is Severity.STYLE


# --- decks, workbooks, PDFs ----------------------------------------------

def test_speaker_notes_and_hidden_slides_are_reported_by_slide():
    content = build.deck(["One", "Two", "Three"], notes={2: "Do not mention the bid."},
                         hidden=(3,))
    assert titles(outbound.package(content, "deck.pptx")) == [
        "Speaker notes are on slide 2", "Slide 3 is hidden in the deck"]
    assert outbound.package(build.deck(["One", "Two"]), "deck.pptx") == []


def test_hidden_sheets_and_comments_in_a_workbook_are_reported():
    content = build.workbook(hidden_sheet="Write-offs", comment="Partner to approve")
    assert titles(outbound.package(content, "fees.xlsx")) == [
        'The workbook has a hidden sheet: "Write-offs"',
        "1 comment is still in the workbook",
    ]
    assert outbound.package(build.workbook(), "fees.xlsx") == []


def test_pdf_markup_attachments_and_properties():
    content = build.pdf(["Exhibit A"], sticky="Do we have to list these?", highlight=True,
                        attached=True, author="Priya Associate",
                        title="Microsoft Word - Bluewater NDA v2.docx")
    found = outbound.package(content, "Exhibit A.pdf", "Exhibit A")
    assert titles(found) == [
        "The PDF still carries mark-up: 1 comment and 1 highlight",
        "The PDF has a file attached inside it: fee workings.xlsx",
        ("The PDF's properties name Priya Associate as its author and the file it was made "
         'from, "Microsoft Word - Bluewater NDA v2.docx"'),
    ]
    assert found[0].severity is Severity.BLOCKER
    assert "Bob Partner" in found[0].explanation


def test_a_plain_pdf_export_is_silent():
    assert outbound.package(build.pdf(["Exhibit A"], title="Exhibit A"), "a.pdf",
                            "Exhibit A") == []


# --- every attachment, not just the one reviewed -------------------------

def test_the_other_attachments_are_checked_and_named():
    email, att, _ = message(
        "Here are the NDA and the exhibit.", ("NDA.docx", build.docx(NDA)),
        ("Exhibit A.pdf", build.pdf(["Exhibit A"], sticky="Too long?")),
        ("Comments.docx", build.docx(NDA, comments=[("Bob Partner", "Push back")])))
    assert titles(email_checks.other_attachments(email, att)) == [
        "In Exhibit A.pdf: the PDF still carries mark-up: 1 comment",
        "In Comments.docx: 1 internal comment(s) are still in the document",
    ]


def test_a_blackline_beside_the_clean_copy_is_tracked_changes_by_design():
    email, att, _ = message(
        "Attached are a clean version and a blackline.", ("NDA v4.docx", build.docx(NDA)),
        ("NDA v4 (blackline against v3).docx", build.docx(NDA, inserted="and affiliates")))
    assert email_checks.other_attachments(email, att) == []


# --- sent as final, still a draft ----------------------------------------

def test_an_execution_version_that_still_says_draft_is_a_blocker():
    found = everything("Please find attached the execution version of the SPA.",
                       ("Acme SPA.docx", build.docx(SPA, header="DRAFT 3: March 1, 2026")))
    (f,) = [f for f in found if f.category == "draft"]
    assert f.severity is Severity.BLOCKER
    assert f.title == "This is going out as final, but it is still marked as a draft"
    assert 'a header ("DRAFT 3: March 1, 2026")' in f.explanation
    assert "execution version" in f.explanation


def test_final_by_its_file_name_with_a_watermark():
    found = everything("Here it is.", ("Acme SPA (execution version).docx",
                                       build.docx(SPA, watermark="DRAFT")))
    (f,) = [f for f in found if f.category == "draft"]
    assert "a header (as a watermark)" in f.explanation
    assert "named Acme SPA (execution version).docx" in f.explanation


def test_a_highlight_left_in_a_final_version():
    found = everything("Attached is the final version for signature.",
                       ("Acme SPA.docx", build.docx(SPA, highlighted="Completion is 31 March.")))
    assert [f.title for f in found if f.category == "draft"] == [
        'Text is still highlighted in a version going out as final: "Completion is 31 March."']


def test_a_draft_called_a_draft_is_silent():
    """"Final draft" is still a draft, and a DRAFT watermark on one is right."""
    found = everything("Attached is the final draft for your comments.",
                       ("Acme NDA.docx", build.docx(NDA, header="DRAFT 3: March 1, 2026",
                                                    watermark="DRAFT")))
    assert [f for f in found if f.category == "draft"] == []


def test_an_execution_version_promised_for_later_is_not_this_one():
    found = everything("The execution version will follow once we have your comments.",
                       ("Acme SPA.docx", build.docx(SPA, header="DRAFT")))
    assert [f for f in found if f.category == "draft"] == []


# --- clean by name or by note --------------------------------------------

def test_a_file_named_clean_with_markup_in_it():
    email, att, _ = message("Here is the NDA.", ("Acme NDA v3 clean.docx",
                                                 build.docx(NDA, deleted="and affiliates")))
    (f,) = email_checks.clean_by_name(email, att)
    assert f.title == "The file is called Acme NDA v3 clean.docx, but it still has tracked changes"
    assert f.severity is Severity.BLOCKER


def test_comments_in_a_copy_called_clean():
    email, att, doc = message("Attached is a clean copy.", ("NDA.docx", build.docx(
        NDA, comments=[("Bob", "ok?")])))
    assert titles(email_checks.claim_mismatch(email, doc, att.content)) == [
        "You called this a clean copy, but it still has 1 comment"]


def test_a_clean_file_called_clean_is_silent():
    email, att, _ = message("Attached is v4, clean.", ("Acme NDA v4 clean.docx", build.docx(NDA)))
    assert email_checks.clean_by_name(email, att) == []


# --- which version ----------------------------------------------------------

def test_the_note_says_one_version_and_the_file_name_another():
    email, att, doc = message("Attached is v4 of the NDA.", ("Acme NDA v3.docx", build.docx(NDA)))
    assert titles(email_checks.version_mismatch(email, doc, att)) == [
        "Your note says version 4, but the file attached is Acme NDA v3.docx"]


def test_the_note_against_the_document_systems_footer_stamp():
    email, att, doc = message("Here is the fourth draft.",
                              ("Acme NDA.docx", build.docx(NDA, footer="NY-4455667v3")))
    assert titles(email_checks.version_mismatch(email, doc, att)) == [
        "Your note says version 4, but its footer says version 3"]


def test_two_versions_in_one_sentence_say_nothing_about_the_attachment():
    email, att, doc = message("Attached is v4, which picks up your comments on v3.",
                              ("Acme NDA v4.docx", build.docx(NDA)))
    assert email_checks.version_mismatch(email, doc, att) == []
    email, att, doc = message("Attached is v4 of the NDA.", ("Acme NDA v4.docx", build.docx(NDA)))
    assert email_checks.version_mismatch(email, doc, att) == []


def test_a_version_in_quoted_history_is_not_the_lawyers():
    email, att, doc = message(
        "Attached is v4.\n\nOn Mon, 2 Mar 2026 at 09:12, Bob <bob@beta.com> wrote:\n"
        "> Here is v3.", ("Acme NDA v4.docx", build.docx(NDA)))
    assert email_checks.version_mismatch(email, doc, att) == []


# --- the note names somebody else, or something not attached ---------------

def test_the_note_names_a_company_the_document_does_not():
    email, att, doc = message("Attached is the NDA with Bluewater Shipping LLC.",
                              ("NDA.docx", build.docx(NDA)))
    (f,) = email_checks.party_in_note_not_in_document(email, doc, att)
    assert f.title == "Your note mentions Bluewater Shipping LLC, but the attached document never does"
    assert "Acme Holdings LLC and Beta Industries Inc." in f.explanation


def test_parties_law_firms_and_banks_mentioned_in_passing_are_silent():
    for body in (
        "Attached is the NDA between Acme Holdings LLC and Beta Industries Inc.",
        "Attached is the NDA we discussed with Smith & Jones LLP.",
        "Attached is the NDA. Harbour Bank plc will need to see it before signing.",
    ):
        email, att, doc = message(body, ("NDA.docx", build.docx(NDA)))
        assert email_checks.party_in_note_not_in_document(email, doc, att) == [], body


def test_a_companion_document_the_note_promises_and_the_message_lacks():
    email, att, doc = message("Attached are the SPA and the disclosure letter.",
                              ("Acme SPA.docx", build.docx(SPA)))
    assert titles(email_checks.promised_attachment_missing(email, doc, att)) == [
        "Your note says the disclosure letter is attached, but it is not"]


def test_a_sentence_a_mail_client_wrapped_is_read_whole():
    email, att, doc = message("Attached is the execution version of the SPA (v5), with the\n"
                              "disclosure letter.\n\nJim", ("Acme SPA v5.docx", build.docx(SPA)))
    assert titles(email_checks.promised_attachment_missing(email, doc, att)) == [
        "Your note says the disclosure letter is attached, but it is not"]


def test_companions_that_are_attached_or_to_follow_are_silent():
    for body, files in (
        ("Attached are the SPA and the disclosure letter.",
         [("Acme SPA.docx", build.docx(SPA)), ("Disclosure Letter.docx", build.docx(SPA))]),
        ("Attached is the SPA. The disclosure letter will follow separately.",
         [("Acme SPA.docx", build.docx(SPA))]),
        ("Attached are a clean version and a blackline.",
         [("SPA.docx", build.docx(SPA)), ("SPA (compare).docx", build.docx(SPA))]),
        ("Thanks for the blackline you sent yesterday; attached is our reply.",
         [("SPA.docx", build.docx(SPA))]),
    ):
        email, att, doc = message(body, *files)
        assert email_checks.promised_attachment_missing(email, doc, att) == [], body


def test_an_internal_marking_on_a_message_going_out():
    email, att, doc = message("Please find attached our comments.",
                              ("NDA.docx", build.docx(NDA, header="INTERNAL DRAFT")))
    (f,) = email_checks.internal_marking_going_out(email, doc, att, ["legal@acme.com"])
    assert f.title == 'The document is marked "INTERNAL DRAFT" and this is going to legal@acme.com'
    assert email_checks.internal_marking_going_out(email, doc, att, []) == []



def test_the_eval_meets_its_target():
    """The outbound half of `python -m evals.run_checks`, held here so a change
    that costs a catch or adds a false positive fails the suite."""
    from evals.run_checks import outbound as score

    caught, planted, false_positives, _ = score()
    assert (caught, false_positives) == (planted, 0)
