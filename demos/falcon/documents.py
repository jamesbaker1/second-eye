"""Every document in Project Falcon, built from code, byte for byte the same on
every run.

Word files are made with python-docx and then made reproducible: every zip
entry gets one fixed timestamp and every revision and comment one fixed date,
so `build.py` run twice writes identical bytes and the determinism test can
say so. PDFs are made with reportlab (invariant mode) and pypdf.

The defects the story needs are planted here and nowhere else; `DEFECTS`
lists each one with the words that prove it is present, and the tests hold
the built files to that list.
"""

from __future__ import annotations

import re
import zipfile
from io import BytesIO

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt

from demos.falcon import cast

FIXED_ZIP_TIME = (2026, 9, 28, 9, 0, 0)
FIXED_DATE = "2026-09-28T09:00:00Z"

# --------------------------------------------------------------------------
# Reproducible Word files
# --------------------------------------------------------------------------


def stable(content: bytes) -> bytes:
    """The same package with fixed timestamps: zip entries, revisions and
    comments. Nothing else is touched."""
    source = zipfile.ZipFile(BytesIO(content))
    out = BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename.startswith("word/") and info.filename.endswith(".xml"):
                data = re.sub(rb'w:date="[^"]*"', f'w:date="{FIXED_DATE}"'.encode(), data)
                data = re.sub(rb'w16cex:dateUtc="[^"]*"',
                              f'w16cex:dateUtc="{FIXED_DATE}"'.encode(), data)
            if info.filename == "docProps/core.xml":
                data = re.sub(rb"(<dcterms:(?:created|modified)[^>]*>)[^<]*",
                              rb"\g<1>2026-09-28T09:00:00Z", data)
            entry = zipfile.ZipInfo(info.filename, date_time=FIXED_ZIP_TIME)
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o644 << 16
            target.writestr(entry, data)
    return out.getvalue()


def _new(author: str = "", title: str = "", spaced: bool = False) -> Document:
    """A blank document. `spaced` sets an agreement the way firms print them:
    12 point, a little leading, space after each paragraph."""
    d = Document()
    style = d.styles["Normal"]
    style.font.name = "Times New Roman"
    style.font.size = Pt(12 if spaced else 11)
    if spaced:
        style.paragraph_format.space_after = Pt(8)
        style.paragraph_format.line_spacing = 1.2
    props = d.core_properties
    props.author = author
    props.last_modified_by = author
    props.title = title
    props.comments = ""
    props.keywords = ""
    props.revision = 1
    return d


def _save(d: Document) -> bytes:
    buf = BytesIO()
    d.save(buf)
    return stable(buf.getvalue())


def _table(d: Document, rows: list[tuple[str, ...]]) -> None:
    t = d.add_table(rows=len(rows), cols=len(rows[0]))
    t.style = "Table Grid"
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            t.rows[r].cells[c].text = value


def _centre(d: Document, text: str, bold: bool = True, size: int = 12):
    p = d.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(text)
    run.bold = bold
    run.font.size = Pt(size)
    return p


def _heading(d: Document, text: str):
    p = d.add_paragraph()
    p.add_run(text).bold = True
    return p


# --------------------------------------------------------------------------
# 1. The firm's positions memo and two precedents
# --------------------------------------------------------------------------


def positions_memo() -> bytes:
    """Carrow Lisle's buy-side positions, about two pages, in the firm's voice."""
    d = _new("Knowledge team", "Private M&A: buy-side negotiating positions")
    _centre(d, f"{cast.FIRM.upper()}: CORPORATE GROUP", size=13)
    _centre(d, "Private M&A: buy-side negotiating positions", bold=False)
    d.add_paragraph("Memorandum to: all corporate fee earners. From: the knowledge team, "
                    "with the corporate partners. Date: 1 September 2026. Status: approved "
                    "by the partners for use on every buy-side share purchase.")
    d.add_paragraph(
        "This memo sets out where we start, where we will settle and where we stop on the "
        "points that decide a share purchase for a buyer. It replaces the 2024 note. Where "
        "a draft departs from a position, raise it; where it goes past the walk-away "
        "point, do not concede it without a partner. Model wording is in the Heron and "
        "Kestrel agreements, which the partners regard as our best recent turns.")
    for n, p in enumerate(cast.POSITIONS, 1):
        _heading(d, f"{n}. {p['clause']}")
        body = [p["intro"], "Our position: " + p["position"]]
        if p["fallback"]:
            body.append("Fallback: " + p["fallback"])
        if p["walk_away"]:
            body.append("Walk-away: " + p["walk_away"])
        if p["watch"]:
            body.append("Watch for: " + p["watch"])
        d.add_paragraph(" ".join(body))
    d.add_paragraph(
        "Questions on any of this go to the knowledge team. Nothing in this memo is to be "
        "sent outside the firm.")
    return _save(d)


_PRECEDENT_CLAUSES = {
    "fraud": ("None of the limitations in this clause shall apply to any Claim arising "
              "from fraud, dishonesty or wilful concealment by the Seller or any of its "
              "agents."),
    "tax": ("The Seller covenants to pay to the Buyer an amount equal to any Tax "
            "liability of the Company arising in respect of any period ending on or "
            "before Completion, pound for pound."),
    "escrow": ("The Escrow Amount shall be held in the Escrow Account and released to the "
               "Seller six months after Completion, less any amount then required to "
               "satisfy a notified Claim."),
    "claims": ("The Seller shall not be liable for a Claim unless the Buyer notifies it "
               "within 24 months after Completion, or within seven years after "
               "Completion in the case of a Tax Claim."),
    "cap": ("The aggregate liability of the Seller for all Claims shall not exceed 50 per "
            "cent. of the Consideration, save for Claims under the title and capacity "
            "Warranties, for which it shall not exceed the Consideration."),
    "covenant": ("The Seller shall not, for 24 months after Completion, carry on or be "
                 "interested in any business which competes with the Business."),
    "law": ("This agreement is governed by the law of England and Wales, and the courts "
            "of England and Wales have exclusive jurisdiction."),
}


