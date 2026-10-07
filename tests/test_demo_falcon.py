"""The Project Falcon demo kit (demos/falcon): built the same way every time,
every planted defect present and caught by what is meant to catch it, and the
whole story rehearsed through the real handler with the model scripted, each
reply carrying the line the audience is meant to see.
"""

from __future__ import annotations

import email
import email.policy
import re
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfReader

from demos.falcon import build, cast, documents, mail, rehearse, signing
from lra.models import Attachment
from lra.pipeline import checks, closing_docs, extract, timeline, wordcomments

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.fixture(scope="module")
def files() -> dict[str, tuple[str, bytes]]:
    return build.build_files()


def read(content: bytes, name: str = "x.docx"):
    return extract.extract(Attachment(filename=name, content_type=DOCX,
                                      size_bytes=len(content), content=content))


def text_of(content: bytes) -> str:
    return "\n".join(b.text for b in read(content).blocks)


# --- the build ---------------------------------------------------------------


def test_the_build_writes_the_same_bytes_twice(tmp_path):
    first = {p.relative_to(tmp_path / "a"): p.read_bytes()
             for p in build.build(tmp_path / "a")}
    second = {p.relative_to(tmp_path / "b"): p.read_bytes()
              for p in build.build(tmp_path / "b")}
    assert first == second
    names = {str(p) for p in first}
    assert len([n for n in names if n.endswith(".eml")]) == len(mail.steps())
    assert len([n for n in names if n.endswith(".docx")]) == len(build.FILES)


def test_every_email_attaches_files_the_build_makes_and_threads_under_an_earlier_step(files):
    seen: set[str] = set()
    for step in mail.steps():
        for key in step.get("attachments") or []:
            assert key in files, (step["id"], key)
        if step.get("reply_to"):
            assert str(step["reply_to"]) in seen, step["id"]
        assert step.get("headline"), step["id"]
        seen.add(str(step["id"]))


def test_an_eml_opens_as_the_email_it_describes(files):
    raw = mail.build_message(mail.step("6"), files)
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    assert msg["From"] == f"{cast.LAWYER_NAME} <{cast.LAWYER}>"
    assert "dana.whitlock@harrowgate.example" in msg["To"]
    assert msg["Bcc"] == f"Redline Desk <{cast.AGENT}>"
    [attached] = list(msg.iter_attachments())
    assert attached.get_filename() == "Project Falcon - SPA v2 (Harrowgate) - JP notes.docx"
    assert attached.get_payload(decode=True) == files["spa_v2_jp"][1]
    delivered = mail.build_message(mail.step("6"), files, with_bcc=False)
    assert b"Bcc:" not in delivered
    reply = mail.build_message(mail.step("7b"), files, in_reply_to="<r@x>")
    parsed = email.message_from_bytes(reply, policy=email.policy.default)
    assert parsed["In-Reply-To"] == "<r@x>"
    assert parsed["Subject"] == "Re: Falcon SPA - agreed form"


