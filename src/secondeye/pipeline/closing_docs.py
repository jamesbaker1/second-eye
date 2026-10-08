"""The paperwork of a closing: packets out, signed pages in, the executed set.

These are the tools the closing agent runs in the sandbox (skills/lra-closing),
not decisions. The agent decides who signs what, whether a scan is signed and
whose it is; this module does the parts that must be exact.

- `survey` reads one document for signing with `sigpack.survey`: its
  execution copy, its signature blocks, what is in the way, and a version
  reference from its words.
- `packets` builds, from the agent's plan, the execution copies and one packet
  per signatory: a cover sheet saying what to sign and how to send it back,
  then each page, headed "<Document> - signature page" and stamped at the
  foot with the version reference and page id, so a scan says what it is.
- `receive` turns one page of a returned scan or a photo into a one-page PDF
  and reads any text and stamp on it, as evidence for the agent.
- `compile_set` substitutes the signed pages into each execution copy and
  renders the closing index (the closing bible).
- `checklist_pdf` renders the checklist.

PDFs are made with LibreOffice where it is installed (the pages then carry the
firm's styles) and otherwise drawn from the words with reportlab, which says
so on the page. reportlab, pypdf and Pillow are imported only when used.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

from secondeye.pipeline import closing_state, sigpack

log = logging.getLogger(__name__)

PDF = "application/pdf"
STAMP = re.compile(r"LRA\s*ref\s*([0-9A-F]{6})\s*[·|/-]?\s*page\s*([\w.-]+--[\w.-]+)",
                   re.IGNORECASE)


class ClosingError(Exception):
    """A sentence for the agent, and through it for the lawyer."""


# --------------------------------------------------------------------------
# Reading documents
# --------------------------------------------------------------------------


def survey(content: bytes, filename: str) -> dict:
    """One document, read for signing, as JSON for the agent."""
    try:
        found = sigpack.survey(content, filename)
    except sigpack.Unsignable as e:
        raise ClosingError(f"{filename}: {e}") from e
    text = "\n".join(b.text for b in found.doc.blocks)
    return {
        "file": filename,
        "title": found.title,
        "date": found.date,
        "ref": closing_state.text_ref(text),
        "execution_file": sigpack._execution_name(filename),
        "blockers": found.blockers,
        "removed": found.removed,
        "parties": [{"name": p.name, "short": p.short, "lines": found.lines.get(p.name, [])}
                    for p in found.parties],
    }


def _party(found: sigpack.Survey, wanted: str) -> sigpack.Party | None:
    key = _norm(wanted)
    for p in found.parties:
        if key in (_norm(p.name), _norm(p.short)):
            return p
    return None


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


# --------------------------------------------------------------------------
# PDF helpers
# --------------------------------------------------------------------------


def _libreoffice() -> bool:
    from secondeye import convert

    return convert.available()


def to_pdf(docx: bytes) -> bytes | None:
    """The Word file as a PDF through LibreOffice, or None without it."""
    from secondeye import convert

    if not convert.available():
        return None
    try:
        return convert._convert(docx, "document.docx", "pdf")
    except convert.ConversionUnavailable as e:
        log.info("no PDF from LibreOffice: %s", e)
        return None


def _styles():
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet

    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("t", parent=base["Title"], fontSize=16, spaceAfter=10),
        "h": ParagraphStyle("h", parent=base["Heading2"], fontSize=12, spaceBefore=10),
        "body": ParagraphStyle("b", parent=base["BodyText"], fontSize=10, leading=13),
        "small": ParagraphStyle("s", parent=base["BodyText"], fontSize=8, leading=10),
        "cell": ParagraphStyle("c", parent=base["BodyText"], fontSize=8.5, leading=10.5),
    }


def _esc(text: str) -> str:
    return (str(text or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _render(flowables_of, footer: str = "") -> bytes:
    """A PDF from reportlab flowables, with an optional footer on every page."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate

    buffer = BytesIO()
    styles = _styles()

    def foot(canvas, doc):
        if footer:
            canvas.saveState()
            canvas.setFont("Helvetica", 7)
            canvas.drawString(18 * mm, 10 * mm, footer)
            canvas.restoreState()

    document = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm,
                                 topMargin=20 * mm, bottomMargin=20 * mm)
    document.build(flowables_of(styles), onFirstPage=foot, onLaterPages=foot)
    return buffer.getvalue()


