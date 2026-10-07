"""The deterministic checks. These must not produce false positives.

False positives are the metric that kills this product, and unlike the model's
findings, these are ours to control exactly.
"""

from pathlib import Path

from lra.models import Attachment, Severity
from lra.pipeline import checks, extract


def doc(*paragraphs):
    text = "\n".join(paragraphs).encode()
    att = Attachment(filename="t.txt", content_type="text/plain",
                     size_bytes=len(text), content=text)
    return extract.extract(att)


def titles(findings):
    return [f.title for f in findings]


# --- placeholders ---------------------------------------------------------

def test_unfilled_bracket_is_a_blocker():
    f = checks.placeholders(doc("This Agreement is between Acme and [PARTY NAME]."))
    assert len(f) == 1
    assert f[0].severity is Severity.BLOCKER


def test_citation_brackets_are_not_placeholders():
    assert checks.placeholders(doc("See Smith v Jones [2019] and the note [sic].")) == []


def test_tbd_is_caught():
    assert any("TBD" in t for t in titles(checks.placeholders(doc("Price: TBD"))))


# --- amounts --------------------------------------------------------------

def test_words_disagreeing_with_numerals():
    f = checks.amounts(doc("Notice shall be given within thirty (13) days."))
    assert len(f) == 1
    assert f[0].severity is Severity.BLOCKER


def test_words_agreeing_with_numerals_is_silent():
    assert checks.amounts(doc("Notice shall be given within thirty (30) days.")) == []


# --- cross references -----------------------------------------------------

def test_reference_to_a_missing_section():
    f = checks.cross_references(doc("1. Term", "2. Fees", "Subject to Section 9."))
    assert len(f) == 1
    assert "Section 9" in f[0].title


def test_reference_to_a_present_section_is_silent():
    assert checks.cross_references(doc("1. Term", "2. Fees", "Subject to Section 2.")) == []


def test_no_numbered_structure_means_no_cross_reference_findings():
    assert checks.cross_references(doc("Please see Section 4 of the other agreement.")) == []


# --- numbering ------------------------------------------------------------

def test_skipped_section_number():
    f = checks.numbering(doc("1. One", "2. Two", "4. Four"))
    assert any("2 to 4" in t for t in titles(f))


def test_duplicate_section_number():
    f = checks.numbering(doc("1. One", "2. Two", "2. Also two"))
    assert any("appears 2 times" in t for t in titles(f))


def test_clean_numbering_is_silent():
    assert checks.numbering(doc("1. One", "2. Two", "3. Three")) == []


# --- party names ----------------------------------------------------------

def test_same_entity_two_suffixes():
    f = checks.party_names(doc("Acme Holdings LLC and later Acme Holdings Inc."))
    assert len(f) == 1
    assert f[0].severity is Severity.BLOCKER


def test_consistent_entity_name_is_silent():
    assert checks.party_names(doc("Acme Holdings LLC", "Acme Holdings LLC again")) == []


# --- dates ----------------------------------------------------------------

def test_mixed_date_formats():
    f = checks.date_consistency(doc("Dated January 1, 2026.", "Expires 2026-12-31."))
    assert len(f) == 1


def test_one_date_format_is_silent():
    assert checks.date_consistency(doc("Dated January 1, 2026.", "Ends March 1, 2026.")) == []


# --- defined terms --------------------------------------------------------

def test_term_used_several_clauses_before_it_is_defined():
    """Judged in numbered clauses, not paragraphs, and only when the gap is real."""
    f = checks.defined_terms(doc(
        "1. Payment. The Purchaser shall pay the Price on Completion.",
        "2. Delivery. The goods are delivered at Completion.",
        '3. Definitions. In this Agreement, "Purchaser" means Acme Holdings LLC.',
    ))
    assert any("defined in clause 3" in t for t in titles(f))


def test_a_term_defined_in_the_very_next_clause_is_not_flagged():
    """Ordinary drafting. Reporting it is wrong on a large share of leases."""
    f = checks.defined_terms(doc(
        "1. Demise. The Landlord lets the Premises for the Term.",
        '2. Term. The "Term" means ten (10) years from the commencement date.',
    ))
    assert not any("defined in clause" in t for t in titles(f))