def test_every_address_in_the_story_is_invented_or_the_lawyers_own():
    raw = mail.EMAILS.read_text() + Path(documents.__file__).read_text()
    addresses = set(re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", raw))
    assert addresses
    for address in addresses:
        assert address in (cast.LAWYER, cast.AGENT) or address.endswith(".example"), address


# --- every planted defect is there ---------------------------------------------


KEY = {"v1": "spa_v1", "v2": "spa_v2", "agreed": "spa_agreed", "v2-jp": "spa_v2_jp"}


@pytest.mark.parametrize("defect", documents.DEFECTS, ids=lambda d: d["what"][:40])
def test_every_planted_defect_is_present(files, defect):
    content = files[KEY[defect["file"]]][1]
    text = text_of(content)
    for words in defect.get("present", []):
        assert words in text, words
    for words in defect.get("absent", []):
        assert words not in text, words
    if "comment" in defect:
        assert [(c.author, c.text) for c in wordcomments.read(content)] == [
            (cast.PARTNER, defect["comment"])]


def test_harrowgate_left_fourteen_typos_and_the_agreed_form_none(files):
    v1, agreed = text_of(files["spa_v1"][1]), text_of(files["spa_agreed"][1])
    assert len(documents.TYPOS) == 14
    for _, wrong in documents.TYPOS:
        typo = re.compile(rf"\b{re.escape(wrong)}\b")
        assert typo.search(v1), wrong
        assert not typo.search(agreed), wrong


def test_their_first_draft_is_otherwise_clean(files):
    """The review of v1 is about positions: the only mechanical findings are
    two of the typos, and nothing blocks."""
    content = files["spa_v1"][1]
    found = checks.run_all(read(content), content)
    assert sorted(f.title for f in found) == ['"in in" repeats a word',
                                             '"the the" repeats a word']


def test_v2s_price_does_not_add_up_and_its_claims_window_closes_first(files):
    content = files["spa_v2"][1]
    doc = read(content)
    arithmetic = [f.title for f in checks.run_all(doc, content) if f.category == "arithmetic"]
    assert arithmetic == [("The Purchase Price is £12,500,000 in clause 3.1, but Schedule 2 "
                          "totals £12,050,000")]
    flags = {f["kind"]: f for f in timeline.build(doc)["flags"]}
    assert flags["claims-before-release"]["where"] == ["clause 12.2", "clause 7.3"]
    # Their v1 had a three-month escrow, and the window fitted.
    assert "claims-before-release" not in {f["kind"] for f in
                                           timeline.build(read(files["spa_v1"][1]))["flags"]}


def test_the_agreed_form_carries_the_three_questions_and_the_execution_version_none(files):
    agreed = files["spa_agreed"][1]
    questions = sorted(f.question for f in checks.run_all(read(agreed), agreed) if f.question)
    assert questions == ["Is the Buyer Falcon Topco Limited or Falcon Holdings Limited?",
                         "Should this be 30 or 13?", "Should this be 4 or 5?"]
    execution = files["spa_execution"][1]
    assert [f for f in checks.run_all(read(execution), execution)
            if f.severity.value == "blocker"] == []


def test_the_partner_left_five_comments_on_our_response(files):
    found = wordcomments.read(files["response_commented"][1])
    assert [c.author for c in found] == [cast.PARTNER] * 5
    assert [c.text for c in found] == [text for _, text, _ in documents.PARTNER_COMMENTS]


def test_the_signed_pages_are_the_packets_pages_signed(files):
    beta = files["signed_beta"][1]
    ours = files["signed_northwind"][1]
    assert len(PdfReader(BytesIO(beta)).pages) == 2
    assert len(PdfReader(BytesIO(ours)).pages) == 4
    stamps = [closing_docs.receive(pdf, "x.pdf", n)[1]["stamp"]["page"]
              for pdf, count in ((beta, 2), (ours, 4)) for n in range(1, count + 1)]
    assert stamps == ["spa--beta-director", "dl--beta-director", "spa--topco-director",
                      "dl--topco-director", "board--topco-director",
                      "spa--northwind-director"]
    # Every page the plan issues comes back exactly once.
    planned = {f"{item['document']}--{s['id']}" for s in signing.PLAN["signatories"]
               for item in s["sign"]}
    assert set(stamps) == planned


# --- the rehearsal --------------------------------------------------------------


@pytest.fixture(scope="module")
def rehearsal(tmp_path_factory):
    import os

    from lra.config import settings

    # The module-scoped run cannot use the per-test storage fixture, so it
    # keeps its own database file under its own folder either way.
    before = dict(os.environ)
    try:
        yield rehearse.run(tmp_path_factory.mktemp("falcon") / "out", echo=lambda *a: None)
    finally:
        os.environ.clear()
        os.environ.update(before)
        settings.cache_clear()


def test_every_step_is_answered_with_its_headline(rehearsal):
    for step in mail.steps():
        got = rehearsal[str(step["id"])]
        assert got["replies"], step["id"]
        assert got["missing"] == [], (step["id"], got["replies"][0].text_body)


def test_every_reply_goes_to_the_lawyer_alone(rehearsal):
    for step in mail.steps():
        for reply in rehearsal[str(step["id"])]["replies"]:
            assert reply.to == [cast.LAWYER] and reply.cc == []


def test_the_lie_detector_reads_v2_against_our_seven_points(rehearsal):
    body = rehearsal["4"]["replies"][0].text_body
    assert body.startswith("Not what they said. Besides the cap, v2 ")
    assert ("5 of your 7 points were accepted; 2 were not — and one of those isn't "
            "mentioned in their email.") in body
    assert "Clause 12.1: Warranty claims period. Rejected, not mentioned in their email" in body


def test_the_save_names_the_wrong_file_and_the_comment(rehearsal):
    body = rehearsal["6"]["replies"][0].text_body
    assert body.startswith("Already sent, with 3 errors in it.")
    assert "This already went to dana.whitlock@harrowgate.example" in body
    assert "Comments from J. Partner will be visible" in body


def test_the_answers_land_and_the_signature_block_is_fixed(rehearsal):
    from docx import Document

    clean = next(a for a in rehearsal["7d"]["replies"][0].attachments
                 if a.filename.endswith("(clean).docx"))
    text = "\n".join(p.text for p in Document(BytesIO(clean.content)).paragraphs)
    assert "thirty (30) days" in text and "four (4) Business Days" in text
    assert "EXECUTED as a deed by FALCON TOPCO LIMITED" in text
    assert "FALCON HOLDINGS" not in text


def test_the_closing_ends_in_the_executed_set(rehearsal):
    [reply] = rehearsal["7g"]["replies"]
    names = sorted(a.filename for a in reply.attachments)
    assert names == ["Falcon Topco - board resolution (executed).pdf",
                     "Project Falcon - Disclosure Letter (executed).pdf",
                     "Project Falcon - SPA (executed).pdf",
                     "Project Falcon - closing index.pdf"]


def test_the_encore_mounts_northwinds_memory_and_nobody_elses(rehearsal):
    calls = [c for c in rehearsal["_calls"]["calls"] if c["kind"] == "review"]
    encore = next(c for c in calls if c["step"] == "8")
    assert encore["memory_stores"]["client"] == f"memstore_client_{cast.MATTER}"
    clients = {c["memory_stores"].get("client") for c in calls}
    assert clients == {f"memstore_client_{cast.MATTER}"}
