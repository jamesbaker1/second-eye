"""The closing's plan, and signed pages that look signed.

`PLAN` is who signs what at completion, as the closing agent would decide it
from the three documents: the rehearsal's scripted agent uses it, and
`build.py` uses it to make the signed pages the story returns, from the very
packets that plan produces. For a live run the agent decides for itself, so
sign the packets it actually sends with

    python -m demos.falcon.signing "Signature packet - Beta's director.pdf" \\
        --signer "Martin Hale" --out "Beta signed pages.pdf"

which draws a signature over the signature line of every page after the cover
sheet, keeping the reference stamp at the foot readable.
"""

from __future__ import annotations

import argparse
import math
import sys
from io import BytesIO
from pathlib import Path

from demos.falcon import cast

SPA = "Project Falcon - SPA.docx"
LETTER = "Project Falcon - Disclosure Letter.docx"
BOARD = "Falcon Topco - board resolution.docx"

PLAN = {
    "closing": cast.DEAL,
    "return": (f"Scan or photograph each signed page and email it to {cast.LAWYER_NAME} "
               f"({cast.LAWYER}). Do not date the pages."),
    "documents": [
        {"id": "spa", "file": SPA, "short": "SPA"},
        {"id": "dl", "file": LETTER, "short": "Disclosure Letter"},
        {"id": "board", "file": BOARD, "short": "Board resolution"},
    ],
    "signatories": [
        {"id": "beta-director", "name": "Beta's director", "person": cast.SELLER_DIRECTOR,
         "capacity": "Director",
         "sign": [{"document": "spa", "party": cast.SELLER.upper()},
                  {"document": "dl", "party": cast.SELLER.upper()}]},
        {"id": "topco-director", "name": "Falcon Topco's director",
         "person": cast.BUYER_DIRECTOR, "capacity": "Director",
         "sign": [{"document": "spa", "party": cast.BUYER.upper()},
                  {"document": "dl", "party": cast.BUYER.upper()},
                  {"document": "board", "party": cast.BUYER.upper()}]},
        {"id": "northwind-director", "name": "Northwind's director",
         "person": cast.CLIENT_DIRECTOR, "capacity": "Director",
         "sign": [{"document": "spa", "party": cast.CLIENT.upper()}]},
    ],
}

# Who returns which pages: Harrowgate send Beta's; the client sends its own.
BETA_PAGES = [("beta-director", 1), ("beta-director", 2)]
NORTHWIND_PAGES = [("topco-director", 1), ("topco-director", 2), ("topco-director", 3),
                   ("northwind-director", 1)]
SIGNERS = {"beta-director": cast.SELLER_DIRECTOR, "topco-director": cast.BUYER_DIRECTOR,
           "northwind-director": cast.CLIENT_DIRECTOR}


def packet_name(signatory_id: str) -> str:
    name = next(s["name"] for s in PLAN["signatories"] if s["id"] == signatory_id)
    return f"Signature packet - {name}.pdf"


def build_packets(files: dict[str, bytes], workdir: Path) -> dict[str, bytes]:
    """The packets the plan produces from the three documents, by file name,
    made with the closing skill's own code (pipeline/closing_docs.py).

    Always drawn from the words, never through LibreOffice even where it is
    installed (CI has it, a laptop may not): a word processor's layout, fonts
    and timestamps differ from machine to machine, and the build has to be
    the same bytes everywhere. The stamp a page carries, which is what makes
    a signed page this closing's, is the same either way."""
    from unittest.mock import patch

    from secondeye import convert, managed
    from secondeye.pipeline import closing_docs

    ws, out = workdir / "workspace", workdir / "outputs"
    ws.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (ws / managed.mount_name(name)).write_bytes(data)
    with patch.object(convert, "available", lambda: False):
        closing_docs.packets(PLAN, ws, out)
    return {p.name: p.read_bytes() for p in sorted(out.iterdir())}


def _signature_line_y(page) -> float | None:
    """Where on the page the signature goes: just above the first "By:" or
    "Director:" line, read from the text's own position."""
    found: list[float] = []

    def visit(text, cm, tm, font, size):
        if text.strip().startswith(("By:", "Director:")) and not found:
            found.append(cm[5] + tm[5])

    page.extract_text(visitor_text=visit)
    return found[0] if found else None


def _overlay(width: float, height: float, y: float, signer: str) -> bytes:
    """A wet-ink squiggle and the signer's name in a hand-like slant."""
    from reportlab.lib.colors import HexColor
    from reportlab.pdfgen import canvas

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(width, height), invariant=1)
    c.setStrokeColor(HexColor("#1b2a6b"))
    c.setFillColor(HexColor("#1b2a6b"))
    c.setLineWidth(1.4)
    x0 = 115.0
    path = c.beginPath()
    path.moveTo(x0, y + 6)
    for i in range(1, 80):
        t = i / 79
        x = x0 + 170 * t
        wobble = 9 * math.sin(t * math.pi * 7) * (1 - 0.6 * t) + 5 * math.sin(t * math.pi * 2)
        path.lineTo(x, y + 4 + wobble)
    c.drawPath(path, stroke=1, fill=0)
    c.setFont("Helvetica-Oblique", 13)
    c.drawString(x0 + 185, y + 2, signer)
    c.save()
    return buf.getvalue()


def sign_pages(pages: list[tuple[bytes, int, str]]) -> bytes:
    """One PDF of signed pages: each is (packet PDF, page number in it,
    counting the cover as 1, signer's name)."""
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for packet, number, signer in pages:
        page = PdfReader(BytesIO(packet)).pages[number - 1]
        width, height = float(page.mediabox.width), float(page.mediabox.height)
        y = _signature_line_y(page) or height * 0.55
        added = writer.add_page(page)
        added.merge_page(PdfReader(BytesIO(_overlay(width, height, y, signer))).pages[0])
    writer.add_metadata({"/Producer": "Project Falcon demo", "/Title": "Signed pages"})
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def signed_sets(packets: dict[str, bytes]) -> dict[str, bytes]:
    """The two returns in the story: Beta's pages through Harrowgate, and the
    client's own."""
    def pages(which):
        return [(packets[packet_name(sid)], n + 1, SIGNERS[sid]) for sid, n in which]
    return {"beta": sign_pages(pages(BETA_PAGES)),
            "northwind": sign_pages(pages(NORTHWIND_PAGES))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sign every page of a signature packet "
                                                 "after its cover sheet.")
    parser.add_argument("packet", type=Path)
    parser.add_argument("--signer", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    from pypdf import PdfReader

    data = args.packet.read_bytes()
    count = len(PdfReader(BytesIO(data)).pages)
    args.out.write_bytes(sign_pages([(data, n, args.signer) for n in range(2, count + 1)]))
    print(f"{args.out}: {count - 1} signed page(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