def precedent(project: str, buyer: str, seller: str, company: str, date: str,
              price: str) -> bytes:
    """A short executed SPA from an earlier deal, as a precedent: the clauses
    the memo calls model wording, and a completed signature page."""
    d = _new("Records", f"Project {project}: share purchase agreement (executed)")
    d.sections[0].header.paragraphs[0].text = f"Project {project}: executed version"
    _centre(d, "SHARE PURCHASE AGREEMENT", size=14)
    _centre(d, f"relating to {company.upper()}", bold=False)
    d.add_paragraph(f"THIS AGREEMENT is dated {date}")
    d.add_paragraph("PARTIES")
    d.add_paragraph(f"(1)\t{seller.upper()}, a company incorporated in England and Wales "
                    "(the “Seller”); and")
    d.add_paragraph(f"(2)\t{buyer.upper()}, a company incorporated in England and Wales "
                    "(the “Buyer”).")
    d.add_paragraph("AGREED TERMS")
    clauses = [
        ("1.", "Definitions"),
        ("1.1", f"“Business” means the business carried on by {company}; “Claim” means "
                "a claim under this agreement; “Company” means " + company + "; "
                "“Completion” means completion of the sale of the Shares; “Consideration” "
                f"means {price}; “Escrow Account” and “Escrow Amount” have the meanings in "
                "clause 3.2; “Shares” means all the issued shares in the Company; “Tax” "
                "and “Tax Claim” have the meanings in the tax covenant; “Warranties” means "
                "the warranties in Schedule 1."),
        ("2.", "Sale"),
        ("2.1", ("The Seller shall sell and the Buyer shall buy the Shares with full title "
                "guarantee.")),
        ("3.", "Consideration and escrow"),
        ("3.1", (f"The Consideration is {price}, payable in cash at Completion, save for the "
                "Escrow Amount.")),
        ("3.2", "An amount equal to 10 per cent. of the Consideration (the “Escrow "
                "Amount”) shall be paid into an account with the escrow agent (the "
                "“Escrow Account”). " + _PRECEDENT_CLAUSES["escrow"]),
        ("4.", "Limitations"),
        ("4.1", _PRECEDENT_CLAUSES["cap"]),
        ("4.2", _PRECEDENT_CLAUSES["claims"]),
        ("4.3", _PRECEDENT_CLAUSES["fraud"]),
        ("5.", "Tax"),
        ("5.1", _PRECEDENT_CLAUSES["tax"]),
        ("6.", "Protection of goodwill"),
        ("6.1", _PRECEDENT_CLAUSES["covenant"]),
        ("7.", "Governing law"),
        ("7.1", _PRECEDENT_CLAUSES["law"]),
    ]
    for number, text in clauses:
        d.add_paragraph(f"{number}\t{text}")
    d.add_paragraph("This agreement has been entered into on the date stated at the "
                    "beginning of it.")
    d.add_page_break()
    d.add_paragraph("SCHEDULE 1")
    d.add_paragraph("Warranties")
    d.add_paragraph("1.\tThe Seller is the sole legal and beneficial owner of the Shares.")
    d.add_paragraph("2.\tThe accounts of the Company give a true and fair view.")
    d.add_page_break()
    for entity, who in ((seller, "Director A"), (buyer, "Director B")):
        d.add_paragraph(f"Signed by {who.upper()} for and on behalf of {entity.upper()}:")
        d.add_paragraph("Signature: /signed/")
        d.add_paragraph(f"Name: {who}")
        d.add_paragraph("Title: Director")
    return _save(d)


def heron_precedent() -> bytes:
    return precedent("Heron", "Northwind Couriers Limited", "Tamsin Group Limited",
                     "Heron Parcels Limited", "14 March 2025", "£6,200,000")


def kestrel_precedent() -> bytes:
    return precedent("Kestrel", "Oakhurst Freight Limited", "Pellow Industries Limited",
                     "Kestrel Cold Chain Limited", "2 December 2025", "£9,750,000")


# --------------------------------------------------------------------------
# 2. The share purchase agreement, every version of it
# --------------------------------------------------------------------------
#
#   v1         Harrowgate's first draft (their paper): 14 typos, seller-friendly.
#   v2         Harrowgate's second draft: accepts five of our seven points,
#              rejects the cap and (silently) the claims period, deletes the
#              fraud carve-out, moves the long-stop date, and restates
#              Schedule 2 so it no longer adds up to the price. Accepting our
#              six-month escrow leaves the 90-day claims window in 12.2
#              closing before the escrow is released.
#   agreed     The agreed form, next morning: "thirty (13)", "four (5)", and
#              the Buyer renamed Falcon Topco Limited everywhere but its
#              signature block.
#   execution  The agreed form with those three settled: what is signed.

VERSIONS = ("v1", "v2", "agreed", "execution")

# The typos Harrowgate left in their draft, as (clause, what it says). The
# review of their paper counts them and writes none of them into their text.
TYPOS = [
    ("1.1", "accordence"),
    ("1.1", "recieve"),
    ("2.2", "the the"),
    ("4.2", "reasonabl"),
    ("5.1", "occured"),
    ("5.1", "seperate"),
    ("6.3", "immediatly"),
    ("8.2", "liabilty"),
    ("8.4", "in in"),
    ("9.1", "amout"),
    ("13.1", "garantees"),
    ("14.2", "anouncement"),
    ("16.1", "responsable"),
    ("21.1", "goverened"),
]


def _t(right: str, wrong: str, typos: bool) -> str:
    return wrong if typos else right


