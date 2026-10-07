"""Renumbering typed clause numbers and every reference to them.

Half of these assert a refusal or silence: a renumber that guesses which
clause a reference meant is a wrong cross-reference written into a client's
contract as if it were a correction.
"""

from __future__ import annotations

import re
import zipfile
from datetime import UTC, datetime
from io import BytesIO

import pytest
from docx import Document

from lra import handler
from lra.models import Attachment, Finding, InboundEmail, Mode, ReviewResult, Severity
from lra.pipeline import checks, extract, intake, redline, renumber, reply
from tests.test_versions import V1, attach, captured, docx, email  # noqa: F401


def text_of(content: bytes) -> list[str]:
    doc = extract.extract(Attachment(filename="x.docx", content_type="",
                                     size_bytes=len(content), content=content))
    return [b.text for b in doc.blocks]


def rejected_text(content: bytes) -> list[str]:
    """What the lawyer gets from Reject All: deleted text back, insertions gone."""
    xml = zipfile.ZipFile(BytesIO(content)).read("word/document.xml").decode()
    xml = re.sub(r"<w:ins [^>]*>.*?</w:ins>", "", xml, flags=re.DOTALL)
    xml = xml.replace("w:delText", "w:t")
    out = []
    for p in re.findall(r"<w:p[ >].*?</w:p>", xml, flags=re.DOTALL):
        out.append("".join(re.findall(r"<w:t(?: [^>]*)?>([^<]*)</w:t>", p)))
    return out


GAP = [
    "1. Definitions. Terms have the meanings given in clause 2.",
    "2. Term. This Agreement starts on 1 March 2026 and runs for 12 months.",
    "4. Payment. The Buyer shall pay £4,000 under clause 5.",
    "4.1 Invoices are payable within 30 days.",
    "4.3 Late sums bear interest; see clause 4.1 and clause 6(b).",
    ("5. Liability. Sections 4 and 5 survive, subject to section 1.1502-6 of the "
    "Treasury Regulations."),
    ("6. Law. Clauses 4 to 6 apply. Section 5 of the Supply Agreement is unaffected, "
    "as is Article 5 GDPR."),
    "SCHEDULE 1",
    "1. See clause 5 and paragraph 3.",
    "3. A schedule paragraph numbered in its own space.",
]


def test_a_gap_is_closed_and_every_reference_follows_it():
    out = renumber.renumber(docx(GAP), "SPA.docx")
    assert out.headline == ("Numbering fixed: clauses 3–5 renumbered and 8 references "
                            "updated, as tracked changes.")
    assert text_of(out.content)[:10] == [
        "1. Definitions. Terms have the meanings given in clause 2.",
        "2. Term. This Agreement starts on 1 March 2026 and runs for 12 months.",
        "3. Payment. The Buyer shall pay £4,000 under clause 4.",
        "3.1 Invoices are payable within 30 days.",
        "3.2 Late sums bear interest; see clause 3.1 and clause 5(b).",
        ("4. Liability. Sections 3 and 4 survive, subject to section 1.1502-6 of the "
        "Treasury Regulations."),
        ("5. Law. Clauses 3 to 5 apply. Section 5 of the Supply Agreement is unaffected, "
        "as is Article 5 GDPR."),
        "SCHEDULE 1",
        "1. See clause 4 and paragraph 3.",
        "3. A schedule paragraph numbered in its own space.",
    ]
    assert [(r.where, r.before, r.after) for r in out.references][:2] == [
        ("Clause 3", "clause 5", "clause 4"),
        ("Clause 3.2", "clause 4.1", "clause 3.1"),
    ]
    assert out.references[-1].where == "Schedule 1"
    assert out.filename == "SPA (renumbered).docx"


def test_every_change_is_tracked_and_reject_all_gives_back_the_original():
    content = docx(GAP)
    out = renumber.renumber(content, "SPA.docx")
    assert redline.verify(out.content, expect_revisions=True)[0]
    assert rejected_text(out.content)[: len(GAP)] == GAP
    # Only numbers were deleted or inserted.
    xml = zipfile.ZipFile(BytesIO(out.content)).read("word/document.xml").decode()
    changed = re.findall(r"<w:(?:delText|t)[^>]*>([^<]*)</w:(?:delText|t)>",
                         "".join(re.findall(r"<w:(?:ins|del) .*?</w:(?:ins|del)>", xml, re.DOTALL)))
    assert changed and all(re.fullmatch(r"[0-9.]+", c) for c in changed)


