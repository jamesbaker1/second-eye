"""A stand-in for LibreOffice's `soffice --headless --convert-to pdf`.

CI and the production container have LibreOffice; a laptop often does not,
and then the conversion path of the closing tools (page numbers, signature
pages found and substituted in the full document) would never run locally.
This is a real executable, put first on PATH by tests/test_closing.py, so the
skill's scripts find it exactly as they would find soffice, in a subprocess.

It lays a Word file out as a real multi-page PDF, deterministically: the
page header on every page, one paragraph after another, and a new page
before the "IN WITNESS" line, so a document's signature blocks sit alone on
its last page as they do in a real agreement. With
SECOND_EYE_FAKE_SOFFICE_LAYOUT=one everything shares one page, as a short document
does under the real LibreOffice. With SECOND_EYE_FAKE_SOFFICE_LAYOUT=fail it fails
the way an absent or broken LibreOffice does: no output file.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _paragraphs(document) -> list[str]:
    out = []
    for p in document.element.body.iter(f"{W}p"):
        out.append("".join(t.text or "" for t in p.iter(f"{W}t")))
    return out


def main() -> int:
    args = sys.argv[1:]
    layout = os.environ.get("SECOND_EYE_FAKE_SOFFICE_LAYOUT", "split")
    target = args[args.index("--convert-to") + 1]
    outdir = Path(args[args.index("--outdir") + 1])
    source = Path(args[-1])
    if layout == "fail":
        print("soffice: conversion failed", file=sys.stderr)
        return 1

    from docx import Document

    document = Document(str(source))
    paragraphs = _paragraphs(document)
    header = " ".join(p.text for s in document.sections for p in s.header.paragraphs
                      if not s.header.is_linked_to_previous and p.text.strip())
    produced = outdir / f"{source.stem}.{target}"
    if target != "pdf":
        produced.write_text("\n".join(paragraphs))
        return 0

    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate

    style = getSampleStyleSheet()["BodyText"]
    story, on_page = [], 0
    for text in paragraphs:
        if layout == "split" and text.strip().upper().startswith("IN WITNESS") and on_page:
            story.append(PageBreak())
            on_page = 0
        safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        story.append(Paragraph(safe or "&nbsp;", style))
        on_page += 1

    def head(canvas, doc):
        if header:
            canvas.setFont("Helvetica-Bold", 9)
            canvas.drawCentredString(A4[0] / 2, A4[1] - 30, header)

    SimpleDocTemplate(str(produced), pagesize=A4).build(story, onFirstPage=head,
                                                        onLaterPages=head)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