def _table(rows: list[list[str]], widths: list[float], styles) -> object:
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Table, TableStyle

    data = [[Paragraph(_esc(c) if i else f"<b>{_esc(c)}</b>", styles["cell"]) for c in row]
            for i, row in enumerate(rows)]
    table = Table(data, colWidths=[w * mm for w in widths], repeatRows=1)
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return table


def _stamp(pdf: bytes, text: str, label: str = "") -> bytes:
    """Write the stamp at the foot of every page, and the label at the head
    of any page that does not already carry it."""
    from pypdf import PdfReader, PdfWriter
    from reportlab.pdfgen import canvas

    reader = PdfReader(BytesIO(pdf))
    writer = PdfWriter()
    for page in reader.pages:
        width, height = float(page.mediabox.width), float(page.mediabox.height)
        overlay = BytesIO()
        c = canvas.Canvas(overlay, pagesize=(width, height))
        c.setFont("Helvetica", 7)
        c.drawString(36, 16, text)
        existing = page.extract_text() or ""
        if label and _norm(label) not in _norm(existing):
            c.setFont("Helvetica-Bold", 9)
            c.drawCentredString(width / 2, height - 24, label)
        c.save()
        writer.add_page(page).merge_page(PdfReader(BytesIO(overlay.getvalue())).pages[0])
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def _page_texts(pdf: bytes) -> list[str]:
    from pypdf import PdfReader

    return [(p.extract_text() or "") for p in PdfReader(BytesIO(pdf)).pages]


def _merge(parts: list[bytes]) -> bytes:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for part in parts:
        for page in PdfReader(BytesIO(part)).pages:
            writer.add_page(page)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def page_count(pdf: bytes) -> int:
    from pypdf import PdfReader

    return len(PdfReader(BytesIO(pdf)).pages)


def _docx_paragraphs(docx: bytes) -> list[str]:
    from docx import Document

    d = Document(BytesIO(docx))
    out: list[str] = []
    for block in d.element.body.iter():
        tag = block.tag.rsplit("}", 1)[-1] if isinstance(block.tag, str) else ""
        if tag == "p":
            text = "".join(t.text or "" for t in block.iter(
                "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"))
            out.append(text)
    return out


def _text_page(label: str, paragraphs: list[str], note: str) -> bytes:
    """A signature page drawn from its words, when LibreOffice is absent."""
    def flow(styles):
        from reportlab.platypus import Paragraph, Spacer

        items = [Paragraph(f"<b>{_esc(label)}</b>", styles["h"]), Spacer(1, 12)]
        for text in paragraphs:
            items.append(Paragraph(_esc(text) or "&nbsp;", styles["body"]))
            items.append(Spacer(1, 4))
        items += [Spacer(1, 18), Paragraph(_esc(note), styles["small"])]
        return items
    return _render(flow)


def locate(pdf: bytes, lines: list[str]) -> int | None:
    """The 1-based page of a PDF holding a signature block, by its words: the
    last page carrying every distinctive line of it."""
    wanted = [_norm(re.sub(r"^[^:]{1,20}:", "", line)) for line in lines]
    wanted = [w for w in wanted if len(w) >= 3]
    if not wanted:
        return None
    found = None
    for n, text in enumerate(_page_texts(pdf), start=1):
        page = _norm(text)
        if all(w in page for w in wanted):
            found = n
    return found


# --------------------------------------------------------------------------
# Packets
# --------------------------------------------------------------------------


def stamp_text(ref: str, pid: str) -> str:
    return f"LRA ref {ref} · page {pid} · do not alter this page"