def test_a_clause_headed_with_its_own_term_is_not_flagged():
    """"4. Rent. The "Rent" means..." is a deliberate structure, not a slip."""
    f = checks.defined_terms(doc(
        "1. Demise. The Landlord lets the Premises for the Term at the Rent.",
        "2. Premises. The Premises are at 14 Bridge Street.",
        "3. Condition. The Tenant accepts the Premises as they are.",
        '4. Rent. The "Rent" means fifty-two thousand pounds (\u00a352,000) per year.',
    ))
    assert not any('"Rent"' in t and "defined in clause" in t for t in titles(f))


def test_an_explicit_forward_reference_is_deliberate():
    f = checks.defined_terms(doc(
        "1. Termination. The Company may terminate for Cause as defined in Section 4.",
        "2. Notice. Notices must be in writing.",
        "3. Payment. Fees are payable monthly.",
        '4. Cause. "Cause" means conviction of a felony or wilful misconduct.',
    ))
    assert not any("Cause" in t and "defined in clause" in t for t in titles(f))


def test_term_restated_in_its_own_definition_is_not_unused():
    f = checks.defined_terms(doc(
        'Confidential Information. "Confidential Information" means anything disclosed.',
        "The Recipient shall protect Confidential Information at all times.",
    ))
    assert not any("never used" in t for t in titles(f))


# --- leftovers ------------------------------------------------------------

def test_author_metadata_is_reported():
    raw = Path("samples/simple.docx").read_bytes()
    assert any(f.category == "leftovers" for f in checks.leftovers(raw))


def test_leftovers_on_a_non_docx_does_not_explode():
    assert checks.leftovers(b"not a zip file at all") == []


# --- runner ---------------------------------------------------------------

def test_run_all_catches_every_planted_defect_in_the_sample():
    raw = Path("samples/simple.docx").read_bytes()
    att = Attachment(filename="simple.docx", content_type="x",
                     size_bytes=len(raw), content=raw)
    found = checks.run_all(extract.extract(att), raw)
    cats = {f.category for f in found}
    assert {"placeholder", "cross-reference", "numbering", "date"} <= cats


def test_prompt_block_tells_the_agent_not_to_repeat_them():
    raw = Path("samples/simple.docx").read_bytes()
    att = Attachment(filename="simple.docx", content_type="x",
                     size_bytes=len(raw), content=raw)
    block = checks.as_prompt_block(checks.run_all(extract.extract(att), raw))
    assert "NOT report them again" in block


def test_prompt_block_on_a_clean_document_says_so():
    assert "found nothing" in checks.as_prompt_block([])


# --- misspelled defined terms: offered, never applied ----------------------

def test_a_near_miss_of_a_defined_term_is_caught_and_offered():
    """Offered as a one-word question, not written in: the same rule would
    otherwise rewrite a real word into a defined term."""
    f = checks.term_misspellings(doc(
        'In this Agreement, "Recipient" means the receiving party.',
        "The Recipent shall protect the information.",
    ))
    assert len(f) == 1
    assert f[0].suggested_text == "Recipient"
    assert f[0].auto_apply is False
    assert f[0].options == ["Recipient", "Recipent"]


def test_a_genuinely_different_word_is_not_called_a_misspelling():
    """"Reciever" is four edits from "Recipient". That is a different word, and
    deciding what the drafter meant is the model's job, not a rule's."""
    assert checks.term_misspellings(doc(
        'In this Agreement, "Recipient" means the receiving party.',
        "The Reciever shall protect the information.",
    )) == []


def test_an_unrelated_capitalized_word_is_not_a_misspelling():
    assert checks.term_misspellings(doc(
        'In this Agreement, "Recipient" means the receiving party.',
        "The Parties agree that London is the venue.",
    )) == []


def test_the_defined_term_itself_is_not_flagged():
    assert checks.term_misspellings(doc(
        'In this Agreement, "Recipient" means the receiving party.',
        "The Recipient shall protect the information.",
    )) == []


def test_unused_defined_terms_are_not_flagged_on_a_short_document():
    """On a one-page letter a term used once is normal. Flagging it is the kind
    of noise that gets the whole tool filtered into a folder."""
    f = checks.defined_terms(doc(
        'This letter refers to "Services" as described below.',
        "We will begin in April.",
    ))
    assert not any("never used" in x.title for x in f)