def test_the_repaired_document_passes_both_checks():
    out = renumber.renumber(docx(GAP), "SPA.docx")
    doc = extract.extract(Attachment(filename="SPA.docx", content_type="",
                                     size_bytes=1, content=out.content))
    body = renumber._body_only(doc)
    assert checks.numbering(body) == []
    assert checks.cross_references(doc) == []


def test_a_duplicate_is_numbered_on_and_later_references_follow():
    out = renumber.renumber(docx([
        "1. Scope.", "2. Services.", "3. Fees.",
        "3. Expenses. As in clause 4.", "4. Term. Ends as clause 2 says.",
    ]), "MSA.docx")
    assert text_of(out.content)[:5] == [
        "1. Scope.", "2. Services.", "3. Fees.",
        "4. Expenses. As in clause 5.", "5. Term. Ends as clause 2 says.",
    ]
    assert out.was_wrong == ["Clause 3 appears 2 times"]


def test_a_reference_to_a_duplicated_number_is_refused():
    with pytest.raises(renumber.CannotRenumber, match="appears 2 times"):
        renumber.renumber(docx([
            "1. Scope.", "2. Fees.", "2. Expenses.", "3. Term. Clause 2 applies.",
        ]), "MSA.docx")


def test_a_reference_to_the_missing_clause_is_refused_not_redirected():
    """After renumbering, "clause 3" would point at what was clause 4."""
    with pytest.raises(renumber.CannotRenumber, match="not there"):
        renumber.renumber(docx([
            "1. Scope.", "2. Fees.", "4. Term. See clause 3.", "5. Law.",
        ]), "MSA.docx")


def test_a_reference_to_a_number_that_never_exists_is_left_alone():
    out = renumber.renumber(docx([
        "1. Scope. See clause 40.", "2. Fees.", "4. Term.",
    ]), "MSA.docx")
    assert text_of(out.content)[:3] == ["1. Scope. See clause 40.", "2. Fees.", "3. Term."]


def test_a_sub_clause_gap_is_closed():
    out = renumber.renumber(docx([
        "1. Payment.", "1.1 Invoices.", "1.3 Interest, as in clause 1.1.",
        "1.4 Set-off under clause 1.3.", "2. Law.",
    ]), "MSA.docx")
    assert text_of(out.content)[:5] == [
        "1. Payment.", "1.1 Invoices.", "1.2 Interest, as in clause 1.1.",
        "1.3 Set-off under clause 1.2.", "2. Law.",
    ]
    assert out.headline.startswith("Numbering fixed: clauses 1.2–1.3 renumbered")


def test_a_figure_at_the_start_of_a_paragraph_is_not_a_clause_number():
    out = renumber.renumber(docx([
        "1. Shares.", "3. Issue.", "3.5 million shares are issued on 2.5 terms.",
    ]), "SPA.docx")
    assert text_of(out.content)[:3] == [
        "1. Shares.", "2. Issue.", "3.5 million shares are issued on 2.5 terms.",
    ]


def test_a_stray_sub_clause_is_refused():
    with pytest.raises(renumber.CannotRenumber, match="does not sit under"):
        renumber.renumber(docx(["1. Scope.", "3. Fees.", "2.1 Expenses."]), "MSA.docx")


def test_numbers_already_in_sequence_are_left_alone():
    with pytest.raises(renumber.CannotRenumber, match="already run in sequence"):
        renumber.renumber(docx(V1), "Supply.docx")


def test_a_slip_only_in_a_schedule_is_left_for_the_lawyer():
    with pytest.raises(renumber.CannotRenumber, match="schedule"):
        renumber.renumber(docx(["1. Scope.", "2. Fees.", "SCHEDULE 1", "1. One.",
                                "3. Three."]), "MSA.docx")