def packets(plan: dict, workspace: Path, out: Path) -> dict:
    """Execution copies, signature pages and a packet per signatory.

    `plan` is the agent's: the documents (by file in `workspace`, with an id
    and a short name), the signatories (an id, the name the lawyer will read,
    and the document parties they sign for), the closing's name and how pages
    come back. All or nothing: a document with a blocker, or a signatory
    naming a party the document does not have, stops everything and says so.
    """
    closing = str(plan.get("closing") or "the closing").strip()
    ret = str(plan.get("return") or "").strip()
    documents = plan.get("documents") or []
    signatories = plan.get("signatories") or []
    if not documents or not signatories:
        raise ClosingError("the plan needs documents and signatories")

    surveys: dict[str, tuple[dict, sigpack.Survey, dict]] = {}
    problems: list[str] = []
    for d in documents:
        path = workspace / str(d.get("file", ""))
        if not d.get("id") or not path.is_file():
            problems.append(f"{d.get('file')!r}: not found in {workspace} (or no id)")
            continue
        content = path.read_bytes()
        try:
            found = sigpack.survey(content, path.name)
        except sigpack.Unsignable as e:
            problems.append(f"{path.name}: {e}")
            continue
        info = survey(content, path.name)
        for b in found.blockers:
            problems.append(f"{d.get('short') or info['title']}: {b}")
        surveys[d["id"]] = (d, found, info)
    for s in signatories:
        for item in s.get("sign") or []:
            entry = surveys.get(item.get("document"))
            if entry and _party(entry[1], item.get("party", "")) is None:
                names = ", ".join(p.name for p in entry[1].parties) or "none"
                problems.append(f"{s.get('name')}: {entry[2]['title']} has no party "
                                f"{item.get('party')!r} (its parties: {names})")
    if problems:
        raise ClosingError("Not ready for signature: " + "; ".join(problems))

    out.mkdir(parents=True, exist_ok=True)
    converting = _libreoffice()
    docs_out: list[dict] = []
    exec_pdfs: dict[str, bytes | None] = {}
    for doc_id, (d, found, info) in surveys.items():
        (out / info["execution_file"]).write_bytes(found.execution)
        # The whole execution copy, so a page number is the signature page's
        # place in the document. None when it cannot be converted: then no
        # number is given at all rather than a guessed one.
        exec_pdf = to_pdf(found.execution) if converting else None
        exec_pdfs[doc_id] = exec_pdf
        docs_out.append({
            "id": doc_id, "title": info["title"], "short": d.get("short") or info["title"],
            "file": info["file"], "execution_file": info["execution_file"],
            "ref": info["ref"], "date": info["date"],
            "parties": [p.name for p in found.parties],
            "pages_total": page_count(exec_pdf) if exec_pdf else None,
        })

    pages_out: list[dict] = []
    packets_out: list[dict] = []
    for s in signatories:
        sid, name = str(s.get("id") or ""), str(s.get("name") or "")
        if not sid or not name:
            raise ClosingError("every signatory needs an id and a name")
        rows = [["Document", "Page", "Signs as", "Page id"]]
        pdf_pages: list[bytes] = []
        for item in s.get("sign") or []:
            d, found, info = surveys[item["document"]]
            party = _party(found, item["party"])
            pid = closing_state.page_id(item["document"], sid)
            label = f"{info['title']} — signature page"
            # All the parties, so a block set beside another party's in one
            # table is cut down to this party's cells.
            built = [b for b in sigpack.page_documents(found.execution, found.doc,
                                                       found.parties, label)
                     if b[0] is party]
            if not built:
                raise ClosingError(f"{info['title']}: the signature page for {party.name} "
                                   "could not be lifted out safely")
            page_docx = built[0][1]
            pdf = to_pdf(page_docx) if converting else None
            if pdf is None:
                pdf = _text_page(label, [t for t in _docx_paragraphs(page_docx) if t.strip()],
                                 "Drawn from the execution copy's words; sign here as marked.")
            pdf = _stamp(pdf, stamp_text(info["ref"], pid), label)
            number = locate(exec_pdfs[item["document"]], found.lines.get(party.name, [])) \
                if exec_pdfs[item["document"]] else None
            where = f"p.{number}" if number else "signature page"
            rows.append([info["title"], where, party.name, pid])
            pdf_pages.append(pdf)
            pages_out.append({"id": pid, "document": item["document"], "signatory": sid,
                              "party": party.name, "page": number, "ref": info["ref"],
                              "lines": found.lines.get(party.name, []),
                              "status": "awaiting", "received": None})
        cover = _cover(closing, name, rows, ret)
        filename = f"Signature packet - {_safe(name)}.pdf"
        (out / filename).write_bytes(_merge([cover] + pdf_pages))
        packets_out.append({"signatory": sid, "file": filename, "pages": len(pdf_pages)})

    method = "libreoffice" if all(exec_pdfs.values()) else "text"
    return {"method": method, "documents": docs_out, "pages": pages_out,
            "packets": packets_out}


