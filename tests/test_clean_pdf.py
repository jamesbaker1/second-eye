"""The PDF clean copy: what comes out, what stays, and when it refuses.

Every PDF here is built with reportlab and pypdf the way a real one arrives:
an export with the drafter's name in it, a partner's sticky notes, a
spreadsheet attached, a script that runs on open.
"""

from __future__ import annotations

import warnings
from io import BytesIO

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.annotations import FreeText, Highlight, Link, Text
from pypdf.generic import (
    ArrayObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    NumberObject,
    StreamObject,
    TextStringObject,
)
from reportlab.pdfgen import canvas

from secondeye.pipeline import clean_pdf
from secondeye.pipeline.clean import CannotClean

PAGES = [
    ["1. Term. This Agreement continues for twelve (12) months.",
     "2. Liability. The Supplier's liability is capped at the fees paid."],
    ["3. Governing law. New York law governs this Agreement."],
]


def base(pages=PAGES) -> PdfWriter:
    buf = BytesIO()
    c = canvas.Canvas(buf)
    for lines in pages:
        for i, line in enumerate(lines):
            c.drawString(72, 720 - 18 * i, line)
        c.showPage()
    c.save()
    return PdfWriter(clone_from=PdfReader(BytesIO(buf.getvalue())))


def save(writer: PdfWriter) -> bytes:
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def comment(writer, page, author, text="Check with client"):
    annot = Text(rect=(50, 550, 200, 650), text=text)
    annot[NameObject("/T")] = TextStringObject(author)
    writer.add_annotation(page_number=page, annotation=annot)


def dirty() -> bytes:
    w = base()
    w.metadata = None
    w.add_metadata({"/Author": "Jane Associate", "/Producer": "Acme DMS 9.2",
                    "/Creator": "Microsoft Word", "/Title": "Supply Agreement",
                    "/MatterNumber": "2026-0413"})
    comment(w, 0, "Jane Associate")
    comment(w, 0, "Jane Associate", "Too low?")
    comment(w, 1, "Jane Associate", "NY or DE?")
    hl = Highlight(rect=(70, 710, 400, 735), quad_points=ArrayObject(
        [FloatObject(v) for v in (70, 735, 400, 735, 70, 710, 400, 710)]))
    hl[NameObject("/T")] = TextStringObject("Bob Partner")
    w.add_annotation(page_number=0, annotation=hl)
    w.add_annotation(page_number=1, annotation=FreeText(
        text="Draft - not for circulation", rect=(50, 400, 300, 450)))
    w.add_annotation(page_number=0, annotation=Link(
        rect=(70, 700, 200, 715), url="https://example.com/terms"))
    w.add_attachment("draft.xlsx", b"PK fees workings")
    with warnings.catch_warnings():             # a document-level script, the
        warnings.simplefilter("ignore")         # kind add_open_action cannot make
        w.add_js("app.alert('hello');")
    meta = StreamObject()
    meta.set_data(b"<x:xmpmeta><dc:creator>Jane Associate</dc:creator></x:xmpmeta>")
    meta.update({NameObject("/Type"): NameObject("/Metadata"),
                 NameObject("/Subtype"): NameObject("/XML")})
    w.root_object[NameObject("/Metadata")] = w._add_object(meta)
    w.pages[0][NameObject("/PieceInfo")] = DictionaryObject(
        {NameObject("/Illustrator"): DictionaryObject()})
    return save(w)


def text(content: bytes) -> list[str]:
    return [" ".join(p.extract_text().split()) for p in PdfReader(BytesIO(content)).pages]


def test_everything_that_travels_comes_out_and_is_listed():
    """The free-text box has no author, and counts with the comments, as it
    does in Acrobat's own list."""
    original = dirty()
    result = clean_pdf.clean(original)
    assert result.removed == [
        "4 comments from Jane Associate",
        "1 highlight from Bob Partner",
        "An embedded file: draft.xlsx",
        "Author and producer names",
        "Document-system properties (matter and document numbers)",
        "XMP metadata (the same details, stored a second way)",
        "A script",
        "Private data left by the editing software",
    ]
    out = PdfReader(BytesIO(result.content))
    assert text(result.content) == text(original)
    assert dict(out.metadata or {}) == {"/Title": "Supply Agreement"}
    assert "/Metadata" not in out.trailer["/Root"]
    assert "/Names" not in out.trailer["/Root"]
    assert "/PieceInfo" not in out.pages[0]
    assert out.attachments == {}
    for needle in (b"Jane Associate", b"Bob Partner", b"Check with client",
                   b"app.alert", b"fees workings", b"Acme DMS", b"2026-0413"):
        assert needle not in result.content


def test_links_and_form_fields_stay():
    result = clean_pdf.clean(dirty())
    annots = [a.get_object() for a in PdfReader(BytesIO(result.content)).pages[0]["/Annots"]]
    assert [a["/Subtype"] for a in annots] == ["/Link"]


