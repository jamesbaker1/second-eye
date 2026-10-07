from datetime import UTC, datetime
from io import BytesIO

import pytest
from docx import Document

from lra.models import Attachment, InboundEmail, Mode
from lra.pipeline import intake

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def mk(attachments=None, body="", subject="") -> InboundEmail:
    return InboundEmail(
        message_id="m1",
        from_address="jim@example.com",
        subject=subject,
        text_body=body,
        attachments=attachments or [],
        received_at=datetime.now(UTC),
    )


def docx_bytes(text="Some contract text.") -> bytes:
    """Real Word bytes. Selection is by content now, so fixtures must be real."""
    d = Document()
    d.add_paragraph(text)
    buf = BytesIO()
    d.save(buf)
    return buf.getvalue()


PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def att(name, size=None, ctype=DOCX, content=None) -> Attachment:
    if content is None:
        if ctype == "application/pdf" or name.lower().endswith(".pdf"):
            content = PDF_BYTES
        elif ctype.startswith("image/") or name.lower().endswith(".png"):
            content = PNG_BYTES
        else:
            content = docx_bytes()
    if size is not None and size > len(content):
        # Pad inside the package name so the file stays a valid document.
        content = content + b"\n" * (size - len(content))
    return Attachment(filename=name, content_type=ctype,
                      size_bytes=size or len(content), content=content)


def test_no_attachment_is_rejected_with_a_readable_sentence():
    with pytest.raises(intake.Rejection) as e:
        intake.pick_document(mk())
    assert ".docx" in str(e.value)


def test_signature_image_is_not_mistaken_for_the_document():
    with pytest.raises(intake.Rejection):
        intake.pick_document(mk([att("logo.png", ctype="image/png")]))


def test_single_docx_is_picked():
    assert intake.pick_document(mk([att("SPA.docx")])).filename == "SPA.docx"


def test_one_docx_among_pdfs_wins():
    e = mk([att("SPA.docx"), att("exhibit.pdf", ctype="application/pdf")])
    assert intake.pick_document(e).filename == "SPA.docx"


def test_two_docx_asks_rather_than_guesses():
    with pytest.raises(intake.Rejection):
        intake.pick_document(mk([att("a.docx"), att("b.docx")]))


def test_memo_hint_suppresses_redlining():
    assert intake.infer_mode(mk([att("a.docx")], body="just tell me, don't edit it")) is Mode.MEMO_ONLY


def test_default_is_redline():
    assert intake.infer_mode(mk([att("a.docx")], body="have a look")) is Mode.REDLINE


def test_quoted_reply_tail_is_stripped_from_instructions():
    body = "Focus on the indemnity.\n\nOn Tue someone wrote:\n> old thread noise"
    assert intake.extract_instructions(mk(body=body)) == "Focus on the indemnity."


# --- which review was asked for -------------------------------------------

def test_a_memorandum_of_understanding_is_not_a_request_for_a_memo():
    """"memo" matched as a substring, so one of the commonest documents a firm
    reviews came back with no tracked changes at all."""
    e = mk([att("MOU.docx")], subject="Memorandum of Understanding - Falcon - please review")
    assert intake.infer_mode(e) is Mode.REDLINE


def test_memorialise_is_not_a_request_for_a_memo():
    e = mk([att("a.docx")], body="Memorialise the agreed cap in clause 4 and send back")
    assert intake.infer_mode(e) is Mode.REDLINE


def test_a_memo_somebody_sent_on_tuesday_is_not_an_instruction():
    e = mk([att("a.docx")], body="See my memo from Tuesday for background. Draft attached.")
    assert intake.infer_mode(e) is Mode.REDLINE


def test_asking_about_typos_and_the_substance_gets_both():
    """Matching on the word "typos" alone silently dropped the indemnity
    question, which is the expensive direction to be wrong in."""
    e = mk([att("a.docx")],
           body="Can you check for typos and also whether the indemnity is acceptable?")
    assert intake.infer_mode(e) is Mode.REDLINE


def test_typos_only_still_means_a_proofread():
    assert intake.infer_mode(mk([att("a.docx")], body="Typos only please")) is Mode.PROOFREAD


def test_an_instruction_in_a_forwarded_block_is_not_ours_to_follow():
    """The counterparty's covering note travels with a forward. Their "no
    redline needed" is not an instruction to us."""
    body = (
        "Please review before I send this back.\n\n"
        "---------- Forwarded message ---------\n"
        "From: Beta Counsel <counsel@betacorp.com>\n"
        "No redline needed, just tell me if you are happy with it.\n"
    )
    assert intake.infer_mode(mk([att("a.docx")], body=body)) is Mode.REDLINE


def test_do_not_edit_beats_fix_the_typos():
    """Two instructions in one email, and the conservative one wins: editing a
    document we were told to leave alone costs more than not editing one."""
    e = mk([att("a.docx")], body="Don't edit it, but tell me about typos only.")
    assert intake.infer_mode(e) is Mode.MEMO_ONLY


def test_no_allowlist_means_the_firm_not_everyone(monkeypatch):
    """The agent's address is visible whenever it is CC'd. An empty allowlist
    used to review a counterparty's reply-all and mail them the firm's view."""
    from lra.config import settings

    monkeypatch.setenv("ALLOWED_SENDERS", "")
    monkeypatch.setenv("FIRM_DOMAINS", "firm.com")
    settings.cache_clear()
    try:
        inside, outside = mk(), mk()
        inside.from_address = "jim@firm.com"
        outside.from_address = "counsel@betacorp.com"
        intake.check_sender(inside)
        with pytest.raises(intake.Rejection):
            intake.check_sender(outside)
    finally:
        settings.cache_clear()


def test_a_contact_card_is_never_the_document():
    """A forward carrying a vCard picked John.vcf as the thing to review."""
    e = mk([att("John.vcf", ctype="text/vcard", content=b"BEGIN:VCARD\nFN:John\nEND:VCARD"),
            att("SPA.docx")])
    assert intake.pick_document(e).filename == "SPA.docx"


def test_a_draft_and_its_blackline_reviews_the_draft():
    e = mk([att("SPA v3 (blackline against v2).docx"), att("SPA v3.docx")])
    assert intake.pick_document(e).filename == "SPA v3.docx"


def test_a_forwarded_subject_does_not_ask_for_a_comparison():
    e = mk([att("SPA.docx")], subject="Fwd: SPA - revised draft and blackline")
    assert not intake.wants_comparison(e)


def test_forward_as_attachment_finds_the_document_inside():
    """Outlook wraps the original as message/rfc822, which decoded to nothing."""
    from email.message import EmailMessage

    from lra.mail.console import ConsoleProvider

    inner = EmailMessage()
    inner["From"], inner["To"], inner["Subject"] = "a@x.com", "b@y.com", "SPA"
    inner.set_content("see attached")
    inner.add_attachment(b"PK\x03\x04docx", maintype="application",
                         subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
                         filename="SPA.docx")
    outer = EmailMessage()
    outer["From"], outer["To"], outer["Subject"] = "jim@firm.com", "review@firm.com", "FW: SPA"
    outer["Message-ID"] = "<o@x>"
    outer.set_content("please review")
    outer.add_attachment(inner)
    parsed = ConsoleProvider().parse_eml(outer.as_bytes())
    assert [a.filename for a in parsed.attachments] == ["SPA.docx"]
