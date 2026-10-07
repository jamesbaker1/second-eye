"""The signature pack: an execution copy and a signature page per party, or
exactly what is in the way.

Everything a lawyer would open is real: the routing, the scrubber, the pages.
The PDF conversion is faked where LibreOffice is not installed.
"""

from __future__ import annotations

import zipfile
from datetime import UTC, datetime
from io import BytesIO

import pytest
from docx import Document
from docx.enum.text import WD_COLOR_INDEX
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from lra import convert, handler, thread
from lra.mail.console import ConsoleProvider
from lra.models import Attachment, Finding, InboundEmail, Severity
from lra.pipeline import intake, reply, sigpack

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

FRONT = [
    "SHARE PURCHASE AGREEMENT",
    "This Agreement is dated 3 March 2026 and is made between:",
    '(1) ACME HOLDINGS LIMITED, a company incorporated in England (the "Seller"); and',
    '(2) BETA TRADING LLC, a Delaware limited liability company (the "Buyer").',
    "1. Sale. The Seller shall sell the Shares to the Buyer.",
    "2. Price. The price is £1,000,000.",
    "3. Confidentiality. The Buyer shall keep the Seller's secrets secret.",
    "IN WITNESS WHEREOF the parties have signed this Agreement on the date above.",
]
SIGNATORIES = (("ACME HOLDINGS LIMITED", "Jane Smith"), ("BETA TRADING LLC", "Bob Jones"))


class Captured(ConsoleProvider):
    def __init__(self):
        self.sent = []

    def send(self, email_out):
        self.sent.append(email_out)
        return f"<sent-{len(self.sent)}@test>"