def test_unused_defined_terms_are_flagged_on_a_long_document():
    paragraphs = [f"{i}. A clause with some text in it." for i in range(1, 16)]
    paragraphs.insert(0, 'In this Agreement, "Escrow Agent" means the bank.')
    f = checks.defined_terms(doc(*paragraphs))
    assert any("never used" in x.title for x in f)


def test_disclosure_is_not_flagged_as_a_misspelling_of_discloser():
    """The false positive that would have killed the product. "Disclosure" sits
    two edits from "Discloser" and appears in every NDA ever written."""
    assert checks.term_misspellings(doc(
        'Between Acme Holdings LLC ("Discloser") and Beta Industries Inc ("Recipient").',
        "Any Disclosure under this Agreement is confidential.",
    )) == []


def test_ordinary_legal_vocabulary_is_never_a_misspelling():
    assert checks.term_misspellings(doc(
        'In this Agreement, "Licensor" means Acme.',
        "The Licensee and the Licensor each give a Warranty.",
    )) == []


def test_a_word_the_drafter_uses_repeatedly_is_not_a_typo():
    """A real typo is rare. A word used throughout is deliberate."""
    body = ["The Purchasor shall pay." for _ in range(4)]
    assert checks.term_misspellings(doc(
        'In this Agreement, "Purchaser" means Beta Industries Inc.', *body,
    )) == []



def test_the_document_title_is_not_a_use_of_a_defined_term():
    """Almost every agreement's title contains the word "Agreement", and its
    preamble defines it. Reporting that is wrong on every document ever drafted."""
    from lra.pipeline.extract import Block, ExtractedDoc

    d = ExtractedDoc(
        blocks=[
            Block(0, "Mutual Non-Disclosure Agreement", "Heading 1", "heading"),
            Block(1, 'This Agreement (this "Agreement") is made on 3 March 2026.',
                  "Normal", "paragraph"),
            Block(2, "1. Term. This Agreement continues for three (3) years.",
                  "Normal", "paragraph"),
            Block(3, "2. Law. New York law governs this Agreement.", "Normal", "paragraph"),
        ],
        docx=None,
        filename="t.docx",
    )
    assert not any("defined in clause" in f.title for f in checks.defined_terms(d))


# --- amounts: compound numbers, in both directions ------------------------

def test_a_hyphenated_compound_number_is_read_correctly():
    """"forty-five (45)" used to be read as "five (45)" and reported as a
    blocker on a correct contract."""
    assert checks.amounts(doc("Invoices are payable within forty-five (45) days.")) == []


def test_a_scaled_number_mismatch_is_caught():
    f = checks.amounts(doc("The price is one million dollars ($1,500,000)."))
    assert len(f) == 1


def test_a_scaled_number_that_agrees_is_silent():
    assert checks.amounts(doc("The price is one million dollars ($1,000,000).")) == []


def test_a_percentage_pair_is_read_correctly():
    assert checks.amounts(doc("a target bonus of forty percent (40%) of salary")) == []


def test_british_compound_with_a_currency_symbol():
    assert checks.amounts(
        doc("The Rent is fifty-two thousand pounds (\u00a352,000) per year.")) == []


# --- placeholders: legal citations are not blanks -------------------------

def test_record_citations_are_not_placeholders():
    """Three blocker-severity false positives on a correct litigation memo was
    the worst failure the corpus found."""
    assert checks.placeholders(doc(
        "Plaintiff pleads only that the parties discussed terms [Compl. \u00b6 14] "
        "and that Defendant indicated willingness [id. \u00b6 17].",
    )) == []


def test_bracketed_editorial_notes_are_not_placeholders():
    assert checks.placeholders(doc(
        "The court held otherwise [emphasis added] [internal citations omitted].")) == []


def test_an_all_caps_bracket_is_still_a_placeholder():
    assert len(checks.placeholders(doc("between Acme LLC and [COUNTERPARTY NAME]"))) == 1


# --- party names: a clause heading is not part of the company name --------

