"""Build the files a person has to open in real Microsoft Word.

Everything this product attaches passes a structural gate, and none of it has
ever been opened in Word. That is the oldest open item in ROADMAP.md, and it
cannot be closed by code: someone has to open each file and try the buttons.

    python -m evals.word_pack [output folder]

Writes one file per kind of output, and CHECKLIST.md saying what to try in each.
"""

from __future__ import annotations

import sys
from io import BytesIO
from pathlib import Path

from docx import Document
from docx.shared import Pt

from secondeye.models import Attachment, Finding, Mode, ReviewResult, Severity
from secondeye.pipeline import annotate, clean, compare, redline, reflow
from secondeye.pipeline.ooxml import Revision, RevisionWriter

CHECKLIST = """# Open each of these in Microsoft Word

For every file: does it open with no repair prompt? That is the first answer
needed, and a "Word found unreadable content" dialog on any of them is a bug
worth more than everything else on this page.

## 1 - review redline.docx
Tracked changes by "Legal Review Agent", plus one comment.
- Review pane lists 3 changes and 1 comment, all attributed and dated.
- Clause 1: "thirty" is bold. Its replacement "sixty" should be bold too.
- Accept the change in clause 2, reject the one in clause 1. Both behave.
- Clause 3A is a whole inserted paragraph. Reject it: the paragraph should
  vanish completely, with no empty line left behind.
- Reply to the comment. It should thread normally.

## 2 - comparison.docx
Tracked changes by "Comparison (Legal Review Agent)".
- Accept All. The text should now match `2b - later version.docx` exactly.
- Undo, then Reject All. It should match `2a - earlier version.docx` exactly.
- Clause 4 was deleted: after Accept All there should be no empty numbered
  paragraph where it was.
- The table cell and the header each carry one change.

## 3 - clean copy.docx
Made from file 2.
- No tracked changes, no comments, Review pane empty.
- File > Info: no author, no last-modified-by, no title, no company.
- File > Info > Check for Issues > Inspect Document: nothing found under
  comments/revisions, document properties, or hidden text.

## 4 - redline from a PDF.docx
The same review as file 1, but the document arrived as `4a - source.pdf` and
the Word copy was built from its text (pipeline/reflow.py).
- Opens with no repair prompt. Plain Calibri paragraphs, one per clause; the
  header from the PDF is ordinary body text, which is expected.
- Review pane lists 2 changes and 1 comment, as in file 1.
- Accept All then compare the words against `4a - source.pdf`: only the two
  changes should differ.

## 5 - annotated.pdf
Open in Acrobat Reader or Preview, not Word.
- Three sticky notes in the left margin, each beside the clause it names,
  authored "Legal Review Agent". Hover or click: title, then the explanation.
- One yellow highlight, on "thirty" in clause 1 (the only blocker). The
  highlight sits on the word, not a line above or below it.
- Nothing in the page text itself has changed.

## Then
Tell Claude what happened, file by file. Anything other than "fine" is a bug in
`src/secondeye/pipeline/ooxml.py`, `compare.py`, `clean.py`, `reflow.py` or
`annotate.py`, and the file that showed it becomes a regression test.
"""

EARLIER = [
    "1. Term. This Agreement continues for {thirty} days from the Effective Date.",
    "2. Payment. The Reciever shall pay the Price within ten days of invoice.",
    ("3. Liability. The Supplier's liability is capped at the fees paid in the prior "
    "twelve months."),
    "4. Notices. Notices shall be sent by post to the registered office.",
    "5. Governing law. This Agreement is governed by the laws of England and Wales.",
]


