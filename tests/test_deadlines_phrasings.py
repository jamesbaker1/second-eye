"""The ways contracts state a dated or relative deadline, each with the row it
must give.

A production canary read "provide the Services from 1 November 2026", "ends on
31 October 2027" and "notice of non-renewal by 1 August 2027" as nothing, and
told the lawyer the document dated none of its deadlines. This is the corpus
of phrasings the extractor must read: commencement, expiry, notice, renewal,
payment, long stop, completion, option windows, rent review, in UK and US date
formats. Every dated row carries its ISO date, because that is what the
calendar file is built from. And the sentences that mention a date without
setting anything to do must stay silent.
"""

from __future__ import annotations

import io

import pytest
from docx import Document

from secondeye.models import Attachment
from secondeye.pipeline import deadlines, extract, ics

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

UK = "This Agreement is dated 1 October 2026 between Kestrel Limited and Beta Limited."
US = "This Agreement is made as of October 1, 2026 between Acme LLC and Beta Inc."


def text_doc(*paragraphs):
    raw = "\n".join(paragraphs).encode()
    return extract.extract(Attachment(filename="t.txt", content_type="text/plain",
                                      size_bytes=len(raw), content=raw))


def rows(*paragraphs):
    return [(r.what, r.when, r.iso) for r in deadlines.rows(text_doc(*paragraphs))
            if r.kind != "agreement-date"]


CASES = [
    # --- commencement ---
    ("from", UK, "1. Services. The Supplier shall provide the Services from 1 November 2026.",
     [("Start date", "from 1 November 2026", "2026-11-01")]),
    ("with effect from", UK, "2. The Consultant is appointed with effect from 1 November 2026.",
     [("Start date", "with effect from 1 November 2026", "2026-11-01")]),
    ("commencing on", UK, "2. The Licence is granted for a period commencing on 1 November 2026.",
     [("Start date", "commencing on 1 November 2026", "2026-11-01")]),
    ("shall commence on", UK, "2. The Term shall commence on 1 November 2026.",
     [("Start date", "shall commence on 1 November 2026", "2026-11-01")]),
    # --- expiry ---
    ("ends on", UK, ("4. Term. This Agreement ends on 31 October 2027 unless renewed under "
                    "clause 5."),
     [("Term ends", "ends on 31 October 2027", "2027-10-31")]),
    ("expires on", UK, "4. This Agreement expires on 31 October 2027.",
     [("Term ends", "expires on 31 October 2027", "2027-10-31")]),
    ("shall terminate on", UK, "4. This Agreement shall terminate on 31 October 2027.",
     [("Term ends", "shall terminate on 31 October 2027", "2027-10-31")]),
    ("until", UK, "4. This Agreement continues in force until 31 October 2027.",
     [("Term ends", "until 31 October 2027", "2027-10-31")]),
    # --- notice ---
    ("notice by", UK, "5. Renewal. Either party may give notice of non-renewal by 1 August 2027.",
     [("Notice to stop renewal", "by 1 August 2027", "2027-08-01")]),
    ("notice no later than", UK, ("9. The Customer must serve any notice to terminate no later "
                                 "than 1 August 2027."),
     [("Notice to terminate", "no later than 1 August 2027", "2027-08-01")]),
    ("notice on or before", UK, ("9. The Tenant shall give notice to the Landlord on or before "
                                "1 August 2027."),
     [("Notice deadline", "on or before 1 August 2027", "2027-08-01")]),
    ("not less than N months before a date", UK,
     ("9. Either party may terminate by giving not less than 3 months' notice before "
     "31 October 2027."),
     [("Notice to terminate", "not less than 3 months' notice before 31 October 2027",
       "2027-07-31")]),
    ("not less than N days before a US date", US,
     ("9. Either party may terminate by giving not less than 90 days' notice before "
     "October 31, 2027."),
     [("Notice to terminate", "not less than 90 days' notice before October 31, 2027",
       "2027-08-02")]),
    # --- renewal ---
    ("renews on", UK, ("5. This Agreement shall renew on 1 November 2027 unless either party "
                      "gives notice."),
     [("Renewal date", "on 1 November 2027", "2027-11-01")]),
    # --- payment ---
    ("pay on", UK, "3. The Buyer shall pay the Deferred Consideration on 1 December 2026.",
     [("Payment due", "on 1 December 2026", "2026-12-01")]),
    ("pay no later than", UK, "3. The Buyer shall pay the Price no later than 15 December 2026.",
     [("Payment due", "no later than 15 December 2026", "2026-12-15")]),
    ("instalments", UK, ("3. The Fee is payable in instalments on 1 January 2027, 1 April 2027 "
                        "and 1 July 2027."),
     [("Payment due", "on 1 January 2027", "2027-01-01"),
      ("Payment due", "on 1 April 2027", "2027-04-01"),
      ("Payment due", "on 1 July 2027", "2027-07-01")]),
    ("within N days after a date", UK, ("3. The Customer shall pay within 30 days after "
                                       "1 November 2026."),
     [("Payment due", "within 30 days after 1 November 2026", "2026-12-01")]),
    # --- long stop and completion ---
    ("long stop by lapse", UK, ("4. If Completion has not occurred by 31 March 2027, either "
                               "party may terminate this agreement."),
     [("Long stop date", "by 31 March 2027", "2027-03-31")]),
    ("long stop named", UK, ("4. If the Conditions have not been satisfied on or before "
                            "31 March 2027 (the “Long Stop Date”), either party may terminate."),
     [("Long Stop Date", "on or before 31 March 2027", "2027-03-31")]),
    ("completion on", UK, "6. Completion shall take place on 30 June 2027.",
     [("Completion date", "on 30 June 2027", "2027-06-30")]),
    # --- option windows ---
    ("option between", UK, ("7. The Option may be exercised at any time between 1 June 2027 "
                           "and 30 June 2027."),
     [("Option window opens", "between 1 June 2027", "2027-06-01"),
      ("Option window closes", "and 30 June 2027", "2027-06-30")]),
    ("option from until", UK, ("7. The Option is exercisable from 1 June 2027 until "
                              "30 June 2027."),
     [("Option window opens", "from 1 June 2027", "2027-06-01"),
      ("Option window closes", "until 30 June 2027", "2027-06-30")]),
    ("option by notice", UK, ("7. The Tenant may exercise the Option by notice no later than "
                             "31 December 2027."),
     [("Option exercise deadline", "no later than 31 December 2027", "2027-12-31")]),
    # --- rent review ---
    ("reviewed on", UK, "8. The Rent shall be reviewed on 25 March 2029.",
     [("Rent review date", "on 25 March 2029", "2029-03-25")]),
    ("review dates defined", UK, "1. “Review Dates” means 25 March 2029 and 25 March 2034.",
     [("Review Date", "25 March 2029", "2029-03-25"),
      ("Review Date", "25 March 2034", "2034-03-25")]),
    # --- date formats ---
    ("UK ordinal", UK, "2. The Term shall commence on 1st November 2026.",
     [("Start date", "shall commence on 1st November 2026", "2026-11-01")]),
    ("day of", UK, "2. The Term shall commence on the 1st day of November 2026.",
     [("Start date", "shall commence on the 1st day of November 2026", "2026-11-01")]),
    ("US", US, "2. The Term shall commence on November 1, 2026.",
     [("Start date", "shall commence on November 1, 2026", "2026-11-01")]),
    ("US ordinal", US, "2. The Term shall commence on November 1st, 2026.",
     [("Start date", "shall commence on November 1st, 2026", "2026-11-01")]),
    ("ISO", UK, "2. The Term shall commence on 2026-11-01.",
     [("Start date", "shall commence on 2026-11-01", "2026-11-01")]),
    ("UK slashes", UK, "2. The Term shall commence on 01/11/2026.",
     [("Start date", "shall commence on 01/11/2026", "2026-11-01")]),
    ("US slashes", US, "2. The Term shall commence on 11/01/2026.",
     [("Start date", "shall commence on 11/01/2026", "2026-11-01")]),
]