def _cover(closing: str, name: str, rows: list[list[str]], ret: str) -> bytes:
    def flow(styles):
        from reportlab.platypus import Paragraph, Spacer

        n = len(rows) - 1
        return [
            Paragraph(_esc(f"{closing}: signature packet"), styles["title"]),
            Paragraph(_esc(f"For {name}. {n} page{'s' if n != 1 else ''} to sign."),
                      styles["body"]),
            Spacer(1, 10),
            _table(rows, [62, 22, 50, 36], styles),
            Spacer(1, 12),
            Paragraph("<b>How to sign</b>", styles["h"]),
            Paragraph("Sign each page where marked. Do not change, date or re-type any "
                      "page unless it asks you to. Keep the reference at the foot of each "
                      "page visible when you scan or photograph it.", styles["body"]),
            Paragraph("<b>How to return</b>", styles["h"]),
            Paragraph(_esc(ret or "Scan or photograph each signed page and email it back to "
                                  "the lawyer who sent you this packet."), styles["body"]),
        ]
    return _render(flow)


def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "", name).strip()[:80] or "signatory"


# --------------------------------------------------------------------------
# Signed pages coming back
# --------------------------------------------------------------------------


def _is_image(data: bytes) -> bool:
    return data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n" \
        or data[:4] in (b"II*\x00", b"MM\x00*") or data[8:12] in (b"WEBP",) \
        or data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1")


def receive(data: bytes, filename: str, page: int = 1) -> tuple[bytes, dict]:
    """One page of what came back, as a one-page PDF, and what can be read
    off it: its text layer and our stamp if either is there. A photo has
    neither; the agent reads it by eye."""
    from pypdf import PdfReader, PdfWriter

    if data[:5] == b"%PDF-":
        reader = PdfReader(BytesIO(data))
        total = len(reader.pages)
        if not 1 <= page <= total:
            raise ClosingError(f"{filename} has {total} page(s); there is no page {page}")
        writer = PdfWriter()
        writer.add_page(reader.pages[page - 1])
        buffer = BytesIO()
        writer.write(buffer)
        text = reader.pages[page - 1].extract_text() or ""
        out = buffer.getvalue()
    elif _is_image(data):
        if data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1"):
            raise ClosingError(f"{filename} is a HEIC photo; ask for it as a JPEG or PDF")
        from PIL import Image, ImageOps

        total = 1
        if page != 1:
            raise ClosingError(f"{filename} is one image; there is no page {page}")
        image = ImageOps.exif_transpose(Image.open(BytesIO(data))).convert("RGB")
        buffer = BytesIO()
        image.save(buffer, format="PDF", resolution=150.0)
        out, text = buffer.getvalue(), ""
    else:
        raise ClosingError(f"{filename} is not a PDF or an image")
    m = STAMP.search(re.sub(r"\s+", " ", text))
    return out, {"pages_in_file": total, "text": text.strip()[:4000],
                 "stamp": {"ref": m.group(1).upper(), "page": m.group(2)} if m else None}