@pytest.fixture
def captured(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/sigpack.sqlite3")
    monkeypatch.setenv("MAIL_AGENT_ADDRESS", "review@legal.firm.com")
    monkeypatch.setenv("FIRM_DOMAINS", "firm.com")
    from lra.config import settings

    settings.cache_clear()
    provider = Captured()
    monkeypatch.setattr(handler, "get_provider", lambda: provider)
    yield provider
    settings.cache_clear()


@pytest.fixture(autouse=True)
def no_libreoffice(monkeypatch):
    """Word pages unless a test says otherwise, whatever this machine has."""
    monkeypatch.setattr(convert, "available", lambda: False)


def _save(d) -> bytes:
    buf = BytesIO()
    d.save(buf)
    return buf.getvalue()


def agreement(front=FRONT, signatories=SIGNATORIES, in_table=False) -> bytes:
    d = Document()
    for text in front:
        d.add_paragraph(text)
    if in_table:
        table = d.add_table(rows=1, cols=len(signatories))
        for cell, (entity, who) in zip(table.rows[0].cells, signatories, strict=True):
            cell.paragraphs[0].text = entity
            for line in ("By: ______________", f"Name: {who}", "Title: Director"):
                cell.add_paragraph(line)
    else:
        for entity, who in signatories:
            d.add_paragraph(entity)
            d.add_paragraph("By: ______________")
            d.add_paragraph(f"Name: {who}")
            d.add_paragraph("Title: Director")
    return _save(d)


def texts(content: bytes) -> list[str]:
    d = Document(BytesIO(content))
    out = [p.text for p in d.paragraphs]
    for t in d.tables:
        for row in t.rows:
            for cell in row.cells:
                out += [p.text for p in cell.paragraphs]
    return out


def header(content: bytes) -> str:
    return " ".join(p.text for p in Document(BytesIO(content)).sections[0].header.paragraphs)


def attach(name: str, content: bytes) -> Attachment:
    return Attachment(filename=name, content_type=DOCX, size_bytes=len(content), content=content)


def email(body: str, *attachments: Attachment, message_id="s-1", in_reply_to=None) -> InboundEmail:
    return InboundEmail(
        message_id=message_id, from_address="jim@firm.com",
        to=["review@legal.firm.com"], subject="Project Falcon SPA",
        text_body=body, attachments=list(attachments),
        received_at=datetime.now(UTC), in_reply_to=in_reply_to,
    )


# --- asking for it ----------------------------------------------------------


@pytest.mark.parametrize("body", [
    "sig pages", "Signature pages please", "signature pack", "execution version",
    "Can you get this ready to sign?", "Could you prepare the execution version?",
    "send me the sig pages",
])
def test_the_ways_a_lawyer_asks(body):
    assert intake.wants_signature_pack(email(body))


@pytest.mark.parametrize("body", [
    "Is this ready to sign?",
    "Attaching the execution version for review.",
    "Please sign the signature pages and return them.",
    "Please find attached the signature pages.",
    "Execution version attached.",
])
def test_describing_a_document_is_not_asking_for_a_pack(body):
    assert not intake.wants_signature_pack(email(body))


# --- the pack ---------------------------------------------------------------


def test_one_page_per_party_headed_with_the_agreement_and_its_date():
    pack = sigpack.build(agreement(), "Falcon SPA v3.docx")

    assert pack.heading == "Signature page to Share Purchase Agreement dated 3 March 2026"
    assert [p.party for p in pack.pages] == ["Acme Holdings", "Beta Trading"]
    assert [p.filename for p in pack.pages] == [
        "Falcon SPA v3 - signature page (Acme Holdings).docx",
        "Falcon SPA v3 - signature page (Beta Trading).docx",
    ]
    acme, beta = (p.content for p in pack.pages)
    assert header(acme) == pack.heading
    assert "Name: Jane Smith" in texts(acme)
    assert "Name: Bob Jones" not in texts(acme)
    assert "Name: Bob Jones" in texts(beta)
    assert "BETA TRADING LLC" in texts(beta)
    # The testimonium comes with each page; the agreement's clauses do not.
    assert FRONT[-1] in texts(acme)
    assert not any("Confidentiality" in t for t in texts(acme) + texts(beta))


def test_a_page_carries_nothing_else_of_the_agreement_in_its_package():
    d = Document(BytesIO(agreement()))
    d.sections[0].header.paragraphs[0].text = "DRAFT: Project Falcon, privileged"
    pack = sigpack.build(_save(d), "Falcon SPA.docx")
    with zipfile.ZipFile(BytesIO(pack.pages[0].content)) as z:
        everything = b"".join(z.read(n) for n in z.namelist())
    assert b"Confidentiality" not in everything
    assert b"privileged" not in everything


def test_signature_blocks_side_by_side_in_a_table():
    pack = sigpack.build(agreement(in_table=True), "Falcon SPA.docx")

    assert [p.party for p in pack.pages] == ["Acme Holdings", "Beta Trading"]
    acme, beta = (texts(p.content) for p in pack.pages)
    assert "Name: Jane Smith" in acme and "Name: Bob Jones" not in acme
    assert "Name: Bob Jones" in beta and "Name: Jane Smith" not in beta


def test_rows_of_a_table_one_party_each_keep_the_table():
    d = Document()
    for text in FRONT:
        d.add_paragraph(text)
    table = d.add_table(rows=2, cols=2)
    for row, (entity, who) in zip(table.rows, SIGNATORIES, strict=True):
        row.cells[0].paragraphs[0].text = f"SIGNED by {who} for and on behalf of {entity}"
        row.cells[1].paragraphs[0].text = "______________"
        row.cells[1].add_paragraph("Director")
    pack = sigpack.build(_save(d), "Falcon SPA.docx")

    acme = Document(BytesIO(pack.pages[0].content))
    assert len(acme.tables) == 1 and len(acme.tables[0].rows) == 1
    assert "Jane Smith" in acme.tables[0].rows[0].cells[0].text
    assert "Bob Jones" not in " ".join(texts(pack.pages[0].content))


def _tracked_insertion(content: bytes, paragraph: int, text: str) -> bytes:
    d = Document(BytesIO(content))
    p = d.paragraphs[paragraph]._p
    ins = OxmlElement("w:ins")
    ins.set(qn("w:id"), "901")
    ins.set(qn("w:author"), "A Associate")
    run = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    t.text = text
    run.append(t)
    ins.append(run)
    p.append(ins)
    return _save(d)


def test_the_execution_copy_has_the_changes_accepted_and_comments_out():
    d = Document(BytesIO(_tracked_insertion(agreement(), 5, " plus VAT")))
    d.add_comment(d.paragraphs[5].runs, text="Is this price agreed?", author="A Associate")
    pack = sigpack.build(_save(d), "Falcon SPA.docx")

    with zipfile.ZipFile(BytesIO(pack.execution)) as z:
        assert "word/comments.xml" not in z.namelist()
        assert b"<w:ins " not in z.read("word/document.xml")
    assert "2. Price. The price is £1,000,000. plus VAT" in texts(pack.execution)
    assert pack.execution_name == "Falcon SPA (execution).docx"
    assert any("comment" in r for r in pack.removed)


def test_no_date_on_the_front_page_leaves_the_heading_undated():
    front = [FRONT[0], "This Agreement is made between:"] + FRONT[2:]
    pack = sigpack.build(agreement(front=front), "Falcon SPA.docx")
    assert pack.heading == "Signature page to Share Purchase Agreement"


# --- what stops it ----------------------------------------------------------


def test_a_drafting_note_and_a_blank_stop_it_clause_first():
    front = list(FRONT)
    front[5] = "2. Price. The price is £1,000,000. [NTD: Seller to confirm tax treatment]"
    signatories = (SIGNATORIES[0], ("BETA TRADING LLC", "[NAME]"))
    with pytest.raises(sigpack.Blocked) as e:
        sigpack.build(agreement(front=front, signatories=signatories), "Falcon SPA.docx")
    assert e.value.blockers == [
        "Clause 2: drafting note [NTD: Seller to confirm tax treatment] is still in",
        "Signature page: [NAME] is still blank",
    ]


def test_highlighted_brackets_stop_it():
    d = Document(BytesIO(agreement()))
    run = d.paragraphs[6].add_run(" [for a period of two years]")
    run.font.highlight_color = WD_COLOR_INDEX.YELLOW
    with pytest.raises(sigpack.Blocked) as e:
        sigpack.build(_save(d), "Falcon SPA.docx")
    assert e.value.blockers == [
        "Clause 3: highlighted [for a period of two years] is still in"]


def test_a_block_dated_where_another_is_not_stops_it():
    d = Document()
    for text in FRONT:
        d.add_paragraph(text)
    for entity, who, date in (("ACME HOLDINGS LIMITED", "Jane Smith", "3 March 2026"),
                              ("BETA TRADING LLC", "Bob Jones", "")):
        for line in (entity, "By: ______________", f"Name: {who}", "Title: Director",
                     f"Date: {date}"):
            d.add_paragraph(line)
    with pytest.raises(sigpack.Blocked) as e:
        sigpack.build(_save(d), "Falcon SPA.docx")
    assert e.value.blockers == [(
        "Signature page: the date is filled in for Acme Holdings but blank "
        "for Beta Trading")]


def test_a_block_on_behalf_of_nobody_stops_it():
    d = Document()
    for text in FRONT:
        d.add_paragraph(text)
    for line in ("SIGNED by Jane Smith for and on behalf of ACME HOLDINGS LIMITED",
                 "______________", "Director",
                 "SIGNED by Bob Jones for and on behalf of", "______________", "Director"):
        d.add_paragraph(line)
    with pytest.raises(sigpack.Blocked) as e:
        sigpack.build(_save(d), "Falcon SPA.docx")
    assert any("on behalf of nobody" in b for b in e.value.blockers)


# --- by email ---------------------------------------------------------------


def test_asked_by_email_it_arrives_with_the_verdict_first(captured):
    handler.handle(email("sig pages please", attach("Falcon SPA.docx", agreement())))

    out = captured.sent[-1]
    assert out.text_body.startswith(
        "Ready to sign: execution copy plus 2 signature pages (Acme Holdings, Beta Trading).")
    assert [a.filename for a in out.attachments] == [
        "Falcon SPA (execution).docx",
        "Falcon SPA - signature page (Acme Holdings).docx",
        "Falcon SPA - signature page (Beta Trading).docx",
    ]
    assert "PDF conversion isn't available here" in out.text_body
    assert out.to == ["jim@firm.com"]


def test_pdfs_when_conversion_is_available(captured, monkeypatch):
    monkeypatch.setattr(convert, "available", lambda: True)
    monkeypatch.setattr(convert, "_convert", lambda content, name, target: b"%PDF-1.7 page")
    handler.handle(email("get this ready to sign", attach("Falcon SPA.docx", agreement())))

    out = captured.sent[-1]
    pages = out.attachments[1:]
    assert [a.filename for a in pages] == [
        "Falcon SPA - signature page (Acme Holdings).pdf",
        "Falcon SPA - signature page (Beta Trading).pdf",
    ]
    assert all(a.content_type == "application/pdf" for a in pages)
    assert "PDF" not in out.text_body


def test_blocked_by_email_nothing_is_attached(captured):
    front = list(FRONT)
    front[1] = "This Agreement is dated [●] 2026 and is made between:"
    handler.handle(email("execution version", attach("Falcon SPA.docx",
                                                      agreement(front=front))))

    out = captured.sent[-1]
    assert out.text_body.startswith("Not ready to sign. 1 thing to settle first.")
    assert "Front page: [●] is still blank" in out.text_body
    assert out.attachments == []


def test_over_the_size_limit_the_largest_is_left_off_and_said(captured, monkeypatch):
    monkeypatch.setattr(reply, "MAX_ATTACHMENT_BYTES", 1)
    handler.handle(email("sig pages", attach("Falcon SPA.docx", agreement())))
    out = captured.sent[-1]
    assert out.attachments == []
    assert "too large to attach with the rest" in out.text_body


def test_a_question_still_open_on_the_thread_stops_it(captured):
    thread.start("t-sig", "jim@firm.com", "Falcon SPA.docx", agreement())
    thread.record_questions("t-sig", 1, [Finding(
        severity=Severity.BLOCKER, category="amount", title="Price", explanation="",
        anchor="£1,000,000", question="Is the price £1,000,000 or £100,000?")])
    status = sigpack.handle(email("sig pages"), captured, thread.load("t-sig"))

    assert status == "signature pack blocked"
    assert ("Still open from my review: Is the price £1,000,000 or £100,000?"
            in captured.sent[-1].text_body)


def test_sig_pages_as_a_reply_on_a_review_thread(captured):
    thread.start("t-reply", "jim@firm.com", "Falcon SPA.docx", agreement())
    thread.add_alias("t-reply", "<review-1@test>")
    handler.handle(email("sig pages", message_id="<s-2@test>", in_reply_to="<review-1@test>"))

    out = captured.sent[-1]
    assert out.text_body.startswith("Ready to sign: execution copy plus 2 signature pages")
    assert len(out.attachments) == 3