def spa_paragraphs(rev: str) -> list[tuple]:
    """The agreement as a list of items: ("p", text), ("table", rows),
    ("break",), ("centre", text). Every variation between versions is here."""
    if rev not in VERSIONS:
        raise ValueError(rev)
    theirs = rev in ("v1", "v2")
    typo = theirs
    v1 = rev == "v1"
    buyer = cast.BUYER_OLD if theirs else cast.BUYER
    buyer_block = cast.BUYER if rev == "execution" else cast.BUYER_OLD
    agreed = rev == "agreed"

    def t(right: str, wrong: str) -> str:
        return _t(right, wrong, typo)

    long_stop = "31 January 2027" if rev == "v2" else "31 March 2027"
    escrow_release = "three months" if v1 else "six months"
    cash = "£9,050,000" if rev == "v2" else "£9,500,000"
    total = "£12,050,000" if rev == "v2" else "£12,500,000"
    cap = "15 per cent." if theirs else "40 per cent."
    claims_months = "12 months" if theirs else "18 months"
    escrow_window = "within 90 days after Completion" if theirs else \
        "within seven months after Completion"
    pay_days = "four (5) Business Days" if agreed else "four (4) Business Days"
    notice_days = "thirty (13) days" if agreed else "thirty (30) days"

    out: list[tuple] = []
    P = lambda text: out.append(("p", text))

    out.append(("centre", "SHARE PURCHASE AGREEMENT"))
    out.append(("centre-plain", (f"relating to the entire issued share capital of "
                                f"{cast.TARGET.upper()}")))
    P("THIS AGREEMENT is dated 30 October 2026")
    P("PARTIES")
    P(f"(1)\t{cast.SELLER.upper()}, a company incorporated in England and Wales with "
      "company number 05512347 whose registered office is at 14 Harbour Row, Southampton "
      "SO14 2AQ (the “Seller”);")
    P(f"(2)\t{buyer.upper()}, a company incorporated in England and Wales with company "
      "number 15893021 whose registered office is at 3 Mercer Yard, London EC1V 4PW (the "
      "“Buyer”); and")
    P(f"(3)\t{cast.CLIENT.upper()}, a company incorporated in England and Wales with "
      f"company number {cast.CLIENT_NUMBER} whose registered office is at 3 Mercer Yard, "
      "London EC1V 4PW (the “Guarantor”).")
    P("BACKGROUND")
    P("(A)\tThe Company is a private company limited by shares. Particulars of the "
      "Company are set out in Schedule 1.")
    P("(B)\tThe Seller is the legal and beneficial owner of the Shares.")
    P("(C)\tThe Seller has agreed to sell and the Buyer has agreed to buy the Shares on "
      "the terms of this agreement, and the Guarantor has agreed to guarantee the "
      "Buyer's obligations under it.")
    P("AGREED TERMS")

    P("1.\tDEFINITIONS AND INTERPRETATION")
    P("1.1\tThe following definitions and rules of interpretation apply in this "
      "agreement.")
    definitions = [
        ("“Accounts” means the audited balance sheet of the Company as at the Accounts "
         "Date and the audited profit and loss account of the Company for the financial "
         "year ended on the Accounts Date, together with the notes to them."),
        "“Accounts Date” means 31 March 2026.",
        ("“Business” means the business of road freight, warehousing and contract "
         "logistics carried on by the Company at the date of this agreement."),
        ("“Business Day” means a day other than a Saturday, Sunday or public holiday in "
         "England when banks in London are open for business."),
        "“Claim” means a Warranty Claim or a claim under the Tax Covenant.",
        "“Company” means " + cast.TARGET + ", a company incorporated in England and "
        "Wales with company number 06634518.",
        ("“Completion” means completion of the sale and purchase of the Shares in "
        f"{t('accordance', 'accordence')} with this agreement."),
        ("“Completion Accounts” means the statement of the Net Working Capital as at "
         "Completion, prepared in accordance with Schedule 2."),
        "“Conditions” means the conditions set out in clause 4.1.",
        ("“Disclosure Letter” means the letter dated the same date as this agreement from "
         "the Seller to the Buyer, with its enclosures."),
        ("“Encumbrance” means any interest or equity of any person (including any right "
         "to acquire, option or right of pre-emption) or any mortgage, charge, pledge, "
         "lien, assignment, hypothecation, security interest, title retention or any "
         "other security agreement or arrangement."),
        ("“Escrow Account” means the interest-bearing account in the joint names of the "
         "Seller's Solicitors and the Buyer's Solicitors opened for the purposes of "
         "clause 7."),
        "“Escrow Amount” means £1,250,000.",
        ("“Intellectual Property Rights” means patents, rights to inventions, "
         "copyright and related rights, trade marks, business names and domain names, "
         "goodwill, rights in designs, rights in computer software, database rights and "
         "all other intellectual property rights, in each case whether registered or "
         "unregistered."),
        ("“Net Working Capital” means the current assets of the Company less its current "
         "liabilities, each determined in accordance with Schedule 2."),
        "“Properties” means the properties listed in Schedule 5.",
        ("“Restricted Business” means the provision of road freight, warehousing or "
         "contract logistics services in the United Kingdom."),
        "“Seller's Solicitors” means " + cast.OC_FIRM + " of 40 Cardinal Place, London "
        "EC4M 7RS.",
        "“Buyer's Solicitors” means " + cast.FIRM + " of 12 Poultry Lane, London EC2R "
        "8AJ.",
        ("“Shares” means the 2,000,000 ordinary shares of £0.10 each in the capital of "
         "the Company, all of which have been issued and are fully paid."),
        ("“Tax” means all forms of taxation and statutory, governmental, state, "
         "provincial, local governmental or municipal impositions, duties, contributions "
         "and levies, whenever and wherever imposed, and all penalties and interest "
         "relating to them."),
        "“Tax Covenant” means the covenant in clause 10.",
        ("“Transaction Documents” means this agreement, the Disclosure Letter and the "
         "escrow letter referred to in clause 7.2."),
        ("“Warranties” means the warranties set out in Schedule 4, and “Warranty” means "
         "any of them."),
        ("“Warranty Claim” means a claim for breach of any of the Warranties. The Buyer "
        f"shall {t('receive', 'recieve')} notice of any matter which may give rise to a "
        "Warranty Claim as soon as reasonably practicable."),
    ]
    for text in definitions:
        P(text)
    P("1.2\tClause, Schedule and paragraph headings shall not affect the interpretation "
      "of this agreement. The Schedules form part of this agreement and shall have "
      "effect as if set out in full in the body of this agreement.")
    P("1.3\tA reference to a company shall include any company, corporation or other "
      "body corporate, wherever and however incorporated or established.")
    P("1.4\tUnless the context otherwise requires, words in the singular shall include "
      "the plural and in the plural shall include the singular.")
    P("1.5\tA reference to a statute or statutory provision is a reference to it as "
      "amended, extended or re-enacted from time to time.")
    P("1.6\tAny words following the terms including, include, in particular or for "
      "example or any similar phrase shall be construed as illustrative and shall not "
      "limit the generality of the related general words.")

    P("2.\tSALE AND PURCHASE")
    P("2.1\tOn the terms of this agreement, the Seller shall sell and the Buyer shall buy "
      "the Shares with full title guarantee, free from all Encumbrances and with all "
      "rights attaching to them at Completion.")
    P(f"2.2\tThe Seller waives any rights of pre-emption over the Shares conferred on it "
      f"by {t('the', 'the the')} articles of association of the Company or in any other "
      "way.")
    P("2.3\tThe Buyer is not obliged to complete the purchase of any of the Shares unless "
      "the purchase of all the Shares is completed simultaneously.")

    P("3.\tCONSIDERATION")
    P("3.1\tThe consideration for the Shares is £12,500,000 (twelve million five hundred "
      "thousand pounds) (the “Purchase Price”), which shall be satisfied as set out in "
      "Schedule 2.")
    if v1:
        P("3.2\tOn the date of this agreement the Buyer shall pay £250,000 (the "
          "“Deposit”) to the Seller's Solicitors, which shall be non-refundable in all "
          "circumstances and shall be credited against the Purchase Price at Completion.")
        P("3.3\tFollowing Completion the Purchase Price shall be reduced by the amount by "
          "which the Net Working Capital shown in the Completion Accounts is less than "
          "£1,400,000, and shall not be increased in any circumstances.")
    else:
        P("3.2\tOn the date of this agreement the Buyer shall pay £250,000 (the "
          "“Deposit”) to the Seller's Solicitors, which shall be refundable to the Buyer "
          "in full if Completion does not occur, save where the Buyer is in material "
          "breach, and shall be credited against the Purchase Price at Completion.")
        P("3.3\tFollowing Completion the Purchase Price shall be adjusted pound for pound "
          "by the amount by which the Net Working Capital shown in the Completion "
          "Accounts is more or less than £1,400,000, whether in favour of the Buyer or "
          "the Seller.")
    P(f"3.4\tAny adjustment under clause 3.3 shall be paid within {pay_days} after the "
      "Completion Accounts are agreed or determined, by electronic transfer to the "
      "account the recipient nominates.")

    P("4.\tCONDITIONS")
    P("4.1\tCompletion is conditional on the Secretary of State having confirmed that no "
      "further action will be taken in relation to the acquisition of the Shares under "
      "the National Security and Investment Act 2021, and on no order having been made "
      "that prohibits Completion.")
    P(f"4.2\tThe Buyer shall use all {t('reasonable', 'reasonabl')} endeavours to satisfy "
      "the Conditions as soon as possible, and the Seller shall provide such information "
      "and assistance as the Buyer may reasonably require for that purpose.")
    P("4.3\tEach party shall notify the other promptly on becoming aware that any of the "
      "Conditions has been satisfied or has become incapable of being satisfied.")
    P(f"4.4\tIf the Conditions have not been satisfied on or before {long_stop} (the "
      "“Long Stop Date”), either the Seller or the Buyer may terminate this agreement by "
      "notice to the other, and no party shall have any claim against another under it, "
      "save for any claim arising from a prior breach.")

    P("5.\tPRE-COMPLETION UNDERTAKINGS")
    P(f"5.1\tFrom the date of this agreement until the earlier of Completion and the Long "
      f"Stop Date, the Seller shall procure "
      f"that the Company carries on the Business in the ordinary course, and that where "
      f"any matter has {t('occurred', 'occured')} which would require the Buyer's consent "
      f"it is recorded in a {t('separate', 'seperate')} log kept for inspection by the "
      "Buyer. In particular the Seller shall procure that the Company does not, without "
      "the prior written consent of the Buyer:")
    for letter, text in zip("abcdefghijkl", [
        "amend its articles of association or pass any shareholder resolution;",
        "allot, issue, buy back or redeem any share or loan capital;",
        "declare, pay or make any dividend or other distribution;",
        "acquire or dispose of any asset with a value of more than £100,000;",
        ("enter into, amend or terminate any contract with a value of more than £250,000 "
         "or which is outside the ordinary course of the Business;"),
        "borrow any money, other than under the Company's existing overdraft facility;",
        "create any Encumbrance over the whole or any part of its business or assets;",
        ("employ or dismiss any employee earning more than £75,000 a year, or vary the "
         "terms of employment of any such employee;"),
        ("commence, settle or waive any litigation or arbitration, other than debt "
         "collection in the ordinary course;"),
        "grant, vary or surrender any lease or licence of any of the Properties;",
        "make any change to its accounting policies or practices; or",
        "agree, conditionally or otherwise, to do any of the foregoing.",
    ], strict=True):
        P(f"({letter})\t{text}")
    P("5.2\tThe Seller shall allow the Buyer and its agents reasonable access to the "
      "Properties and to the books and records of the Company during normal business "
      "hours on reasonable notice.")

    P("6.\tCOMPLETION")
    P("6.1\tCompletion shall take place at the offices of the Seller's Solicitors on the "
      "fifth Business Day after the date on which the last of the Conditions is "
      "satisfied, or at such other place or on such other date as the Seller and the "
      "Buyer agree in writing.")
    P("6.2\tAt Completion the Seller shall deliver or cause to be delivered to the Buyer "
      "the documents and evidence set out in Part 1 of Schedule 3, and the Buyer shall "
      "comply with its obligations in Part 2 of Schedule 3.")
    P(f"6.3\tIf the Seller fails to comply with any of its obligations under clause 6.2, "
      f"the Buyer may, by notice given {t('immediately', 'immediatly')}, defer Completion "
      "to a date not more than 20 Business Days after the date on which Completion would "
      "otherwise have taken place, or proceed to Completion so far as practicable.")
    P("6.4\tThe Buyer shall pay the Purchase Price, less the Deposit and the Escrow "
      "Amount, to the Seller's Solicitors at Completion, and the Seller's Solicitors are "
      "authorised to receive it on the Seller's behalf.")
    P("6.5\tPayment to the Seller's Solicitors in accordance with clause 6.4 shall be a "
      "good discharge to the Buyer of its obligation to pay that part of the Purchase "
      "Price, and the Buyer shall not be concerned to see to its application.")
    P("6.6\tThe Seller shall, for as long as it remains the registered holder of any of "
      "the Shares after Completion, hold those Shares and any distributions, property "
      "and rights deriving from them in trust for the Buyer, and deal with them only as "
      "the Buyer directs.")
    P("6.7\tThe Seller irrevocably appoints the Buyer as its attorney, with effect from "
      "Completion, to exercise all rights attaching to the Shares, to receive notice of "
      "and attend general meetings of the Company, and to sign any document the Buyer "
      "reasonably requires to perfect its title to the Shares.")

    P("7.\tESCROW")
    P("7.1\tAt Completion the Buyer shall pay the Escrow Amount into the Escrow Account.")
    P("7.2\tThe Escrow Account shall be operated in accordance with an escrow letter in "
      "the agreed form between the Seller's Solicitors and the Buyer's Solicitors.")
    P(f"7.3\tThe balance of the Escrow Account shall be released to the Seller "
      f"{escrow_release} after Completion, together with any interest accrued on it.")
    P("7.4\tThe Seller and the Buyer shall each give such instructions to the holders of "
      "the Escrow Account as are needed to give effect to this clause 7.")

    P("8.\tWARRANTIES")
    P("8.1\tThe Seller warrants to the Buyer that each Warranty is true, accurate and not "
      "misleading at the date of this agreement.")
    P(f"8.2\tThe Warranties are qualified by the facts and circumstances fairly disclosed "
      f"in the Disclosure Letter, and the Seller's {t('liability', 'liabilty')} for any "
      "Warranty Claim is limited as set out in clause 9.")
    P("8.3\tThe Warranties shall be deemed to be repeated immediately before Completion "
      "by reference to the facts and circumstances then existing.")
    P(f"8.4\tEach of the Warranties is separate and independent and, except as expressly "
      f"provided {t('in', 'in in')} this agreement, is not limited by reference to any "
      "other Warranty or any other provision of this agreement.")
    P("8.5\tThe Seller shall not make any claim against the Company or any of its "
      "directors or employees on whom it may have relied before agreeing to any term of "
      "this agreement or authorising any statement in the Disclosure Letter.")
    P("8.6\tThe Seller shall promptly disclose in writing to the Buyer any matter or "
      "circumstance which arises or becomes known to it before Completion which is "
      "inconsistent with any of the Warranties, or which is material to be known by a "
      "purchaser of the Shares.")
    P("8.7\tThe rights and remedies of the Buyer in respect of any breach of the "
      "Warranties shall not be affected by Completion, by any investigation made into "
      "the affairs of the Company, or by any failure to exercise any right or remedy.")

    P("9.\tLIMITATIONS ON CLAIMS")
    P(f"9.1\tThe Seller shall not be liable for any individual Warranty Claim unless the "
      f"{t('amount', 'amout')} recoverable in respect of it exceeds £25,000.")
    P("9.2\tThe Seller shall not be liable for any Warranty Claim unless the aggregate "
      "amount of all such claims exceeds £125,000, in which case the Seller shall be "
      "liable for the whole amount and not only the excess.")
    P(f"9.3\tThe aggregate liability of the Seller for all Claims shall not exceed {cap} "
      "of the Purchase Price.")
    if rev != "v2":
        P("9.4\tNone of the limitations in this clause 9 shall apply to any Claim arising "
          "from fraud, dishonesty or wilful concealment by the Seller or any of its "
          "agents.")

    P("10.\tTAX")
    if v1:
        P("10.1\tThe Seller gives no covenant in respect of Tax, and the Buyer shall rely "
          "solely on the Warranties in Part C of Schedule 4 in respect of Tax.")
    else:
        P("10.1\tThe Seller covenants to pay to the Buyer an amount equal to any Tax "
          "liability of the Company arising in respect of any period ending on or before "
          "Completion, pound for pound.")
    P("10.2\tAny payment under the Tax Covenant shall be made within 10 Business Days of "
      "written demand, and in any event no later than the last day on which the relevant "
      "Tax may be paid without incurring interest or penalties.")

    P("11.\tPROTECTION OF GOODWILL")
    P(f"11.1\tThe Seller shall not, for a period of {'12' if v1 else '24'} months after "
      "Completion, carry on or be engaged, concerned or interested in any Restricted "
      "Business.")
    P("11.2\tThe Seller shall not, for the same period, solicit or entice away from the "
      "Company any person who was a customer of the Company in the 12 months before "
      "Completion, or any employee of the Company earning more than £60,000 a year.")
    P("11.3\tEach of the covenants in this clause 11 is a separate undertaking and is "
      "considered reasonable by the parties as necessary to protect the goodwill of the "
      "Business.")

    P("12.\tTIME LIMITS FOR CLAIMS")
    P(f"12.1\tThe Seller shall not be liable for a Warranty Claim unless the Buyer gives "
      f"notice of it to the Seller before the date falling {claims_months} after "
      "Completion.")
    P(f"12.2\tNo claim may be made against the Escrow Amount unless notified to the "
      f"Seller {escrow_window}.")
    P("12.3\tA Warranty Claim notified in accordance with clause 12.1 shall be deemed "
      "withdrawn unless legal proceedings in respect of it have been issued and served "
      "within nine months of the notice.")

    P("13.\tGUARANTEE")
    P(f"13.1\tThe Guarantor irrevocably and unconditionally "
      f"{t('guarantees', 'garantees')} to the Seller the due and punctual performance by "
      "the Buyer of all its obligations under this agreement.")
    P("13.2\tThe liability of the Guarantor under clause 13.1 shall not be affected by "
      "any time or indulgence granted to the Buyer, or by any variation of this "
      "agreement.")

    P("14.\tCONFIDENTIALITY AND ANNOUNCEMENTS")
    P("14.1\tEach party shall keep confidential the terms of this agreement and all "
      "information it has received about the other parties, save as required by law or "
      "by any regulatory body.")
    P(f"14.2\tNo party shall make any {t('announcement', 'anouncement')} relating to "
      "this agreement or its subject matter without the prior written approval of the "
      "other parties.")
    P("14.3\tClauses 14.1 and 14.2 do not apply to any disclosure required by law, by any "
      "governmental or regulatory authority or by the rules of any stock exchange on "
      "which the shares of a party or its holding company are listed, provided that the "
      "party making the disclosure consults the others about its form and timing so far "
      "as it is lawful and practicable to do so.")
    P("14.4\tThe restrictions in this clause 14 continue to apply after Completion and "
      "after the termination of this agreement without limit in time.")

    P("15.\tASSIGNMENT")
    P("15.1\tNo party may assign or transfer any of its rights or obligations under this "
      "agreement without the prior written consent of the others, save that the Buyer "
      "may assign the benefit of the Warranties to a member of its group.")

    P("16.\tCOSTS")
    P(f"16.1\tEach party is {t('responsible', 'responsable')} for its own costs in "
      "connection with the negotiation, preparation and performance of this agreement "
      "and the other Transaction Documents.")
    P("16.2\tThe Seller shall procure that no costs of the Seller in connection with the "
      "sale of the Shares are borne by the Company, and any such costs paid by the "
      "Company before Completion shall be repaid to it by the Seller on demand.")
    P("16.3\tAll sums payable under this agreement shall be paid free and clear of any "
      "deduction or withholding, save as required by law. Where a deduction or "
      "withholding is required by law, the payer shall pay such further amount as will "
      "ensure that the payee receives the same amount as it would have received had no "
      "deduction or withholding been made.")

    P("17.\tNOTICES")
    P("17.1\tA notice given under this agreement shall be in writing and delivered by "
      "hand or by pre-paid first-class post to the following addresses:")
    P(f"Seller: {cast.SELLER}, 14 Harbour Row, Southampton SO14 2AQ, marked for the "
      f"attention of the company secretary, with a copy to {cast.OC_FIRM}.")
    P(f"Buyer: {buyer}, 3 Mercer Yard, London EC1V 4PW, marked for the attention of the "
      f"company secretary, with a copy to {cast.FIRM}.")
    P(f"Guarantor: {cast.CLIENT}, 3 Mercer Yard, London EC1V 4PW.")
    P("17.2\tA notice delivered by hand is deemed received at the time of delivery, and a "
      "notice sent by pre-paid first-class post is deemed received at 9.00 am on the "
      "second Business Day after posting.")
    P(f"17.3\tA party may change its address for notices by giving the others not less "
      f"than {notice_days}' notice of the change.")

    P("18.\tENTIRE AGREEMENT")
    P("18.1\tThis agreement and the other Transaction Documents constitute the entire "
      "agreement between the parties and supersede all previous agreements between them "
      "relating to their subject matter.")
    P("18.2\tEach party acknowledges that in entering into this agreement it does not "
      "rely on, and shall have no remedies in respect of, any statement, representation, "
      "assurance or warranty (whether made innocently or negligently) that is not set "
      "out in this agreement or the other Transaction Documents.")
    P("18.3\tNothing in this clause 18 shall limit or exclude any liability for fraud.")

    P("19.\tVARIATION AND WAIVER")
    P("19.1\tNo variation of this agreement shall be effective unless it is in writing "
      "and signed by the parties or their authorised representatives.")
    P("19.2\tA waiver of any right or remedy under this agreement or by law is only "
      "effective if given in writing and shall not be deemed a waiver of any subsequent "
      "right or remedy. A failure or delay by a party to exercise any right or remedy "
      "shall not constitute a waiver of that or any other right or remedy, nor shall it "
      "prevent or restrict any further exercise of that or any other right or remedy.")
    P("19.3\tIf any provision or part-provision of this agreement is or becomes invalid, "
      "illegal or unenforceable, it shall be deemed modified to the minimum extent "
      "necessary to make it valid, legal and enforceable, and the validity of the rest "
      "of this agreement shall not be affected.")

    P("20.\tCOUNTERPARTS")
    P("20.1\tThis agreement may be executed in any number of counterparts, each of which "
      "when executed shall constitute a duplicate original.")
    P("20.2\tTransmission of an executed counterpart of this agreement by email in PDF "
      "form shall take effect as the delivery of an executed original counterpart. No "
      "counterpart shall be effective until each party has delivered at least one "
      "executed counterpart.")

    P("21.\tGOVERNING LAW AND JURISDICTION")
    P(f"21.1\tThis agreement and any dispute or claim arising out of it shall be "
      f"{t('governed', 'goverened')} by the law of England and Wales, and the courts of "
      "England and Wales shall have exclusive jurisdiction.")
    P("This agreement has been entered into on the date stated at the beginning of it.")

    # Schedules
    out.append(("break",))
    P("SCHEDULE 1")
    P("Particulars of the Company")
    out.append(("table", [
        ("Name", cast.TARGET), ("Registered number", "06634518"),
        ("Registered office", "Unit 7, Kingsway Park, Northampton NN3 6RT"),
        ("Date of incorporation", "11 June 2008"), ("Issued share capital",
                                                      "2,000,000 ordinary shares of £0.10"),
        ("Directors", "Martin Hale, Ruth Adeyemi"), ("Accounting reference date",
                                                     "31 March"),
    ]))
    out.append(("break",))
    P("SCHEDULE 2")
    P("Completion Accounts and satisfaction of the Purchase Price")
    P("Part 1: satisfaction of the Purchase Price")
    out.append(("table", [
        ("Element", "Amount"),
        ("Deposit paid on signing", "£250,000"),
        ("Cash payable at Completion", cash),
        ("Escrow Amount", "£1,250,000"),
        ("Deferred consideration payable on the first anniversary of Completion",
         "£1,500,000"),
        ("Total Purchase Price", total),
    ]))
    P("Part 2: preparation of the Completion Accounts")
    for n, text in enumerate([
        ("The Buyer shall prepare the Completion Accounts within 60 Business Days after "
         "Completion and deliver them to the Seller."),
        ("The Completion Accounts shall be prepared on the same basis and applying the "
         "same accounting policies as the Accounts."),
        ("The Seller may, within 20 Business Days after receipt, give notice that it "
         "disputes the Completion Accounts, specifying each item in dispute."),
        ("Any dispute not resolved within 15 Business Days shall be referred to an "
         "independent firm of chartered accountants, whose decision shall be final."),
    ], 1):
        P(f"{n}.\t{text}")
    out.append(("break",))
    P("SCHEDULE 3")
    P("Completion")
    P("Part 1: the Seller's obligations")
    for n, text in enumerate([
        "a transfer of the Shares duly executed by the Seller in favour of the Buyer;",
        "the share certificates for the Shares;",
        "the written resignations of Martin Hale as director and secretary;",
        "the statutory books of the Company, written up to the time of Completion;",
        "a certified copy of the board minutes of the Seller authorising the sale.",
    ], 1):
        P(f"{n}.\t{text}")
    P("Part 2: the Buyer's obligations")
    for n, text in enumerate([
        "payment of the amounts due at Completion under clause 6.4 and clause 7.1;",
        "a certified copy of the board minutes of the Buyer authorising the purchase.",
    ], 1):
        P(f"{n}.\t{text}")
    out.append(("break",))
    P("SCHEDULE 4")
    P("Warranties")
    warranties = {
        "Part A: the Shares and the Seller": [
            ("The Seller is the sole legal and beneficial owner of the Shares and is "
             "entitled to transfer them free from all Encumbrances."),
            ("The Shares comprise the whole of the allotted and issued share capital of "
             "the Company and are fully paid."),
            ("The Seller has the requisite power and authority to enter into and perform "
             "this agreement and the other Transaction Documents."),
            ("No person has the right to call for the issue of any share or loan capital "
             "of the Company."),
        ],
        "Part B: accounts and trading": [
            ("The Accounts have been prepared in accordance with the accounting standards "
             "applicable to a United Kingdom company and give a true and fair view of the "
             "state of affairs of the Company at the Accounts Date."),
            ("Since the Accounts Date the Company has carried on the Business in the "
             "ordinary and usual course and there has been no material adverse change in "
             "its turnover or financial position."),
            ("The Company has not given any guarantee or indemnity in respect of the "
             "obligations of any other person."),
            ("No customer of the Company which accounted for more than 5 per cent. of its "
             "turnover in the last financial year has ceased or indicated that it will "
             "cease to trade with the Company."),
            ("The Company is not party to any contract which is of an unusual or onerous "
             "nature, or which cannot be terminated by it on 12 months' notice or less."),
            ("The Company's vehicle fleet is roadworthy, properly licensed and maintained "
             "in accordance with its operator's licence."),
            ("The Company holds all licences, consents and permits required to carry on "
             "the Business, and none of them is likely to be revoked."),
            ("The Company is not, and has not been in the last three years, a party to "
             "any litigation, arbitration or administrative proceedings."),
            ("The Company has complied in all material respects with all applicable laws "
             "and regulations, including those relating to data protection."),
            ("The Company owns or has the right to use all the Intellectual Property "
             "Rights used in the Business."),
            ("The Company's information technology systems have not suffered any failure "
             "or security breach which has caused material disruption to the Business."),
        ],
        "Part C: tax": [
            ("The Company has made all returns and supplied all information required to "
             "be supplied to any taxation authority, and all such returns were accurate "
             "and made on time."),
            "The Company has paid all Tax which it has become liable to pay.",
            ("The Company is not, and has not been in the last four years, the subject of "
             "any investigation or enquiry by any taxation authority."),
            ("The Company has properly operated the PAYE system and has deducted and "
             "accounted for Tax on all payments to employees."),
        ],
        "Part D: employees and property": [
            ("Particulars of the terms of employment of every employee earning more than "
             "£60,000 a year have been disclosed to the Buyer."),
            "There is no dispute between the Company and any employee or trade union.",
            ("The Company does not operate and has never operated a defined benefit "
             "pension scheme."),
            ("The Properties comprise all the land and buildings owned, used or occupied "
             "by the Company."),
            ("The Company has complied with all the covenants and obligations affecting "
             "the Properties."),
            ("No notice has been received by the Company affecting any of the Properties "
             "or their use."),
        ],
        "Part E: environment, insurance and data": [
            ("The Company has at all times complied with all applicable environmental laws "
             "and holds every environmental permit needed to operate its depots."),
            ("No hazardous substance has been deposited, disposed of or allowed to escape "
             "at or from any of the Properties."),
            ("The Company's assets are insured against the risks and to the values that a "
             "prudent operator of a similar business would insure, and every policy is in "
             "full force and effect."),
            ("No insurance claim is outstanding, and nothing has been done or omitted "
             "which could make any policy void or voidable."),
            ("The Company has complied with the data protection legislation, has not "
             "received any notice or complaint from the Information Commissioner or any "
             "data subject, and has not suffered any personal data breach."),
            ("The Company's fleet management and warehouse management software is either "
             "owned by the Company or licensed to it on terms which will not be affected "
             "by the sale of the Shares."),
            ("The Company is not in default under any agreement under which it has "
             "borrowed money, and no event has occurred which would entitle a lender to "
             "demand early repayment."),
            ("No director, officer or employee of the Company has offered, paid or "
             "received any bribe, and the Company has adequate procedures to prevent "
             "bribery by persons associated with it."),
        ],
    }
    n = 0
    for part, items in warranties.items():
        P(part)
        for text in items:
            n += 1
            P(f"{n}.\t{text}")
    out.append(("break",))
    P("SCHEDULE 5")
    P("Properties")
    out.append(("table", [
        ("Property", "Tenure", "Use"),
        ("Unit 7, Kingsway Park, Northampton NN3 6RT", "Leasehold", ("Head office and "
                                                                      "warehouse")),
        ("Plot 12, Trafford Wharf, Manchester M17 1AB", "Leasehold", "Cross-dock depot"),
        ("Bay 4, Avonmouth Distribution Park, Bristol BS11 9FG", "Freehold",
         "Warehouse"),
    ]))
    out.append(("break",))
    P("EXECUTION")
    for entity, role in ((cast.SELLER.upper(), "Seller"), (buyer_block.upper(), "Buyer"),
                         (cast.CLIENT.upper(), "Guarantor")):
        P(f"EXECUTED as a deed by {entity} acting by a director in the presence of a "
          "witness:")
        P("Director: ______________________")
        P("Witness: ______________________")
    return out