# --------------------------------------------------------------------------
# The executed set and the closing index
# --------------------------------------------------------------------------


def _find(name: str, dirs: list[Path]) -> bytes:
    for d in dirs:
        path = d / name
        if path.is_file():
            return path.read_bytes()
    raise ClosingError(f"{name} is not in {', '.join(str(d) for d in dirs)}")


def _executed_name(document: dict) -> str:
    return f"{sigpack._stem(document['file'])} (executed).pdf"


def compile_set(state: dict, dirs: list[Path], out: Path) -> dict:
    """Each document with its signed pages in, and the closing index.

    Refuses while a page is outstanding. With LibreOffice, the execution
    copy's own signature pages are found by their words and replaced by the
    signed ones, in place; without it the document is drawn from its words
    with the signed pages after the text, and the index says so.
    """
    state = closing_state.normalise(state)
    waiting = [p["id"] for p in state["pages"] if p.get("status") != "received"]
    if waiting:
        raise ClosingError("Not all pages are in: " + ", ".join(waiting))
    out.mkdir(parents=True, exist_ok=True)
    converting = _libreoffice()
    method = "libreoffice"
    files: list[str] = []
    for doc in state["documents"]:
        execution = _find(doc.get("execution_file") or sigpack._execution_name(doc["file"]),
                          dirs)
        pages = [p for p in state["pages"] if p["document"] == doc["id"]]
        signed = [_find(p["received"]["file"], dirs) for p in pages]
        pdf = to_pdf(execution) if converting else None
        if pdf is not None:
            executed = _substitute(pdf, execution, pages, signed)
        else:
            method = "text"
            body = [t for t in _docx_paragraphs(execution)]
            skip = {_norm(line) for p in pages for line in (p.get("lines") or [])}
            body = [t for t in body if _norm(t) not in skip]
            text_pdf = _text_page(doc["title"], [t for t in body if t.strip()],
                                  "Conformed copy drawn from the execution copy's words; "
                                  "the signed pages follow.")
            executed = _merge([text_pdf] + signed)
        name = _executed_name(doc)
        (out / name).write_bytes(executed)
        files.append(name)
    index_name = f"{_safe(state.get('name') or 'Closing')} - closing index.pdf"
    (out / index_name).write_bytes(closing_index(state, files, method))
    return {"method": method, "files": files, "index": index_name}


def _body_fragments(execution: bytes, pages: list[dict]) -> list[str]:
    """Distinctive text from the document's body: every paragraph before the
    first signature block, except the "IN WITNESS" line that belongs with the
    blocks. A page carrying any of it is not only a signature page."""
    block_lines = {_norm(line) for p in pages for line in (p.get("lines") or [])}
    paragraphs = [_norm(t) for t in _docx_paragraphs(execution)]
    first = next((i for i, t in enumerate(paragraphs) if t and t in block_lines),
                 len(paragraphs))
    body = paragraphs[:first]
    if body and re.search(r"\b(?:in witness|executed|signed)\b", body[-1]):
        body = body[:-1]
    return [t[:40] for t in body if len(t) >= 12]


def _substitute(pdf: bytes, execution: bytes, pages: list[dict],
                signed: list[bytes]) -> bytes:
    """The execution copy with the signed pages in. A page holding nothing but
    signature blocks is replaced by the signed ones; a page that also carries
    the agreement's words is kept, and the signed pages follow it, so no word
    of the document is ever lost to a substitution."""
    from pypdf import PdfReader

    texts = _page_texts(pdf)
    located = {locate(pdf, p.get("lines") or []) for p in pages} - {None}
    body = _body_fragments(execution, pages)
    replace = {n for n in located
               if not any(fragment in _norm(texts[n - 1]) for fragment in body)}
    after = max(located) if located else None
    original = PdfReader(BytesIO(pdf)).pages
    parts: list[bytes] = []
    inserted = False
    for n in range(1, len(texts) + 1):
        if n in replace:
            if not inserted:
                parts += signed
                inserted = True
            continue
        parts.append(_single(original[n - 1]))
        if n == after and not inserted:
            parts += signed
            inserted = True
    if not inserted:
        parts += signed
    return _merge(parts)