def test_a_clause_heading_is_not_read_as_part_of_the_company_name():
    f = checks.party_names(doc(
        "1. Parties. This deed is made with Northgate Freight Limited.",
        "3. Warranties. Northgate Freight Ltd gives the warranties in Schedule 1.",
    ))
    assert len(f) == 1
    assert "Northgate Freight" in f[0].title


# --- findings the audit caught -------------------------------------------

def test_a_signature_block_is_not_three_blockers():
    """Every unsigned contract came back with blockers for its signature lines,
    on precisely the documents this product exists to check."""
    assert checks.placeholders(doc(
        "IN WITNESS WHEREOF the parties have executed this Agreement.",
        "ACME HOLDINGS LLC",
        "By: ______________________",
        "Name: ____________________",
        "Title: ___________________",
        "Date: ____________________",
    )) == []


def test_a_bare_signature_rule_is_not_a_placeholder():
    assert checks.placeholders(doc("SIGNED for and on behalf of ACME HOLDINGS LLC",
                                   "______________________________")) == []


def test_a_genuine_blank_in_a_clause_is_still_a_placeholder():
    f = checks.placeholders(doc("1. Term. The term shall be ______ years."))
    assert len(f) == 1


def test_a_plural_of_a_defined_term_is_not_a_misspelling():
    """"Tenants" is one edit from "Tenant". Auto-correcting it made
    "other Tenants of the Building" ungrammatical and changed its meaning."""
    assert checks.term_misspellings(doc(
        '1. Definitions. In this Lease, "Tenant" means Marchmont Retail Limited.',
        "2. Quiet enjoyment. The Landlord will not disturb other Tenants of the Building.",
    )) == []


def test_a_possessive_of_a_defined_term_is_not_a_misspelling():
    assert checks.term_misspellings(doc(
        '1. Definitions. "Recipient" means the receiving party.',
        "2. Care. The Recipients obligations survive termination.",
    )) == []


# --- the second round of audit findings -----------------------------------

def test_a_definition_restating_its_own_term_in_lower_case_is_not_flagged():
    """'"Purchase Price" means the purchase price payable for the Shares' is
    how every definitions clause is written. One "worth your judgment" item per
    definition, on a document with nothing wrong with it."""
    assert checks.term_capitalization(doc(
        '1. Definitions. "Purchase Price" means the purchase price payable for '
        "the Shares.",
        "2. Payment. The Buyer shall pay the Purchase Price at Completion.",
    )) == []


def test_a_lower_case_use_outside_the_definition_is_still_flagged():
    f = checks.term_capitalization(doc(
        '1. Definitions. "Purchase Price" means the amount in Schedule 1.',
        "2. Payment. The Buyer shall pay the purchase price at Completion.",
    ))
    assert len(f) == 1


def test_a_lettered_exhibit_that_exists_is_not_a_broken_reference():
    """Exhibit titles carry no heading style in most drafts and none at all in
    a .txt, so every reference to a lettered exhibit read as broken."""
    assert checks.cross_references(doc(
        "1. Services. The service levels are in Exhibit A.",
        "2. Data. The data terms are in Exhibit B.",
        "EXHIBIT A", "Service levels.", "EXHIBIT B", "Data processing terms.",
    )) == []


def test_a_reference_to_a_lettered_exhibit_that_is_missing_is_caught():
    f = checks.cross_references(doc(
        "1. Services. The service levels are in Exhibit C.",
        "EXHIBIT A", "Service levels.",
    ))
    assert any("Exhibit C" in t for t in titles(f))


def test_a_schedule_reference_is_not_satisfied_by_a_clause_of_the_same_number():
    f = checks.cross_references(doc(
        "1. One. Text.", "2. Two. Text.", "3. Warranties are in Schedule 9.",
        "Schedule 1. Warranties.",
    ))
    assert any("Schedule 9" in t for t in titles(f))


def test_schedules_that_are_not_in_the_file_are_not_invented_as_missing():
    """A document whose schedules are bound separately has no schedule titles
    to check against. Saying they do not exist would be a false blocker."""
    assert checks.cross_references(doc(
        "1. One. Text.", "2. Two. Text.", "3. Warranties are in Schedule 4.",
    )) == []