def spa(rev: str) -> bytes:
    """The agreement as a Word file, in the version asked for."""
    # Harrowgate scrub their metadata before a draft goes out, as most firms
    # do; the review knows it is their paper because it was forwarded from them.
    d = _new("", "Share purchase agreement: " + cast.DEAL, spaced=True)
    # Harrowgate do not number their drafts in the header, so v1 and v2 differ
    # only where the deal does; the version is in the file name.
    label = {"v1": f"{cast.OC_FIRM} draft: subject to contract.",
             "v2": f"{cast.OC_FIRM} draft: subject to contract.",
             "agreed": "Agreed form: 29 September 2026. Subject to contract.",
             "execution": "Execution version"}[rev]
    section = d.sections[0]
    section.header.paragraphs[0].text = label
    section.footer.paragraphs[0].text = "HGL/BETA/0417"
    for item in spa_paragraphs(rev):
        kind = item[0]
        if kind == "p":
            d.add_paragraph(item[1])
        elif kind == "centre":
            _centre(d, item[1], size=14)
        elif kind == "centre-plain":
            _centre(d, item[1], bold=False)
        elif kind == "table":
            _table(d, item[1])
        elif kind == "break":
            d.add_page_break()
    return _save(d)


# --------------------------------------------------------------------------
# 3. Our response to their first draft, with the partner's comments
# --------------------------------------------------------------------------