def _single(page) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_page(page)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def closing_index(state: dict, files: list[str], method: str = "libreoffice") -> bytes:
    """The closing bible's first page: every document, its parties, its date
    and who signed it, then every signature with where it came from."""
    docs = state.get("documents", [])
    sigs = {s["id"]: s for s in state.get("signatories", [])}
    today = datetime.now(UTC).date().isoformat()

    def flow(styles):
        from reportlab.platypus import Paragraph, Spacer

        rows = [["#", "Document", "Parties", "Date", "Signed by", "Executed copy"]]
        for n, doc in enumerate(docs, start=1):
            pages = [p for p in state.get("pages", []) if p.get("document") == doc["id"]]
            signed_by = "; ".join(sigs.get(p["signatory"], {}).get("name", p["signatory"])
                                  for p in pages)
            rows.append([str(n), doc["title"], ", ".join(doc.get("parties") or []),
                         doc.get("date") or "undated", signed_by,
                         files[n - 1] if n - 1 < len(files) else ""])
        sig_rows = [["Page", "Document", "Party", "Signatory", "Received", "From"]]
        titles = {d["id"]: d.get("short") or d["title"] for d in docs}
        for p in state.get("pages", []):
            r = p.get("received") or {}
            sig_rows.append([p["id"], titles.get(p["document"], p["document"]),
                             p.get("party") or sigs.get(p["signatory"], {}).get("party", ""),
                             sigs.get(p["signatory"], {}).get("name", p["signatory"]),
                             (r.get("at") or "")[:10], r.get("from") or ""])
        items = [
            Paragraph(_esc(f"{state.get('name') or 'Closing'}: closing index"),
                      styles["title"]),
            Paragraph(_esc(f"Compiled {today}. {len(docs)} document"
                           f"{'s' if len(docs) != 1 else ''}, "
                           f"{len(state.get('pages', []))} signature pages."), styles["body"]),
            Spacer(1, 8),
            _table(rows, [7, 38, 40, 20, 35, 30], styles),
            Spacer(1, 10),
            Paragraph("Signatures", styles["h"]),
            _table(sig_rows, [34, 26, 32, 30, 20, 28], styles),
        ]
        if method != "libreoffice":
            items += [Spacer(1, 10), Paragraph(
                "The executed copies were drawn from the execution copies' words because "
                "no word processor was available; the signed pages follow each document's "
                "text.", styles["small"])]
        return items
    return _render(flow)


def checklist_pdf(state: dict) -> bytes:
    state = closing_state.normalise(state)

    def flow(styles):
        from reportlab.platypus import Paragraph, Spacer

        rows = [["", "Item", "Source", "Who", "Status"]]
        order = {k: i for i, k in enumerate(closing_state.CHECKLIST_KINDS)}
        for item in sorted(state["checklist"], key=lambda i: order.get(i.get("kind"), 9)):
            mark = "x" if item.get("status") in ("done", "waived",
                                                      "not applicable") else ""
            rows.append([mark, item.get("item", ""), item.get("source", ""),
                         item.get("responsible", ""),
                         item.get("status", "") + (f" - {item['note']}" if item.get("note")
                                                   else "")])
        return [Paragraph(_esc(f"{state.get('name') or 'Closing'}: closing checklist"),
                          styles["title"]),
                Paragraph(_esc(closing_state.tally(state) + " "
                               + closing_state.checklist_line(state)), styles["body"]),
                Spacer(1, 8), _table(rows, [6, 70, 30, 30, 34], styles)]
    return _render(flow)


__all__ = ["ClosingError", "checklist_pdf", "closing_index", "compile_set", "locate",
           "packets", "receive", "stamp_text", "survey", "to_pdf"]