def test_automatic_numbering_is_left_to_word():
    d = Document()
    for t in ("Scope.", "Fees.", "Term."):
        d.add_paragraph(t, style="List Number")
    buf = BytesIO()
    d.save(buf)
    with pytest.raises(renumber.CannotRenumber, match="automatic"):
        renumber.renumber(buf.getvalue(), "MSA.docx")


def test_nothing_is_returned_when_the_gate_fails(monkeypatch):
    monkeypatch.setattr(redline, "verify", lambda *a, **k: (False, "broken"))
    with pytest.raises(renumber.CannotRenumber, match="not sent a renumbered copy"):
        renumber.renumber(docx(GAP), "SPA.docx")


# --- asking for it ------------------------------------------------------------


@pytest.mark.parametrize("body", [
    "renumber", "Renumber please", "Can you fix the numbering?",
    "please sort out the clause numbers", "Correct the section numbering.",
])
def test_asking_to_renumber(body):
    assert intake.wants_renumber(email(body))


@pytest.mark.parametrize("body", [
    "We will renumber once the draft settles.", "Please do not renumber.",
    "They\u2019ll renumber it once the draft settles.",
    "fix the numbers in the price table", "Please review the attached.",
])
def test_not_asking_to_renumber(body):
    assert not intake.wants_renumber(email(body))


def test_a_renumber_request_gets_the_document_back(captured):  # noqa: F811
    handler.handle(email("Can you fix the numbering?", attach("SPA.docx", docx(GAP))))
    out = captured.sent[0]
    assert out.text_body.startswith("Numbering fixed: clauses 3–5 renumbered and 8 "
                                    "references updated, as tracked changes.")
    assert "Clause 3: clause 5 → clause 4" in out.text_body
    assert [a.filename for a in out.attachments] == ["SPA (renumbered).docx"]


def test_a_refused_renumber_is_one_sentence_and_no_file(captured):  # noqa: F811
    handler.handle(email("renumber", attach("MSA.docx", docx([
        "1. Scope.", "2. Fees.", "4. Term. See clause 3."]))))
    out = captured.sent[0]
    assert not out.attachments
    assert "not sent a renumbered copy" in out.text_body


def test_renumber_as_a_reply_uses_the_document_the_conversation_holds(
        captured, monkeypatch):  # noqa: F811
    from lra.pipeline import compare

    monkeypatch.setattr(compare, "_assess", lambda *a, **k: None)
    later = ["1. Term. Twelve months.", "3. Law. Clause 1 governs."]
    handler.handle(email("Compare these please.",
                         attach("Supply v2.docx", docx(later)),
                         attach("Supply v1.docx", docx(V1))))
    handler.handle(InboundEmail(
        message_id="v-2", from_address="jim@firm.com", to=["review@legal.firm.com"],
        subject="Re: Supply agreement", text_body="renumber",
        in_reply_to="v-1", references=["v-1"], received_at=datetime.now(UTC),
    ))
    out = captured.sent[-1]
    assert out.text_body.startswith("Numbering fixed: clause 2 renumbered and no "
                                    "references needed updating")
    assert text_of(out.attachments[0].content)[:2] == [
        "1. Term. Twelve months.", "2. Law. Clause 1 governs."]


# --- offering it --------------------------------------------------------------


def _review(findings) -> str:
    inbound = InboundEmail(message_id="m1", from_address="jim@example.com",
                           subject="SPA draft", received_at=datetime.now(UTC))
    return reply.compose(inbound, ReviewResult(mode=Mode.REDLINE, summary="",
                                               findings=findings),
                         None, "a.docx", [], findings).text_body


def test_the_review_offers_renumber_when_the_numbering_check_fires():
    slip = Finding(severity=Severity.FORMATTING, category="numbering",
                   title="Numbering jumps from 2 to 4", explanation="", anchor="4. Pay")
    assert 'Reply "renumber" and I\'ll fix the numbering' in _review([slip])


def test_no_offer_without_a_numbering_finding():
    typo = Finding(severity=Severity.FORMATTING, category="typo",
                   title="Double space", explanation="", anchor="a  b")
    assert "renumber" not in _review([typo])