# Our seven points, each as the associate wrote it into their v1.
# (clause, their words, our words)
OUR_POINTS = [
    ("3.2", "which shall be non-refundable in all circumstances",
     "which shall be refundable to the Buyer in full if Completion does not occur"),
    ("3.3", "and shall not be increased in any circumstances",
     "and shall be increased pound for pound where it is more"),
    ("7.3", "released to the Seller three months after Completion",
     "released to the Seller six months after Completion"),
    ("9.3", "shall not exceed 15 per cent. of the Purchase Price",
     "shall not exceed 100 per cent. of the Purchase Price"),
    ("10.1", "The Seller gives no covenant in respect of Tax",
     "The Seller gives a full covenant in respect of Tax"),
    ("11.1", "for a period of 12 months after",
     "for a period of 36 months after"),
    ("12.1", "before the date falling 12 months after",
     "before the date falling 24 months after"),
]

# The partner's five comments on that markup: (anchor, comment, what the
# associate agent should do). Four are instructions; one is a question for
# the client and is left open.
PARTNER_COMMENTS = [
    ("which shall be refundable to the Buyer in full if Completion does not occur",
     "Add “save where the Buyer is in material breach”: the memo's words.", "edit"),
    ("shall not exceed 100 per cent. of the Purchase Price",
     "Too aggressive to open with. Go in at 50 per cent.", "edit"),
    ("for a period of 36 months after",
     "24 months. 36 will not survive a court.", "edit"),
    ("“Business Day” means a day other than a Saturday",
     "Fine as drafted, no change.", "noop"),
    ("before the date falling 24 months after",
     "Would the client live with 18 months here if we hold the tax period? Ask Owen.",
     "open"),
]


