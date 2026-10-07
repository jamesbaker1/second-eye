"""What the check layer can actually see.

The commonest state of a document in a live negotiation is "carries somebody's
tracked changes". Everything in checks.py reads Block.text, so anything the
extractor cannot see is not reviewed at all, and coverage is the metric the
product leads with.
"""

import zipfile
from io import BytesIO

from docx import Document

from lra.models import Attachment
from lra.pipeline import checks, extract

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def with_revision(paragraphs: list[str], markup: str) -> bytes:
    """A real .docx with one extra paragraph of hand-written markup at the end.

    python-docx cannot write w:ins or w:del, so the XML is injected into a file
    it did build. That keeps the rest of the package genuine.
    """
    document = Document()
    for text in paragraphs:
        document.add_paragraph(text)
    buffer = BytesIO()
    document.save(buffer)
    original = buffer.getvalue()

    source = zipfile.ZipFile(BytesIO(original))
    xml = source.read("word/document.xml").decode()
    head, closed, tail = xml.rpartition("</w:p>")
    xml = head + closed + f"<w:p>{markup}</w:p>" + tail

    out = BytesIO()
    with zipfile.ZipFile(out, "w") as rebuilt:
        for name in source.namelist():
            rebuilt.writestr(
                name, xml.encode() if name == "word/document.xml" else source.read(name)
            )
    return out.getvalue()


def attachment(content: bytes) -> Attachment:
    return Attachment(filename="Negotiated.docx", content_type="application/docx",
                      size_bytes=len(content), content=content)


def test_text_added_with_track_changes_on_is_reviewed():
    """A placeholder left inside somebody's insertion was invisible to every
    deterministic check, because python-docx's paragraph.text reads only the
    direct w:r children of w:p."""
    content = with_revision(
        ["The purchase price is "],
        f'<w:ins xmlns:w="{W}" w:id="900" w:author="Beta" w:date="2026-01-01T00:00:00Z">'
        '<w:r><w:t xml:space="preserve">[PRICE TO BE CONFIRMED]</w:t></w:r></w:ins>',
    )
    doc = extract.extract(attachment(content))
    assert "[PRICE TO BE CONFIRMED]" in " ".join(b.text for b in doc.blocks)

    found = checks.run_all(doc, content)
    assert any("PRICE TO BE CONFIRMED" in f.anchor for f in found), \
        "a placeholder inside a tracked insertion was not caught"


def test_text_somebody_deleted_is_not_treated_as_part_of_the_document():
    """Deleted text is not in the document. Reviewing it would produce findings
    about words that are already gone, which is a false positive."""
    content = with_revision(
        ["The term is thirty (30) days."],
        f'<w:del xmlns:w="{W}" w:id="901" w:author="Beta" w:date="2026-01-01T00:00:00Z">'
        '<w:r><w:delText xml:space="preserve">[OLD PLACEHOLDER]</w:delText></w:r></w:del>',
    )
    doc = extract.extract(attachment(content))
    assert "[OLD PLACEHOLDER]" not in " ".join(b.text for b in doc.blocks)


def test_a_tab_after_a_clause_number_survives_extraction():
    """Numbering and cross-reference checks read the start of a block, and
    clause numbering arrives as "1.<tab>Definitions"."""
    content = with_revision(
        ["Intro"],
        f'<w:r xmlns:w="{W}"><w:t>1.</w:t><w:tab/><w:t>Definitions</w:t></w:r>',
    )
    doc = extract.extract(attachment(content))
    assert any(b.text == "1.\tDefinitions" for b in doc.blocks)


def test_hyperlink_text_is_part_of_the_paragraph():
    content = with_revision(
        ["See "],
        f'<w:hyperlink xmlns:w="{W}" w:anchor="x"><w:r><w:t>Schedule 2</w:t></w:r>'
        "</w:hyperlink>",
    )
    doc = extract.extract(attachment(content))
    assert any("Schedule 2" in b.text for b in doc.blocks)