def test_a_section_of_an_act_is_not_a_reference_to_our_own_clause():
    assert checks.cross_references(doc(
        "1. One. Text.", "2. Two. Text.",
        "3. Directors. Each director complies with Section 172 of the "
        "Companies Act 2006.",
    )) == []


def test_a_postal_address_is_not_a_section_number():
    """An engagement letter on firm letterhead was told its numbering jumped
    from 88 to 1200."""
    assert checks.numbering(doc(
        "Ashworth & Partners LLP",
        "1600 Pennsylvania Avenue, Washington",
        "1. Scope. We will advise you.",
        "2. Fees. Our fees are set out below.",
        "12 March 2026 is the start date for this matter.",
    )) == []


def test_numbering_restarts_in_a_schedule():
    """Schedules number from 1 again. Reading them as a continuation of the
    body reported three duplicated clauses on a correctly numbered lease."""
    assert checks.numbering(doc(
        "1. One. Text.", "2. Two. Text.",
        "SCHEDULE 1", "1. Warranties. Text.", "2. Tax. Text.",
    )) == []


def test_a_long_numbering_gap_is_summarised_rather_than_enumerated():
    f = checks.numbering(doc("1. One. Text.", "40. Forty. Text."))
    assert len(f) == 1
    assert "and 33 more" in f[0].explanation
    assert len(f[0].explanation) < 300


def test_an_effective_date_that_differs_from_the_signing_date_is_not_a_blocker():
    """An effective date deliberately different from execution is ordinary
    drafting: commencement dates, completion dates, policy inception dates."""
    assert checks.contradictory_dates(doc(
        "This Agreement is made on 1 January 2026 between Acme LLC and Beta Inc.",
        "1. Commencement. This Agreement takes effect on the Effective Date, "
        "being 1 March 2026.",
    )) == []


def test_a_genuine_back_reference_to_the_opening_date_is_still_caught():
    f = checks.contradictory_dates(doc(
        "This Agreement is entered into as of March 3, 2026 between Acme LLC "
        "and Beta Inc.",
        "2. Term. Three years from the date first written above, being "
        "March 5, 2026.",
    ))
    assert len(f) == 1


def test_a_party_short_name_used_only_in_the_signature_block_is_not_unused():
    preamble = (
        'This Agreement is made between Northbridge Analytics Limited '
        '("Northbridge") and Calder Systems Inc ("Calder").'
    )
    paragraphs = [preamble]
    paragraphs += [f"{i}. Clause {i}. Operative text." for i in range(1, 15)]
    paragraphs += ["NORTHBRIDGE ANALYTICS LIMITED", "By: ______", "CALDER SYSTEMS INC"]
    assert not any("never used" in t for t in titles(checks.defined_terms(doc(*paragraphs))))


def test_a_suffix_spelling_is_a_style_point_and_a_different_form_is_a_blocker():
    """Ltd and Limited are one company written two ways. Ltd and LLP are two
    different companies, and only the second is "do not send"."""
    style = checks.party_names(doc(
        "This deed is made with Northgate Freight Limited.",
        "3. Warranties. Northgate Freight Ltd gives the warranties.",
    ))
    assert [f.severity for f in style] == [Severity.STYLE]

    blocker = checks.party_names(doc(
        "Whitfield Logistics Limited is the Buyer.",
        "4. Indemnity. Whitfield Logistics LLP is indemnified.",
    ))
    assert [f.severity for f in blocker] == [Severity.BLOCKER]


def test_two_different_corporate_forms_are_never_merged_automatically():
    """A parent and its subsidiary share a stem. Rewriting one into the other
    on our own guess is a misplaced edit in a client's contract."""
    f = checks.party_names(doc("Acme Holdings LLC and later Acme Holdings Inc."))
    assert f[0].auto_apply is False
    assert f[0].question


def test_tracked_changes_in_a_header_are_found():
    """Headers are a separate XML part. Reading only word/document.xml told a
    lawyer the file was clean while another author's header edit went out."""
    raw = Path("samples/simple.docx").read_bytes()
    f = checks.leftovers(_with_header(raw, _HEADER_WITH_A_TRACKED_CHANGE))
    tracked = [x for x in f if "tracked changes" in x.title]
    assert len(tracked) == 1
    assert "A. Associate" in tracked[0].explanation
    assert "header" in tracked[0].explanation