def our_response() -> bytes:
    """Our response to their v1, the associate's draft: their text with our
    seven points written in, and the partner's five comments on it. What
    "turn these" is sent."""
    from secondeye.pipeline.ooxml import RevisionWriter

    d = _new(cast.ASSOCIATE, "Share purchase agreement: " + cast.DEAL, spaced=True)
    section = d.sections[0]
    section.header.paragraphs[0].text = (f"{cast.FIRM} draft for {cast.CLIENT}: 28 September "
                                         "2026. Subject to contract.")
    section.footer.paragraphs[0].text = "CL/NORTHWIND/1042"
    for item in spa_paragraphs("v1"):
        kind = item[0]
        if kind == "p":
            text = item[1]
            for _, theirs, ours in OUR_POINTS:
                text = text.replace(theirs, ours)
            d.add_paragraph(text)
        elif kind == "centre":
            _centre(d, item[1], size=14)
        elif kind == "centre-plain":
            _centre(d, item[1], bold=False)
        elif kind == "table":
            _table(d, item[1])
        elif kind == "break":
            d.add_page_break()
    commenter = RevisionWriter(d, cast.PARTNER)
    for anchor, text, _ in PARTNER_COMMENTS:
        commenter.comment(anchor, text, initials="JP")
    return _save(d)