@pytest.mark.parametrize("opening,clause,expected", [c[1:] for c in CASES],
                         ids=[c[0] for c in CASES])
def test_each_phrasing_gives_its_row(opening, clause, expected):
    assert rows(opening, clause) == expected


@pytest.mark.parametrize("opening,expected", [
    (UK, ("Date of this agreement", "1 October 2026", "2026-10-01")),
    (US, ("Date of this agreement", "October 1, 2026", "2026-10-01")),
    ("THIS AGREEMENT is made on the 1st day of October 2026 between A Limited and B Limited.",
     ("Date of this agreement", "1st day of October 2026", "2026-10-01")),
])
def test_the_agreement_date_carries_its_iso_date(opening, expected):
    [row] = [(r.what, r.when, r.iso) for r in deadlines.rows(text_doc(opening))]
    assert row == expected


@pytest.mark.parametrize("clause", [
    "Background. On 1 March 2024 the Seller acquired the Property.",
    "The Company was incorporated on 3 May 2010.",
    "By a lease dated 1 June 2020 the Landlord let the Property to the Tenant.",
    "The Accounts cover the period from 1 January 2025 to 31 December 2025.",
    "The accounts cover the 12 months to 31 December 2025.",
    "The Seller shall procure that the Company acts as it did before 1 January 2025.",
    "This Agreement is between Kestrel Limited and Beta Limited.",
])
def test_a_date_that_sets_nothing_to_do_is_not_a_row(clause):
    assert rows(UK, clause) == []


def canary() -> bytes:
    d = Document()
    for text in (
        "SERVICES AGREEMENT",
        ('This Services Agreement is dated 1 October 2026 between Northwind Holdings '
        'Limited (the "Customer") and Beta Trading Limited (the "Supplier").'),
        "1. Services. The Supplier shall provide the Services from 1 November 2026.",
        "2. Fees. The Customer shall pay each invoice within 30 days, subject to clause 4.",
        "4. Term. This Agreement ends on 31 October 2027 unless renewed under clause 5.",
        "5. Renewal. Either party may give notice of non-renewal by 1 August 2027.",
        "6. Notices. Notices must be in writing.",
    ):
        d.add_paragraph(text)
    out = io.BytesIO()
    d.save(out)
    return out.getvalue()


def test_the_canary_services_agreement_has_its_three_deadlines_in_the_calendar():
    content = canary()
    doc = extract.extract(Attachment(filename="Canary Services.docx", content_type=DOCX,
                                     size_bytes=len(content), content=content))
    found = ics.read(doc)
    assert [(e.day.isoformat(), e.what, e.where) for e in found.dated] == [
        ("2026-11-01", "Start date", "clause 1"),
        ("2027-08-01", "Notice to stop renewal", "clause 5"),
        ("2027-10-31", "Term ends", "clause 4"),
    ]
    made = ics.attachment(found, "Canary Services.docx", minimum=1)
    raw = made.content.decode()
    assert raw.count("BEGIN:VEVENT") == 3
    assert "20261001" not in raw        # the agreement's own date is not a deadline