def test_findings_of_one_kind_are_capped_and_counted():
    """Hundreds of listed items in a preview pane is not a verdict."""
    paragraphs = [f"{i}. Clause {i}. See Section {i + 400}." for i in range(1, 40)]
    f = checks.run_all(doc(*paragraphs))
    refs = [x for x in f if x.category == "cross-reference"]
    assert len(refs) == 11                       # ten of them, plus the count
    assert any("more broken cross-references" in x.title for x in refs)
    # The lawyer's words for the kind, not the internal category name.
    assert not any("cross-reference findings" in x.explanation for x in refs)


_HEADER_WITH_A_TRACKED_CHANGE = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    '<w:p><w:ins w:id="900" w:author="A. Associate" w:date="2026-01-01T00:00:00Z">'
    "<w:r><w:t>PRIVILEGED AND CONFIDENTIAL</w:t></w:r></w:ins></w:p></w:hdr>"
)


def _with_header(original: bytes, header_xml: str) -> bytes:
    """The same .docx with one more header part. python-docx cannot add one."""
    import zipfile
    from io import BytesIO

    source = zipfile.ZipFile(BytesIO(original))
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as out:
        for item in source.infolist():
            out.writestr(item, source.read(item.filename))
        out.writestr("word/header1.xml", header_xml)
    return buffer.getvalue()


# --- execution readiness: the signature page ---------------------------------
#
# Each finding is a question, never an edit: who signs is not ours to decide.
# The false-positive cases matter more than the catches. A signature page is on
# every draft from day one, so anything wrong here fires on every document.

US_PARTIES = ('This Agreement is made as of March 3, 2026 between Acme Holdings, Inc., '
              'a Delaware corporation ("Buyer"), and Northgate Freight Ltd ("Seller").')
WITNESS = "IN WITNESS WHEREOF, the parties have executed this Agreement."


def block(entity, name="Jane Doe", title="Director"):
    return [entity, "By: ____________", f"Name: {name}", f"Title: {title}", ""]


def test_a_complete_american_signature_page_is_silent():
    d = doc(US_PARTIES, "RECITALS",
            'WHEREAS, Seller owns all of the shares of Target Co Ltd (the "Company").',
            "1. Sale. Seller sells the Shares to Buyer.", WITNESS,
            *block("ACME HOLDINGS, INC."), *block("NORTHGATE FREIGHT LTD", "John Smith"))
    assert checks.signature_blocks(d) == []


def test_a_complete_english_execution_page_is_silent():
    """Names written by hand, so every block is "blank" and none is reported."""
    d = doc("PARTIES",
            '(1) ACME LIMITED, a company incorporated in England and Wales with '
            'registered number 01234567 whose registered office is at 1 High Street, '
            'London EC1A 1AA (the "Seller"); and',
            '(2) NORTHGATE FREIGHT LIMITED, a company incorporated in England (the "Buyer").',
            "BACKGROUND", '(A) The Seller owns Target Limited (the "Company").',
            "1. Interpretation.",
            "EXECUTED as a DEED by ACME LIMITED acting by a director", "______________", "Director",
            "EXECUTED as a DEED by NORTHGATE FREIGHT LIMITED acting by a director",
            "______________", "Director")
    assert checks.signature_blocks(d) == []


def test_a_description_is_not_an_entity():
    """"a Delaware corporation" is not a party called Delaware."""
    d = doc(US_PARTIES, "1. Sale.", WITNESS,
            *block("ACME HOLDINGS, INC."), *block("NORTHGATE FREIGHT LTD"))
    assert [name for name, _ in checks._parties(d)[0]] == ["Acme Holdings, Inc.", "Northgate Freight Ltd"]


def test_the_target_in_the_recitals_is_not_a_party():
    d = doc(US_PARTIES, "RECITALS",
            'WHEREAS, Seller owns Target Co Ltd (the "Company").', "1. Sale.")
    assert "Target Co Ltd" not in [name for name, _ in checks._parties(d)[0]]