# --------------------------------------------------------------------------
# 4. The 23:47 attachment: their v2 with the partner's comment still in it
# --------------------------------------------------------------------------

LEFTOVER_COMMENT = "we can go to 2x if pushed"
LEFTOVER_ANCHOR = "exceeds £125,000"


def v2_with_partner_comment() -> bytes:
    """Harrowgate's v2 as the partner annotated it for the team: the file that
    goes to Harrowgate by mistake at 23:47 instead of our v3."""
    from secondeye.pipeline.ooxml import RevisionWriter

    d = Document(BytesIO(spa("v2")))
    props = d.core_properties
    props.last_modified_by = cast.PARTNER
    RevisionWriter(d, cast.PARTNER).comment(LEFTOVER_ANCHOR, LEFTOVER_COMMENT, initials="JP")
    buf = BytesIO()
    d.save(buf)
    return stable(buf.getvalue())


def v3() -> bytes:
    """Our v3, the file that should have gone: v2 with the carve-out back, the
    long-stop restored and the claims window fixed. Built for completeness:
    the story is that it was not attached."""
    return spa("agreed")


# --------------------------------------------------------------------------
# 5. The other closing documents
# --------------------------------------------------------------------------


def disclosure_letter() -> bytes:
    d = _new("", "Disclosure letter: " + cast.DEAL)
    d.sections[0].header.paragraphs[0].text = "Execution version"
    _centre(d, "DISCLOSURE LETTER", size=14)
    d.add_paragraph("This letter is dated 30 October 2026.")
    d.add_paragraph(f"From: {cast.SELLER.upper()} (the “Seller”)")
    d.add_paragraph(f"To: {cast.BUYER.upper()} (the “Buyer”)")
    d.add_paragraph("1.\tIntroduction. This is the Disclosure Letter referred to in the "
                    "share purchase agreement between the Seller, the Buyer and "
                    f"{cast.CLIENT} relating to {cast.TARGET}. Words defined there have "
                    "the same meanings here.")
    d.add_paragraph("2.\tGeneral disclosures. The Seller discloses the contents of the "
                    "data room as at 27 October 2026, the public registers at Companies "
                    "House and the Land Registry, and the Company's statutory books.")
    d.add_paragraph("3.\tSpecific disclosures. Against paragraph 10 of Part B of Schedule "
                    "4: the operator's licence for the Manchester depot is due for "
                    "renewal on 31 January 2027.")
    d.add_paragraph("4.\tAgainst paragraph 8 of Part B of Schedule 4: the claim by "
                    "Midland Pallet Services Limited for £18,400, settled on 2 October "
                    "2026.")
    d.add_paragraph("IN WITNESS WHEREOF this letter has been signed and acknowledged.")
    for entity, who in ((cast.SELLER.upper(), cast.SELLER_DIRECTOR),
                        (cast.BUYER.upper(), cast.BUYER_DIRECTOR)):
        d.add_paragraph(f"Signed for and on behalf of {entity}")
        d.add_paragraph("By: ______________")
        d.add_paragraph(f"Name: {who}")
        d.add_paragraph("Title: Director")
    return _save(d)