def test_a_form_field_keeps_its_value():
    w = base()
    field = DictionaryObject({
        NameObject("/Type"): NameObject("/Annot"), NameObject("/Subtype"): NameObject("/Widget"),
        NameObject("/FT"): NameObject("/Tx"), NameObject("/T"): TextStringObject("buyer"),
        NameObject("/V"): TextStringObject("Northwind Ltd"),
        NameObject("/Rect"): ArrayObject([FloatObject(v) for v in (72, 600, 300, 620)]),
    })
    ref = w._add_object(field)
    w.pages[0][NameObject("/Annots")] = ArrayObject([ref])
    w.root_object[NameObject("/AcroForm")] = DictionaryObject(
        {NameObject("/Fields"): ArrayObject([ref])})
    comment(w, 0, "Jane Associate")
    result = clean_pdf.clean(save(w))
    assert result.removed[0] == "1 comment from Jane Associate"
    fields = PdfReader(BytesIO(result.content)).get_fields()
    assert fields["buyer"]["/V"] == "Northwind Ltd"


def test_a_clean_pdf_is_already_clean():
    w = base()
    w.metadata = None
    w.add_metadata({"/Title": "Supply Agreement"})
    w._info.get_object().pop(NameObject("/Producer"), None)
    content = save(w)
    result = clean_pdf.clean(content)
    assert result.removed == [] and not result.changed
    assert result.content == content


def test_a_title_that_is_a_file_path_goes():
    w = base()
    w.metadata = None
    w.add_metadata({"/Title": r"C:\Users\jb\Matters\Supply v3 JB comments.docx"})
    w._info.get_object().pop(NameObject("/Producer"), None)
    result = clean_pdf.clean(save(w))
    assert result.removed == ["A title that gave away a file name"]
    assert PdfReader(BytesIO(result.content)).metadata in (None, {})


def test_a_script_that_runs_on_open_goes():
    w = base()
    w.metadata = None
    action = DictionaryObject({NameObject("/S"): NameObject("/JavaScript"),
                               NameObject("/JS"): TextStringObject("this.print();")})
    w.root_object[NameObject("/OpenAction")] = w._add_object(action)
    result = clean_pdf.clean(save(w))
    assert "A script" in result.removed
    root = PdfReader(BytesIO(result.content)).trailer["/Root"]
    assert "/OpenAction" not in root
    assert b"this.print" not in result.content


def test_a_file_pinned_to_a_page_is_named():
    w = base()
    w.metadata = None
    stream = StreamObject()
    stream.set_data(b"secret workings")
    stream[NameObject("/Type")] = NameObject("/EmbeddedFile")
    spec = DictionaryObject({
        NameObject("/Type"): NameObject("/Filespec"),
        NameObject("/F"): TextStringObject("fees.xlsx"),
        NameObject("/EF"): DictionaryObject({NameObject("/F"): w._add_object(stream)}),
    })
    annot = DictionaryObject({
        NameObject("/Type"): NameObject("/Annot"),
        NameObject("/Subtype"): NameObject("/FileAttachment"),
        NameObject("/Rect"): ArrayObject([FloatObject(v) for v in (10, 10, 30, 30)]),
        NameObject("/FS"): w._add_object(spec),
    })
    w.pages[0][NameObject("/Annots")] = ArrayObject([w._add_object(annot)])
    result = clean_pdf.clean(save(w))
    assert "1 attached file" in result.removed
    assert "An embedded file: fees.xlsx" in result.removed
    assert b"secret workings" not in result.content


def test_a_password_protected_pdf_is_refused_in_a_sentence():
    w = base()
    w.encrypt("secret")
    with pytest.raises(CannotClean, match="password-protected; send it unlocked"):
        clean_pdf.clean(save(w))


def test_a_signed_pdf_is_left_alone():
    w = base()
    w.root_object[NameObject("/AcroForm")] = DictionaryObject(
        {NameObject("/Fields"): ArrayObject(), NameObject("/SigFlags"): NumberObject(3)})
    with pytest.raises(CannotClean, match="digitally signed"):
        clean_pdf.clean(save(w))


def test_unapplied_redactions_are_refused():
    w = base()
    w.add_annotation(page_number=0, annotation=DictionaryObject({
        NameObject("/Type"): NameObject("/Annot"), NameObject("/Subtype"): NameObject("/Redact"),
        NameObject("/Rect"): ArrayObject([FloatObject(v) for v in (70, 710, 400, 735)]),
    }))
    with pytest.raises(CannotClean, match="redactions marked but not applied"):
        clean_pdf.clean(save(w))


def test_something_that_is_not_a_pdf_is_refused():
    with pytest.raises(CannotClean, match="could not open that PDF"):
        clean_pdf.clean(b"%PDF-1.7\n")


def test_a_copy_whose_words_moved_is_not_sent(monkeypatch):
    real = clean_pdf._page_text
    calls = []

    def drifting(reader):
        calls.append(1)
        pages = real(reader)
        return pages if len(calls) == 1 else [p + " altered" for p in pages]

    monkeypatch.setattr(clean_pdf, "_page_text", drifting)
    with pytest.raises(CannotClean, match="would have changed its words"):
        clean_pdf.clean(dirty())


def test_a_copy_still_carrying_a_comment_is_not_sent(monkeypatch):
    monkeypatch.setattr(clean_pdf, "_strip_annotations", lambda writer: set())
    with pytest.raises(CannotClean, match="only looks clean"):
        clean_pdf.clean(dirty())