def test_a_party_with_no_signature_block_is_a_question():
    d = doc('This Agreement is between Acme Holdings, Inc. ("Buyer"), Northgate Freight '
            'Ltd ("Seller") and Harbour Guarantee LLC ("Guarantor").', "1. Sale.", WITNESS,
            *block("ACME HOLDINGS, INC."), *block("NORTHGATE FREIGHT LTD", "John Smith"))
    [f] = checks.signature_blocks(d)
    assert f.title == "No signature block for Harbour Guarantee LLC"
    assert f.severity is Severity.SUBSTANTIVE
    assert f.question and not f.suggested_text
    assert f.anchor == "Harbour Guarantee LLC"


def test_a_signatory_that_is_not_a_party_is_the_previous_deals_page():
    d = doc(US_PARTIES, "1. Sale.", WITNESS,
            *block("ACME HOLDINGS, INC."), *block("BLUEWATER SHIPPING LLC", "Pat Lee"))
    found = titles(checks.signature_blocks(d))
    assert "BLUEWATER SHIPPING LLC signs, but is not a party" in found
    assert "No signature block for Northgate Freight Ltd" in found


def test_one_half_filled_block_among_complete_ones():
    d = doc(US_PARTIES, "1. Sale.", WITNESS,
            *block("ACME HOLDINGS, INC."),
            "NORTHGATE FREIGHT LTD", "By: ____________", "Name:", "Title:")
    found = titles(checks.signature_blocks(d))
    assert "Signature block for NORTHGATE FREIGHT LTD has no name" in found
    assert "Signature block for NORTHGATE FREIGHT LTD has no title" in found


def test_individuals_signing_are_not_compared_to_the_parties_clause():
    d = doc('This Agreement is between Acme Holdings, Inc. ("Company") and Jane Doe ("Employee").',
            "1. Role.", "Signed: ______", "Name: Jane Doe", "Date: ______",
            *block("ACME HOLDINGS, INC.", "Pat Lee", "HR Director"))
    assert checks.signature_blocks(d) == []


def test_a_table_header_saying_name_and_title_is_not_a_signature_block():
    d = doc("1. Officers.", "Name", "Title", "Jane Doe", "Director")
    assert checks._signature_blocks(d) == []
    assert not checks.looks_like_execution_version(d)


def test_a_document_with_no_signature_page_says_nothing():
    d = doc(US_PARTIES, "1. Sale.", "2. Price.")
    assert checks.signature_blocks(d) == []


def test_execution_version_is_recognised_from_the_page_or_the_filename():
    signed = doc("1. Sale.", WITNESS)
    assert checks.looks_like_execution_version(signed)
    draft = doc("1. Sale.")
    assert not checks.looks_like_execution_version(draft)
    text = b"1. Sale."
    att = Attachment(filename="SPA - Execution Version.docx", content_type="text/plain",
                     size_bytes=len(text), content=text)
    assert checks.looks_like_execution_version(extract.extract(att))


# --- the shapes a real signature page takes, none of which is a finding -----

def test_a_name_with_lowercase_connectors_matches_its_capitals():
    d = doc('This Agreement is between The Royal Bank of Scotland plc ("Bank") and '
            'Simmons and Simmons LLP ("Adviser").', "1. Terms.", WITNESS,
            *block("THE ROYAL BANK OF SCOTLAND PLC"), *block("SIMMONS AND SIMMONS LLP", "C"))
    assert checks.signature_blocks(d) == []


def test_the_english_form_puts_the_company_under_the_signature():
    d = doc('Between Acme Limited ("Seller") and Northgate Freight Limited ("Buyer").',
            "1. Sale.", WITNESS,
            "Signed: ______________", "Name:", "Title:", "for and on behalf of ACME LIMITED", "",
            "Signed: ______________", "Name:", "Title:",
            "for and on behalf of NORTHGATE FREIGHT LIMITED")
    assert checks.signature_blocks(d) == []
    assert [b.entity for b in checks._signature_blocks(d)] == ["ACME LIMITED",
                                                               "NORTHGATE FREIGHT LIMITED"]


def test_a_partnership_signs_through_a_chain_of_by_lines():
    d = doc('Between Acme Fund, L.P. ("Fund") and Northgate Freight Ltd ("Seller").',
            "1. Sale.", WITNESS,
            "ACME FUND, L.P.", "By: Acme GP, LLC, its general partner", "By: ____",
            "Name: Jane Doe", "Title: Managing Member", "",
            *block("NORTHGATE FREIGHT LTD", "John Smith"))
    assert checks.signature_blocks(d) == []