def board_minutes() -> bytes:
    d = _new("", "Board minutes: " + cast.BUYER)
    d.sections[0].header.paragraphs[0].text = "Execution version"
    _centre(d, "WRITTEN RESOLUTION OF THE BOARD OF DIRECTORS", size=14)
    d.add_paragraph(f"This resolution of the board of {cast.BUYER.upper()} (the "
                    "“Company”) is dated 30 October 2026 and is made between the "
                    "directors of the Company:")
    d.add_paragraph(f"(1)\t{cast.BUYER.upper()}, a company incorporated in England and "
                    "Wales with company number 15893021 (the “Company”).")
    d.add_paragraph("1.\tApproval. The directors resolve that the acquisition of the "
                    f"entire issued share capital of {cast.TARGET} for £12,500,000 on "
                    "the terms of the share purchase agreement in the agreed form is "
                    "approved.")
    d.add_paragraph(f"2.\tAuthority. {cast.BUYER_DIRECTOR}, a director, is authorised to "
                    "sign the share purchase agreement, the disclosure letter and any "
                    "other document needed for Completion on behalf of the Company.")
    d.add_paragraph("3.\tDeclarations. Each director declared the nature and extent of "
                    "any interest in the transaction as required by the Companies Act "
                    "2006 and the articles.")
    d.add_paragraph("IN WITNESS WHEREOF this resolution has been signed.")
    d.add_paragraph(f"Signed for and on behalf of {cast.BUYER.upper()}")
    d.add_paragraph("By: ______________")
    d.add_paragraph(f"Name: {cast.BUYER_DIRECTOR}")
    d.add_paragraph("Title: Director and chair")
    return _save(d)


# --------------------------------------------------------------------------
# 6. The encore: Beta's counsel's NDA, a week later
# --------------------------------------------------------------------------


def beta_nda() -> bytes:
    d = _new(cast.OC, "Confidentiality agreement: Beta Freight")
    d.core_properties.last_modified_by = cast.OC
    d.sections[0].header.paragraphs[0].text = f"{cast.OC_FIRM} draft: 6 October 2026"
    _centre(d, "CONFIDENTIALITY AGREEMENT", size=14)
    paras = [
        "THIS AGREEMENT is dated 6 October 2026",
        "PARTIES",
        (f"(1)\t{cast.SELLER.upper()}, a company incorporated in England and Wales with "
        "company number 05512347 (the “Discloser”); and"),
        (f"(2)\t{cast.CLIENT.upper()}, a company incorporated in England and Wales with "
        f"company number {cast.CLIENT_NUMBER} (the “Recipient”)."),
        "BACKGROUND",
        ("(A)\tThe Recipient is considering the acquisition of the freight forwarding "
         "business of the Discloser known as Beta Freight (the “Purpose”)."),
        "AGREED TERMS",
        "1.\tConfidential Information",
        ("1.1\t“Confidential Information” means all information of any kind disclosed by "
         "the Discloser to the Recipient in connection with the Purpose, whether before "
         "or after the date of this agreement."),
        "2.\tObligations",
        ("2.1\tThe Recipient shall keep the Confidential Information confidential and use "
         "it only for the Purpose. The Discloser gives no undertaking in respect of any "
         "information disclosed by the Recipient."),
        ("2.2\tThe Recipient's obligations under clause 2.1 continue for 12 months after "
         "the date of this agreement."),
        "3.\tNon-solicitation",
        ("3.1\tThe Recipient shall not, for 18 months after the date of this agreement, "
         "employ or offer to employ any employee of the Discloser, including any employee "
         "who responds to a public advertisement."),
        "4.\tGoverning law",
        ("4.1\tThis agreement is governed by the law of England and Wales, and the courts "
         "of England and Wales have exclusive jurisdiction."),
        "This agreement has been entered into on the date stated at the beginning of it.",
    ]
    for text in paras:
        d.add_paragraph(text)
    for entity, who in ((cast.SELLER.upper(), cast.SELLER_DIRECTOR),
                        (cast.CLIENT.upper(), cast.CLIENT_DIRECTOR)):
        d.add_paragraph(f"Signed for and on behalf of {entity}")
        d.add_paragraph("By: ______________")
        d.add_paragraph(f"Name: {who}")
        d.add_paragraph("Title: Director")
    return _save(d)


# --------------------------------------------------------------------------
# What is planted, and the words that prove it
# --------------------------------------------------------------------------

DEFECTS: list[dict] = [
    {"beat": 2, "file": "v1", "what": "14 typos Harrowgate left in their first draft",
     "present": [w for _, w in TYPOS]},
    {"beat": 4, "file": "v2", "what": "the fraud carve-out in 9.4 is deleted",
     "absent": ["None of the limitations in this clause 9 shall apply"]},
    {"beat": 4, "file": "v2", "what": "the long-stop date moves from 31 March to 31 January",
     "present": ["on or before 31 January 2027 (the “Long Stop Date”)"],
     "absent": ["31 March 2027"]},
    {"beat": 4, "file": "v2", "what": "the cap is still 15 per cent. (our point rejected)",
     "present": ["shall not exceed 15 per cent. of the Purchase Price"]},
    {"beat": 4, "file": "v2",
     "what": "the claims period is still 12 months (our point rejected, not mentioned)",
     "present": ["before the date falling 12 months after"]},
    {"beat": 5, "file": "v2", "what": "Schedule 2 totals £12,050,000 against £12,500,000 "
                                      "in clause 3.1",
     "present": ["£12,500,000 (twelve million", "£12,050,000"]},
    {"beat": 5, "file": "v2",
     "what": "the 90-day claims window in 12.2 closes before the six-month escrow release "
             "in 7.3",
     "present": ["within 90 days after Completion",
                 "released to the Seller six months after Completion"]},
    {"beat": 6, "file": "v2-jp", "what": "J. Partner's comment left in the file",
     "comment": LEFTOVER_COMMENT},
    {"beat": 7, "file": "agreed", "what": "thirty (13) days", "present": ["thirty (13) days"]},
    {"beat": 7, "file": "agreed", "what": "four (5) Business Days",
     "present": ["four (5) Business Days"]},
    {"beat": 7, "file": "agreed", "what": "the Buyer's signature block still says Falcon "
                                          "Holdings Limited",
     "present": ["(2)\tFALCON TOPCO LIMITED",
                 "EXECUTED as a deed by FALCON HOLDINGS LIMITED"]},
]
