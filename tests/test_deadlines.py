"""The dates-and-deadlines table, and the contradictions it can prove.

The table is informational, so what matters is that it reads the document the
way a lawyer would and says so in the lawyer's words. The conflicts are
findings on documents that run through every check, so half of what follows
asserts silence: on the clean corpus, and on drafting that looks like a
conflict and is not one.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from evals.corpus import CLEAN, by_name
from secondeye.models import Attachment, InboundEmail, Mode, ReviewResult, Severity
from secondeye.pipeline import checks, deadlines, extract, reply

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def text_doc(*paragraphs):
    raw = "\n".join(paragraphs).encode()
    return extract.extract(Attachment(filename="t.txt", content_type="text/plain",
                                      size_bytes=len(raw), content=raw))


def spec_doc(spec):
    content = spec.build()
    return extract.extract(Attachment(filename=f"{spec.name}.docx", content_type=DOCX,
                                      size_bytes=len(content), content=content))


def table(*paragraphs):
    return {(r.what, r.when, r.date) for r in deadlines.rows(text_doc(*paragraphs))}


OPENING = ("This Agreement is dated 2 February 2026 and is made between Kestrel "
           "Components Limited and Marchmont Retail Limited.")


# --- reading the document -----------------------------------------------------

def test_each_kind_of_time_limit_is_read_in_plain_english():
    rows = table(
        OPENING,
        "1. Definitions. “Long Stop Date” means 31 March 2027.",
        "2. Term. This Agreement continues for an initial term of twenty-four (24) "
        "months (the “Initial Term”) and automatically renews for successive 12-month "
        "periods unless either party gives not less than 90 days' notice before the "
        "end of the Initial Term.",
        "3. Payment. The Customer shall pay each invoice within 10 Business Days "
        "after receipt.",
        "4. Breach. Either party may terminate if a material breach is not remedied "
        "within thirty (30) days after written notice.",
    )
    assert ("Date of this agreement", "2 February 2026", "") in rows
    assert ("Long Stop Date", "31 March 2027", "") in rows
    assert ("Initial term", "twenty-four (24) months", "") in rows
    assert ("Renewal period", "12-month periods", "") in rows
    assert ("Notice to stop renewal",
            "not less than 90 days' notice before the end of the Initial Term", "") in rows
    assert ("Payment due", "within 10 Business Days after receipt", "") in rows
    assert ("Time to remedy a breach", "within thirty (30) days after written notice",
            "") in rows


def test_every_what_is_eight_words_or_fewer():
    for spec in CLEAN:
        for row in deadlines.rows(spec_doc(spec)):
            assert len(row.what.split()) <= 8, row


def test_rows_carry_the_clause_they_are_in():
    rows = deadlines.rows(text_doc(
        OPENING, "4. Payment. The Customer shall pay within thirty (30) days of receipt."))
    assert [(r.where, r.what) for r in rows] == [
        ("", "Date of this agreement"), ("clause 4", "Payment due")]


def test_a_period_from_a_date_the_document_states_is_computed():
    rows = table(
        "This Agreement is made as of March 3, 2026 between Acme LLC and Beta Inc.",
        "4. Term. This Agreement continues for three (3) years from the date first "
        "written above.",
    )
    # In the document's own style, American here.
    assert ("Term ends", "for three (3) years from the date first written above",
            "March 3, 2029") in rows


def test_a_term_beginning_on_a_date_ends_the_day_before_its_anniversary():
    rows = table(
        "This Lease is made on 1 May 2026 between Kestrel Limited and Marchmont Limited.",
        "3. Term. The “Term” means ten (10) years beginning on 1 June 2026.",
        "5. Rent Review. The Rent is reviewed on the fifth anniversary of the start "
        "of the Term.",
    )
    assert ("Term ends", "ten (10) years beginning on 1 June 2026", "31 May 2036") in rows
    assert ("Review date", "on the fifth anniversary of the start of the Term",
            "1 June 2031") in rows


def test_months_land_on_the_last_day_when_the_month_is_shorter():
    rows = table(
        "This Agreement is dated 31 January 2026 between Kestrel Limited and Beta Limited.",
        "2. Delivery. The Supplier shall deliver within one (1) month after the date "
        "of this agreement.",
    )
    assert ("Time limit", "within one (1) month after the date of this agreement",
            "28 February 2026") in rows


def test_business_days_are_never_turned_into_a_date():
    """Which days are holidays depends on where, and the document rarely says."""
    rows = table(OPENING, "2. The Buyer shall pay within 10 Business Days after the "
                          "date of this agreement.")
    assert ("Payment due", "within 10 Business Days after the date of this agreement",
            "") in rows


def test_a_period_whose_words_and_figures_disagree_gets_no_date():
    rows = table(
        "This Agreement is dated 2 February 2026 between Kestrel Limited and Beta Limited.",
        "2. The Agreement continues for thirty (13) months from the date of this agreement.",
    )
    assert [date for _, when, date in rows if "thirty" in when] == [""]


def test_quantities_that_are_not_time_limits_are_left_out():
    rows = table(
        "The Executive, who is 45 years old, has 20 years' experience.",
        "The accounts cover the 12 months to 31 December 2025.",
        "The Company has 1,000 days of stock.",
    )
    assert rows == set()


# --- what a clause is: the labels the Falcon rehearsal got wrong ----------------

FALCON_OPENING = "THIS AGREEMENT is dated 30 October 2026"


def kinds(*paragraphs):
    return {r.when: (r.kind, r.what) for r in deadlines.rows(text_doc(*paragraphs))}


def test_the_sellers_solicitors_are_not_a_non_solicit():
    """"solicit" matched inside "Solicitors", so where Completion takes place
    read as a restrictive covenant."""
    got = kinds(FALCON_OPENING,
                "6.1 Completion shall take place at the offices of the Seller's Solicitors "
                "on the fifth Business Day after the date on which the last of the "
                "Conditions is satisfied.")
    assert got["on the fifth Business Day after the date"] == ("completion", "Completion step")


@pytest.mark.parametrize("clause", [
    "11.2 The Seller shall not, for 12 months after Completion, solicit any customer.",
    ("11.2 The Seller shall not, for 12 months after Completion, be engaged in any "
     "Restricted Business."),
    "11.2 The Seller shall not, for 12 months after Completion, compete with the Company.",
    ("11.2 For 12 months after Completion the Seller shall not engage in any "
     "non-solicitation of staff."),
])
def test_a_real_restrictive_covenant_is_still_one(clause):
    got = kinds(FALCON_OPENING, clause)
    [kind] = [k for when, (k, _) in got.items() if "12 months after Completion" in when]
    assert kind == "restrictive"


def test_a_non_compete_running_from_completion_is_not_a_completion_step():
    """Completion there is the event the covenant runs from, not a step in it."""
    got = kinds(FALCON_OPENING,
                "11.1 The Seller shall not, for a period of 24 months after Completion, "
                "carry on or be engaged, concerned or interested in any Restricted Business.")
    assert got["for a period of 24 months after Completion"] == (
        "restrictive", "Restrictive covenant period")


def test_a_competent_authority_is_not_a_non_compete():
    got = kinds(FALCON_OPENING,
                "8.1 The Buyer shall pay any fine imposed by a competent authority within "
                "10 Business Days after demand.")
    assert got["within 10 Business Days after demand"][0] == "payment"


def test_the_long_stop_clause_is_the_long_stop_date_not_a_time_limit_for_claims():
    """"no party shall have any claim against another" made the long stop a
    "Time limit for claims"."""
    doc = text_doc(FALCON_OPENING,
                   "4.4 If the Conditions have not been satisfied on or before 31 March 2027 "
                   "(the “Long Stop Date”), either the Seller or the Buyer may terminate "
                   "this agreement by notice to the other, and no party shall have any "
                   "claim against another under it.")
    [row] = [r for r in deadlines.rows(doc) if r.kind != "agreement-date"]
    assert (row.kind, row.what) == ("long-stop", "Long Stop Date")


def test_a_long_stop_named_in_its_clause_is_checked_against_the_agreement_date():
    found = deadlines.conflicts(text_doc(
        FALCON_OPENING,
        "4.4 If the Conditions have not been satisfied on or before 31 March 2026 "
        "(the “Long Stop Date”), either party may terminate this agreement."))
    assert [f.title for f in found] == ["The Long Stop Date is before the date of this agreement"]


def test_a_historic_reference_date_is_not_a_deadline():
    """The Accounts Date is the date the accounts were drawn up to, before
    signing by design. It went into the calendar as a deadline and was
    flagged as a date carried over from an earlier draft."""
    from secondeye.pipeline import ics, timeline

    doc = text_doc(FALCON_OPENING,
                   "1.1 “Accounts Date” means 31 March 2026.",
                   "1.2 “Long Stop Date” means 31 March 2027.")
    assert [e.what for e in ics.read(doc).dated] == ["Long Stop Date"]
    assert timeline.build(doc)["flags"] == []
    accounts = next(r for r in deadlines.rows(doc) if r.what == "Accounts Date")
    assert accounts.kind == "reference"


def test_an_accounts_date_is_a_reference_even_with_no_agreement_date():
    from secondeye.pipeline import ics

    doc = text_doc("THIS AGREEMENT is dated [●] 2026",
                   "1.1 “Accounts Date” means 31 March 2026.",
                   "1.2 “Locked Box Date” means 30 June 2026.",
                   "1.3 “Long Stop Date” means 31 March 2027.")
    assert [e.what for e in ics.read(doc).dated] == ["Long Stop Date"]


def test_a_deadline_dated_before_signing_is_still_flagged():
    """Only reference dates are let off. A Completion Date before the
    agreement is the stale date the flag exists for."""
    from secondeye.pipeline import timeline

    doc = text_doc(FALCON_OPENING, "1.1 “Completion Date” means 30 September 2026.")
    assert [f["kind"] for f in timeline.build(doc)["flags"]] == ["date-before-signing"]


def test_the_claims_window_is_checked_against_an_escrow_held_by_solicitors():
    """With the escrow clause read as a non-solicit (it names the Seller's
    Solicitors), the claims-window-versus-release check never saw it."""
    from secondeye.pipeline import timeline

    doc = text_doc(FALCON_OPENING,
                   "7.3 The Escrow Amount shall be held by the Seller's Solicitors and "
                   "released to the Seller six months after Completion.",
                   "12.2 No claim may be made against the Escrow Amount unless notified "
                   "to the Seller within 90 days after Completion.")
    assert [f["kind"] for f in timeline.build(doc)["flags"]] == ["claims-before-release"]


def test_a_scan_has_no_table():
    doc = text_doc(OPENING, "2. Pay within thirty (30) days of receipt.")
    doc.transcribed = True
    assert deadlines.rows(doc) == [] and deadlines.conflicts(doc) == []


# --- conflicts ------------------------------------------------------------------

def conflicts(*paragraphs):
    return deadlines.conflicts(text_doc(*paragraphs))


def test_notice_longer_than_the_period_it_must_precede():
    found = conflicts(
        OPENING,
        "2. Term. This Agreement renews for successive periods of three (3) months "
        "(each a “Renewal Period”) unless either party gives not less than six (6) "
        "months' written notice before the end of the then-current Renewal Period.",
    )
    assert [f.title for f in found] == [
        ("Six (6) months' notice is longer than the Renewal Period it must come "
         "before (three (3) months)")]
    assert found[0].severity is Severity.SUBSTANTIVE
    assert found[0].anchor == "six (6) months'"


@pytest.mark.parametrize("notice", [
    "ninety (90) days'",     # 90 days against 3 months (84 to 93 days): not certain
    "three (3) months'",     # equal: tight, but it can be given
    "one (1) month's",
])
def test_notice_that_fits_or_might_fit_is_silent(notice):
    assert conflicts(
        OPENING,
        "2. Term. This Agreement renews for successive periods of three (3) months "
        f"(each a “Renewal Period”) unless either party gives not less than {notice} "
        "written notice before the end of the then-current Renewal Period.",
    ) == []


def test_notice_to_terminate_at_any_time_is_not_measured_against_the_term():
    assert conflicts(
        OPENING,
        "2. Term. The initial term is three (3) months (the “Initial Term”).",
        "3. Termination. Either party may terminate on six (6) months' written notice.",
    ) == []


def test_long_stop_date_before_the_agreement_is_dated():
    found = conflicts(OPENING, "1. “Long Stop Date” means 31 December 2025.")
    assert len(found) == 1 and found[0].severity is Severity.BLOCKER
    assert found[0].anchor == "“Long Stop Date” means 31 December 2025"


def test_long_stop_date_after_signing_and_other_earlier_dates_are_silent():
    assert conflicts(
        OPENING,
        "1. “Long Stop Date” means 31 March 2027. “Accounts Date” means 31 December "
        "2025. “Effective Date” means 1 January 2026.",
    ) == []


def test_long_stop_date_with_no_agreement_date_is_silent():
    assert conflicts("1. “Long Stop Date” means 31 December 2025.") == []


def test_the_same_period_given_two_lengths():
    found = conflicts(
        OPENING,
        "2. Term. This Agreement continues for an initial term of twelve (12) months "
        "(the “Initial Term”).",
        "3. Extension. The Customer may extend the Initial Term of twenty-four (24) "
        "months once by notice.",
    )
    assert [f.title for f in found] == [
        ("The Initial Term is twelve (12) months in one place and twenty-four (24) "
         "months in another")]


def test_the_same_period_said_the_same_way_twice_is_silent():
    assert conflicts(
        OPENING,
        "2. Term. This Agreement continues for an initial term of twelve (12) months "
        "(the “Initial Term”).",
        "3. Extension. The Customer may extend the Initial Term of one (1) year once.",
        "4. Further. The Initial Term of 12 months may be extended once.",
    ) == []


def test_conflicts_run_with_every_other_check_and_are_placed():
    found = [f for f in checks.run_all(spec_doc(by_name("defect_deadlines")))
             if f.category == "deadline"]
    assert len(found) == 3
    assert {f.where for f in found} == {"clause 1", "clause 2", "clause 3"}


@pytest.mark.parametrize("spec", CLEAN, ids=lambda s: s.name)
def test_no_deadline_finding_on_any_clean_document(spec):
    assert [f.title for f in checks.run_all(spec_doc(spec))
            if f.category == "deadline"] == []


def test_the_clean_deadline_document_still_has_a_full_table():
    """Silence on the conflicts is not silence because nothing was read."""
    rows = deadlines.rows(spec_doc(by_name("clean_supply_deadlines")))
    assert len(rows) >= 8
    assert {"Notice to stop renewal", "Long Stop Date", "Renewal period",
            "Time to remedy a breach"} <= {r.what for r in rows}


# --- the reply ------------------------------------------------------------------

def inbound() -> InboundEmail:
    return InboundEmail(message_id="m1", from_address="jim@example.com",
                        subject="Supply agreement", received_at=datetime.now(UTC))


def compose(dates, **kw):
    result = ReviewResult(mode=Mode.REDLINE, summary="", findings=[])
    return reply.compose(inbound(), result, None, "a.docx", [], [], dates=dates, **kw)


def rows(n):
    return [deadlines.Row(i, f"clause {i}", "Payment due", f"within {i} days of receipt",
                          "4 March 2026" if i == 1 else "")
            for i in range(1, n + 1)]


def test_the_section_follows_the_findings_in_text_and_html():
    out = compose(rows(2))
    text = out.text_body
    assert "Dates and deadlines:\n" in text
    assert "  - Clause 1: Payment due — within 1 days of receipt (4 March 2026)\n" in text
    assert text.index("Ready to send") < text.index("Dates and deadlines")
    html = out.html_body
    assert "Dates and deadlines" in html and "<table" in html
    assert "<th" in html and "<style" not in html
    assert "<td style=" in html and ">Clause 1<" in html


def test_one_row_is_not_a_table():
    out = compose(rows(1))
    assert "Dates and deadlines" not in out.text_body
    assert "<table" not in out.html_body


def test_no_dates_changes_nothing():
    assert compose(None).text_body == compose([]).text_body == \
        reply.compose(inbound(), ReviewResult(mode=Mode.REDLINE, summary=""),
                      None, "a.docx", [], []).text_body


def test_a_long_table_is_capped():
    out = compose(rows(11))
    assert out.text_body.count("Payment due") == deadlines.SHOWN
    assert "  ... and 3 more.\n" in out.text_body
    assert "and 3 more" in out.html_body


def test_it_sits_above_the_footer():
    sections = deadlines.place([("verdict", "v"), ("list", "h", []), ("summary", "s"),
                                ("footer", "f", [])], rows(2))
    assert [s[0] for s in sections] == ["verdict", "list", "deadlines", "summary", "footer"]


def test_the_whole_path_carries_the_table(monkeypatch, tmp_path):
    from secondeye import handler
    from secondeye.config import settings
    from secondeye.mail.console import ConsoleProvider

    class Captured(ConsoleProvider):
        def __init__(self):
            self.sent = []

        def send(self, email_out):
            self.sent.append(email_out)
            return "captured"

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/d.sqlite3")
    monkeypatch.setenv("MAIL_AGENT_ADDRESS", "review@legal.firm.com")
    monkeypatch.setenv("FIRM_DOMAINS", "firm.com")
    settings.cache_clear()
    provider = Captured()
    monkeypatch.setattr(handler, "get_provider", lambda: provider)
    monkeypatch.setattr(handler.review, "review", lambda doc, mode, *a, **k: ReviewResult(
        mode=mode, summary="", findings=[]))
    raw = by_name("clean_supply_deadlines").build()
    try:
        handler.handle(InboundEmail(
            message_id="d-1", from_address="jim@firm.com", to=["review@legal.firm.com"],
            subject="Supply agreement", text_body="Quick look?",
            attachments=[Attachment(filename="Supply.docx", content_type=DOCX,
                                    size_bytes=len(raw), content=raw)],
            received_at=datetime.now(UTC)))
    finally:
        settings.cache_clear()
    body = provider.sent[0].text_body
    assert "Dates and deadlines:" in body
    assert "Clause 1: Long Stop Date — 31 March 2027" in body
    assert "and 3 more." in body