def test_parties_listed_in_a_schedule_mean_anyone_may_sign():
    d = doc("PARTIES", '(1) ACME LIMITED (the "Borrower"); and',
            '(2) THE FINANCIAL INSTITUTIONS listed in Schedule 1 (the "Original Lenders").',
            "1. Definitions.", WITNESS, *block("ACME LIMITED"), *block("BIGBANK PLC", "C"))
    assert checks.signature_blocks(d) == []


def test_hereinafter_referred_to_as_is_a_role():
    d = doc('(1) ACME LIMITED (hereinafter referred to as the "Seller"); and',
            '(2) NORTHGATE FREIGHT LIMITED (hereinafter called the "Buyer").', "1. Sale.")
    assert [k[0] for _, k in checks._parties(d)[0]] == ["acme", "northgate freight"]


def test_a_notice_clause_is_not_a_signature_page():
    d = doc(US_PARTIES, "1. Sale. See Schedule 2.", "9. Notices.",
            "If to Buyer:", "Acme Holdings, Inc.", "Name: Jane Doe", "Title: General Counsel",
            "If to Seller:", "Northgate Freight Ltd", "Name: John Smith", "Title: Director")
    assert checks._signature_blocks(d) == []
    assert not checks.looks_like_execution_version(d)


def test_a_witness_block_with_a_blank_name_is_not_a_finding():
    d = doc(US_PARTIES, "1. Sale.", WITNESS,
            *block("ACME HOLDINGS, INC."), "Witness: ______", "Name:", "",
            *block("NORTHGATE FREIGHT LTD", "John Smith"))
    assert checks.signature_blocks(d) == []


# --- punctuation and spacing ----------------------------------------------------
#
# Character-changing slips are written; whitespace and quote-style slips are
# counted. The writer normalises whitespace and quotes when it matches, so a
# tracked change that only removed a space would delete and re-insert the words
# around it.

def test_a_repeated_function_word_is_fixed():
    [f] = checks.punctuation(doc("The the Buyer shall pay."))
    assert f.anchor == "The the" and f.suggested_text == "The" and f.auto_apply


def test_that_that_and_had_had_are_english():
    assert checks.punctuation(doc("I know that that is true, and he had had enough.")) == []


def test_a_space_on_the_wrong_side_of_a_comma_is_moved():
    [f] = checks.punctuation(doc("The term is thirty days ,and no more."))
    assert f.suggested_text == f.anchor.replace(" ,and", ", and")


def test_a_missing_space_after_a_comma_is_added():
    [f] = checks.punctuation(doc("Pay the Seller,and then the Buyer."))
    assert f.anchor == "Seller,and" and f.suggested_text == "Seller, and"


def test_numbers_addresses_and_paths_are_not_comma_slips():
    assert checks.punctuation(doc("Pay 1,000 to john.smith@acme.com per acme.com/terms,v2 at 10:30.")) == []


def test_two_spaces_after_a_full_stop_are_a_convention():
    assert checks.punctuation(doc("First sentence.  Second sentence.  Third.")) == []


def test_double_spaces_between_words_are_counted_not_edited():
    [f] = checks.punctuation(doc("The  Buyer shall  pay the Seller."))
    assert f.title == "2 double spaces between words"
    assert not f.auto_apply and f.severity is Severity.FORMATTING


def test_columns_laid_out_with_spaces_are_not_double_spaces():
    assert checks.punctuation(doc("Name          Title          Date")) == []


def test_a_stray_pair_of_straight_quotes_among_curly_ones_is_counted():
    [f] = checks.punctuation(doc(
        'The “Agreement”, the "Buyer", the “Seller”, the “Company” and the “Shares”.'))
    assert f.title.startswith("1 pair of straight quotes")
    assert f.anchor == '"Buyer"' and not f.auto_apply


def test_a_document_in_straight_quotes_throughout_is_consistent():
    assert checks.punctuation(doc('The "Agreement" and the "Buyer" and the "Seller".')) == []