def _save(document) -> bytes:
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _base(later: bool = False) -> bytes:
    d = Document()
    d.sections[0].header.paragraphs[0].text = (
        "DRAFT 2 - Subject to contract" if later else "DRAFT 1 - Subject to contract")
    d.add_heading("Supply Agreement", 1)
    for text in EARLIER:
        if later and text.startswith("4."):
            continue
        if later and text.startswith("3."):
            text = "3. Liability. The Supplier's liability is not capped."
        if later and text.startswith("5."):
            text = text.replace("England and Wales", "New York")
        p = d.add_paragraph()
        before, _, rest = text.partition("{thirty}")
        if rest:
            p.add_run(before)
            word = p.add_run("sixty" if later else "thirty")
            word.bold = True
            word.font.size = Pt(12)
            p.add_run(rest)
        else:
            p.add_run(text)
        if later and text.startswith("2."):
            d.add_paragraph("2A. Interest. Late payments bear interest at 8% per annum.")
    table = d.add_table(rows=2, cols=2)
    table.style = "Table Grid"
    for (row, col), text in {(0, 0): "Milestone", (0, 1): "Fee",
                             (1, 0): "Delivery",
                             (1, 1): "USD 60,000" if later else "USD 50,000"}.items():
        table.cell(row, col).text = text
    return _save(d)


def _pdf_of(docx_bytes: bytes) -> bytes:
    """The earlier version printed to PDF, one line per paragraph, with
    reportlab (a dev dependency: this script is not shipped)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    text = [p.text for p in Document(BytesIO(docx_bytes)).paragraphs if p.text.strip()]
    buffer = BytesIO()
    page = canvas.Canvas(buffer, pagesize=A4)
    y = 800
    for line in text:
        page.drawString(60, y, line)
        y -= 20
    page.save()
    return buffer.getvalue()


def build(out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    def write(name: str, content: bytes | str) -> None:
        path = out / name
        if isinstance(content, str):
            path.write_text(content)
        else:
            path.write_bytes(content)
        written.append(path)

    earlier, later = _base(), _base(later=True)

    d = Document(BytesIO(earlier))
    writer = RevisionWriter(d, "Legal Review Agent")
    writer.apply(Revision("thirty", "sixty", "Legal Review Agent"))
    writer.apply(Revision("Reciever", "Receiver", "Legal Review Agent"))
    writer.insert_paragraph_after(
        "3. Liability", "3A. Insurance. The Supplier shall maintain adequate insurance.")
    writer.comment("registered office", "Whose registered office? The clause does not say.")
    write("1 - review redline.docx", _save(d))

    compared = compare.compare(earlier, later)
    if not (compared.content and compared.proven):
        raise SystemExit("the comparison did not prove itself; fix that before Word")
    write("2a - earlier version.docx", earlier)
    write("2b - later version.docx", later)
    write("2 - comparison.docx", compared.content)

    write("3 - clean copy.docx", clean.clean(compared.content).content)

    # The same document as a PDF, reviewed the way a PDF now is: a Word copy
    # built from its text carries the tracked changes, and the PDF itself
    # carries a note per finding with the blocker highlighted.
    pdf = _pdf_of(earlier)
    source = Attachment(filename="4a - source.pdf", content_type="application/pdf",
                        size_bytes=len(pdf), content=pdf)
    findings = [
        Finding(severity=Severity.BLOCKER, category="amount", title="thirty should be sixty",
                explanation="The term sheet says sixty days.", anchor="thirty",
                suggested_text="sixty", auto_apply=True),
        Finding(severity=Severity.FORMATTING, category="defined-term",
                title="Reciever is misspelt", explanation="", anchor="Reciever",
                suggested_text="Receiver", auto_apply=True),
        Finding(severity=Severity.SUBSTANTIVE, category="notices",
                title="Whose registered office?", explanation="The clause does not say.",
                anchor="registered office", question="Whose registered office?"),
    ]
    rebuilt = reflow.to_docx(source)
    from_pdf = redline.apply(rebuilt.content, ReviewResult(mode=Mode.REDLINE, summary="",
                                                           findings=findings))
    if len(from_pdf.applied) != 2 or not from_pdf.commented:
        raise SystemExit("the PDF-built redline did not take every change; fix that before Word")
    write("4a - source.pdf", pdf)
    write("4 - redline from a PDF.docx", from_pdf.content)
    marked = annotate.annotate(pdf, findings)
    if marked.content is None or marked.placed != 3 or marked.highlighted != 1:
        raise SystemExit(f"the PDF annotation did not land as expected: {marked.notes}")
    write("5 - annotated.pdf", marked.content)
    write("CHECKLIST.md", CHECKLIST)
    return written


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("work/word-pack")
    for path in build(target):
        print(path)
