"""What travels with a file besides its words.

`checks.leftovers` reads a Word document's tracked changes, comments, hidden
text, watermark and author. This module reads the rest of what a careful
associate checks before a file leaves the firm, for every kind of file a
lawyer attaches:

- Word: document properties or a document system's custom properties naming
  a company the document never mentions (the precedent's client, or the last
  matter's); a spreadsheet or document embedded whole inside it; links to
  files on the firm's network.
- PowerPoint: speaker notes, hidden slides and comments.
- Excel: hidden sheets, cell comments, and formulas linked to other
  workbooks on the firm's network.
- PDF: sticky notes, highlights, stamps and drawings; attached files; scripts;
  and the author and source file name in its properties.

Every finding says where the thing is and what the recipient would see, in a
lawyer's words. Like the rest of the deterministic layer, these stay silent
unless the thing is definitely there: a property that names nobody, a link to
a public web page, a slide with no notes, are not reported.

`package()` is called on the reviewed document (checks.run_all) and on every
other attachment on the message (email_checks.other_attachments), because
what goes out is every file, not just the one we marked up.
"""

from __future__ import annotations

import logging
import re
import zipfile
from io import BytesIO

from lra.models import Finding, Severity

log = logging.getLogger(__name__)

_CP = "{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}"
_DC = "{http://purl.org/dc/elements/1.1/}"
_CUSTOM = "{http://schemas.openxmlformats.org/officeDocument/2006/custom-properties}"
_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"

# Words that are in company names everywhere and identify nobody. "Holdings"
# in the properties and "holdings" in a warranty are not the same company.
_GENERIC_NAME_WORDS = {
    "holdings", "holding", "group", "international", "global", "capital",
    "partners", "services", "industries", "systems", "limited", "company",
    "bank", "trust", "investments", "investment", "management", "properties",
    "property", "solutions", "technologies", "technology", "enterprises",
    "the", "and", "of", "for", "uk", "us", "usa", "europe", "north", "south",
    "east", "west", "new", "first", "general", "national", "american",
    "british", "corporation", "corp", "inc", "llc", "ltd", "plc", "llp",
}

# A file path rather than a web address: a mapped drive, a network share,
# a local path, or a host with no dots (an intranet server).
_LOCAL_TARGET = re.compile(
    r"^(?:file:|[A-Za-z]:[\\/]|\\\\|/(?:Users|Volumes|home|mnt)/|https?://[^/.:]+(?:[:/]|$))",
    re.IGNORECASE,
)


def package(content: bytes, filename: str, doc_text: str = "",
            label: str = "") -> list[Finding]:
    """Everything travelling in this file that its words do not show.

    `doc_text` is the document's readable text, used to tell a company the
    properties name from one the document is about. `label` prefixes each
    title with the file's name, for an attachment other than the one reviewed.
    """
    kind = _kind(content, filename)
    try:
        if kind == "docx":
            found = _word(content, doc_text)
        elif kind == "pptx":
            found = _deck(content, doc_text)
        elif kind == "xlsx":
            found = _workbook(content, doc_text)
        elif kind == "pdf":
            found = _pdf(content, doc_text)
        else:
            return []
    except Exception:  # a file we cannot inspect is reported elsewhere
        log.exception("could not inspect %s for what travels with it", filename)
        return []
    if label:
        for f in found:
            f.title = f"In {label}: {f.title[0].lower()}{f.title[1:]}"
    return found


def other_files(content: bytes, filename: str) -> list[Finding]:
    """An attachment we did not review, checked for what travels with it.

    For Word, the same leftovers the reviewed document is checked for
    (tracked changes, comments, hidden text) plus everything above; the
    author line is left out, because one per attachment is noise.
    """
    from lra.pipeline import checks

    text = _readable_text(content, filename)
    found: list[Finding] = []
    if _kind(content, filename) == "docx":
        from lra.pipeline.intake import _COMPARISON_NAME

        found = [f for f in checks.leftovers(content) if f.severity is not Severity.STYLE]
        if _COMPARISON_NAME.search(filename):
            # A blackline beside the clean copy is tracked changes by design.
            found = [f for f in found if "tracked changes" not in f.title]
        for f in found:
            f.title = f"In {filename}: {f.title[0].lower()}{f.title[1:]}"
    found += [f for f in package(content, filename, text, label=filename)
              if f.severity is not Severity.STYLE]
    return found


def _kind(content: bytes, filename: str) -> str:
    if content[:5] == b"%PDF-":
        return "pdf"
    if content[:2] != b"PK":
        return ""
    try:
        names = set(zipfile.ZipFile(BytesIO(content)).namelist())
    except Exception:  # noqa: BLE001
        return ""
    if "word/document.xml" in names:
        return "docx"
    if "ppt/presentation.xml" in names:
        return "pptx"
    if "xl/workbook.xml" in names:
        return "xlsx"
    return ""


def _readable_text(content: bytes, filename: str) -> str:
    """The words of an attachment we did not review, for the name test."""
    from lra.models import Attachment
    from lra.pipeline import extract

    try:
        doc = extract.extract(Attachment(filename=filename, content_type="",
                                         size_bytes=len(content), content=content))
    except Exception:  # noqa: BLE001 - unreadable text just means no name test
        return ""
    return "\n".join(b.text for b in doc.blocks)


# --------------------------------------------------------------------------
# Office packages: properties, embedded files, links
# --------------------------------------------------------------------------


def _xml(z: zipfile.ZipFile, name: str):
    from lxml import etree

    try:
        return etree.fromstring(z.read(name))
    except Exception:  # noqa: BLE001
        return None


def _office_common(z: zipfile.ZipFile, doc_text: str, embed_dir: str) -> list[Finding]:
    names = set(z.namelist())
    out: list[Finding] = []
    out += _properties_naming_others(z, names, doc_text)
    embedded = sorted(n for n in names if n.startswith(embed_dir) and not n.endswith("/"))
    if embedded:
        out.append(_embedded_finding(embedded))
    links = _local_links(z, names)
    if links:
        shown = links[0] if len(links) == 1 else f"{links[0]} and {len(links) - 1} more"
        out.append(Finding(
            severity=Severity.SUBSTANTIVE,
            category="leftovers",
            title=f"The file links to {'a file' if len(links) == 1 else 'files'} on "
                  f"your network: {shown}",
            explanation=(
                "A linked file is named by its full path, which shows the recipient "
                "your folder structure, often with client and matter names in it, "
                "and the link breaks on their machine. Break the link (paste the "
                "values) before this goes out."
            ),
            anchor="",
            confidence=0.9,
        ))
    return out


_EMBED_KIND = (
    (re.compile(r"\.xls[xmb]?$|Excel|Worksheet", re.IGNORECASE), "spreadsheet"),
    (re.compile(r"\.doc[xm]?$|Word", re.IGNORECASE), "Word document"),
    (re.compile(r"\.ppt[xm]?$|PowerPoint|Presentation", re.IGNORECASE), "presentation"),
    (re.compile(r"\.pdf$", re.IGNORECASE), "PDF"),
)


def _embedded_finding(embedded: list[str]) -> Finding:
    kinds = []
    for name in embedded:
        kind = next((k for pattern, k in _EMBED_KIND if pattern.search(name)), "file")
        kinds.append(kind)
    counted = sorted(set(kinds))
    what = (f"an embedded {counted[0]}" if len(kinds) == 1
            else f"{len(kinds)} embedded files ({', '.join(counted)})")
    return Finding(
        severity=Severity.SUBSTANTIVE,
        category="leftovers",
        title=f"The file carries {what}",
        explanation=(
            "An embedded object travels whole: the recipient can open it and see "
            "everything in it, not just the part shown on the page, such as every "
            "sheet of a workbook and the workings behind the figures. Paste it as a "
            "picture or as values if only the result is meant to go."
        ),
        anchor="",
        confidence=0.9,
    )


def _local_links(z: zipfile.ZipFile, names: set[str]) -> list[str]:
    """Targets of external relationships that are files, not web pages, and
    field codes that pull a file in. A template a document was made from is
    left out: every firm document names one, and it shows only the firm's
    template folder."""
    found: list[str] = []
    for name in sorted(names):
        if not name.endswith(".rels"):
            continue
        root = _xml(z, name)
        if root is None:
            continue
        for rel in root.iter(f"{_REL}Relationship"):
            if rel.get("TargetMode") != "External":
                continue
            kind = (rel.get("Type") or "").rsplit("/", 1)[-1]
            if kind in ("attachedTemplate", "hyperlink"):
                continue
            target = rel.get("Target") or ""
            if _LOCAL_TARGET.match(target) or kind in ("oleObject", "externalLinkPath",
                                                         "subDocument"):
                found.append(_tidy_path(target))
    for name in sorted(names):
        if not re.match(r"^word/(document|header\d*|footer\d*)\.xml$", name):
            continue
        xml = z.read(name).decode("utf-8", errors="replace")
        for code in re.findall(r"<w:instrText[^>]*>([^<]*)</w:instrText>", xml):
            m = re.match(r'\s*(?:LINK\s+\S+|INCLUDETEXT|INCLUDEPICTURE)\s+"([^"]+)"', code)
            if m and _LOCAL_TARGET.match(m.group(1).replace("\\\\", "\\")):
                found.append(_tidy_path(m.group(1).replace("\\\\", "\\")))
    return list(dict.fromkeys(found))


def _tidy_path(target: str) -> str:
    from urllib.parse import unquote

    return unquote(target.removeprefix("file:///").removeprefix("file://"))


def _property_values(z: zipfile.ZipFile, names: set[str]) -> list[tuple[str, str]]:
    """(where, value) for every property a person or a document system wrote
    about the file. The author and editor are not here: checks.leftovers
    reports those as an observation."""
    values: list[tuple[str, str]] = []
    if "docProps/core.xml" in names:
        root = _xml(z, "docProps/core.xml")
        if root is not None:
            for tag, label in ((f"{_DC}title", "title"), (f"{_DC}subject", "subject"),
                               (f"{_DC}description", "comments"),
                               (f"{_CP}keywords", "keywords"), (f"{_CP}category", "category")):
                for el in root.iter(tag):
                    if (el.text or "").strip():
                        values.append((f"its {label} property", el.text.strip()))
    if "docProps/custom.xml" in names:
        root = _xml(z, "docProps/custom.xml")
        if root is not None:
            for prop in root.iter(f"{_CUSTOM}property"):
                text = " ".join(t.strip() for t in prop.itertext() if t.strip())
                if text:
                    values.append((f'its custom property "{prop.get("name")}"', text))
    for name in sorted(names):
        if re.match(r"^customXml/item\d+\.xml$", name):
            root = _xml(z, name)
            if root is not None:
                text = " ".join(t.strip() for t in root.itertext() if t.strip())
                if text:
                    values.append(("the custom data stored in the file", text))
    return values


def _properties_naming_others(z: zipfile.ZipFile, names: set[str],
                              doc_text: str) -> list[Finding]:
    """A company named in the file's properties that the document never names.

    The commonest way another client's name travels: the document was built
    from the last deal's, and Word kept that deal's title, or the document
    system's stamp, in the properties. A company the document itself names is
    not reported: a title of "Acme / Beta SPA" on the Acme and Beta SPA is
    what a title is for.
    """
    from lra.pipeline import checks

    if not doc_text.strip():
        return []
    haystack = doc_text.lower()
    out: list[Finding] = []
    seen: set[str] = set()
    for where, value in _property_values(z, names):
        for m in checks._ENTITY_ANY_CASE.finditer(value):
            base = checks._entity_base(m.group(1))
            name = f"{base} {m.group(2)}".strip()
            if not base or name.lower() in seen or _named_in(base, haystack):
                continue
            if m.group(2).upper() == "LLP":
                continue          # a law firm, most often the firm itself
            seen.add(name.lower())
            out.append(Finding(
                severity=Severity.SUBSTANTIVE,
                category="leftovers",
                title=f"The file's properties name {name}, which the document never "
                      "mentions",
                explanation=(
                    f"{name} is in {where}, which travels with the file and shows in "
                    "File > Info. It usually means the document was started from "
                    "another matter's. Clear the properties before this goes out."
                ),
                anchor="",
                question=(f"The file's properties name {name}, which this document never "
                          "mentions. Clear them before it goes out?"),
                confidence=0.8,
            ))
    return out


def _named_in(base: str, haystack: str) -> bool:
    """Whether a company is named in the text, by any distinctive word of its
    name. No distinctive word means it cannot be told apart, so it counts as
    named and nothing is said."""
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z'&\-]+", base)
             if len(w) >= 3 and w.lower() not in _GENERIC_NAME_WORDS]
    if not words:
        return True
    return any(re.search(rf"\b{re.escape(w.lower())}\b", haystack) for w in words)


# --------------------------------------------------------------------------
# Word
# --------------------------------------------------------------------------


def _word(content: bytes, doc_text: str) -> list[Finding]:
    z = zipfile.ZipFile(BytesIO(content))
    return _office_common(z, doc_text, "word/embeddings/")


# --------------------------------------------------------------------------
# PowerPoint
# --------------------------------------------------------------------------


def _deck(content: bytes, doc_text: str) -> list[Finding]:
    z = zipfile.ZipFile(BytesIO(content))
    names = set(z.namelist())
    out = _office_common(z, doc_text, "ppt/embeddings/")

    noted = []
    for name in sorted(names, key=_number_in):
        if not re.match(r"^ppt/notesSlides/notesSlide\d+\.xml$", name):
            continue
        xml = z.read(name).decode("utf-8", errors="replace")
        # The body placeholder only: a notes page also carries the slide image
        # and its number, which are not notes anybody wrote.
        for sp in re.findall(r"<p:sp>.*?</p:sp>", xml, re.DOTALL):
            if 'type="body"' in sp and "".join(re.findall(r"<a:t>([^<]*)</a:t>", sp)).strip():
                noted.append(_slide_of_notes(z, name))
                break
    if noted:
        out.append(Finding(
            severity=Severity.SUBSTANTIVE,
            category="leftovers",
            title=f"Speaker notes are on {_slides(noted)}",
            explanation=(
                "Notes do not show in the slideshow but travel with the deck, and "
                "anyone who opens it in Normal view reads them. Speaking notes are "
                "usually the presenter's own: take them out, or send a PDF of the "
                "slides only."
            ),
            anchor="",
            confidence=0.9,
        ))

    order = _slide_order(z)
    hidden = []
    for name in sorted(names, key=_number_in):
        if re.match(r"^ppt/slides/slide\d+\.xml$", name):
            head = z.read(name)[:600].decode("utf-8", errors="replace")
            if re.search(r'<p:sld\b[^>]*\bshow="(?:0|false)"', head):
                hidden.append(order.get(name, _number_in(name)))
    if hidden:
        out.append(Finding(
            severity=Severity.SUBSTANTIVE,
            category="leftovers",
            title=f"{_slides(sorted(hidden), 'hidden')} the deck",
            explanation=(
                "A hidden slide is skipped in the slideshow but is in the file, and "
                "the recipient sees it in the slide list. Delete it if it is not "
                "meant for them."
            ),
            anchor="",
            confidence=0.95,
        ))

    count, authors = 0, set()
    for name in names:
        if re.match(r"^ppt/comments/[^/]+\.xml$", name):
            xml = z.read(name).decode("utf-8", errors="replace")
            count += len(re.findall(r"<(?:p:|p188:)?cm\b", xml))
    if "ppt/commentAuthors.xml" in names:
        xml = z.read("ppt/commentAuthors.xml").decode("utf-8", errors="replace")
        authors |= set(re.findall(r'\bname="([^"]+)"', xml))
    if "ppt/authors.xml" in names:
        xml = z.read("ppt/authors.xml").decode("utf-8", errors="replace")
        authors |= set(re.findall(r'\bname="([^"]+)"', xml))
    if count:
        out.append(_comments_finding(count, authors, "deck"))
    return out


def _number_in(name: str) -> int:
    m = re.search(r"(\d+)\.xml$", name)
    return int(m.group(1)) if m else 0


def _slide_order(z: zipfile.ZipFile) -> dict[str, int]:
    """Slide part -> its position in the deck, which is not its file name's
    number once slides have been reordered."""
    try:
        pres = z.read("ppt/presentation.xml").decode("utf-8", errors="replace")
        rels = z.read("ppt/_rels/presentation.xml.rels").decode("utf-8", errors="replace")
    except KeyError:
        return {}
    targets = dict(re.findall(r'Id="([^"]+)"[^>]*Target="([^"]+)"', rels))
    targets.update({k: v for v, k in re.findall(r'Target="([^"]+)"[^>]*Id="([^"]+)"', rels)})
    order: dict[str, int] = {}
    for position, rid in enumerate(re.findall(r'<p:sldId\b[^>]*r:id="([^"]+)"', pres), 1):
        target = targets.get(rid, "")
        if target:
            order["ppt/" + target.removeprefix("/ppt/").removeprefix("../")] = position
    return order


def _slide_of_notes(z: zipfile.ZipFile, notes: str) -> int:
    rels = notes.replace("notesSlides/", "notesSlides/_rels/") + ".rels"
    try:
        xml = z.read(rels).decode("utf-8", errors="replace")
    except KeyError:
        return _number_in(notes)
    m = re.search(r'Target="\.\./slides/(slide\d+\.xml)"', xml)
    if not m:
        return _number_in(notes)
    return _slide_order(z).get(f"ppt/slides/{m.group(1)}", _number_in(m.group(1)))


def _slides(numbers: list[int], hidden: str = "") -> str:
    from lra.pipeline.checks import _join

    shown = _join([str(n) for n in numbers[:6]]) + (" and others" if len(numbers) > 6 else "")
    if hidden:
        return (f"Slide {shown} is hidden in" if len(numbers) == 1
                else f"Slides {shown} are hidden in")
    return f"slide {shown}" if len(numbers) == 1 else f"slides {shown}"


def _comments_finding(count: int, authors: set[str], where: str) -> Finding:
    return Finding(
        severity=Severity.SUBSTANTIVE,
        category="leftovers",
        title=f"{count} comment{'s are' if count != 1 else ' is'} still in the {where}",
        explanation=(
            "Comments from " + (", ".join(sorted(authors)) if authors else "a reviewer")
            + " travel with the file and anyone who opens it can read them."
        ),
        anchor="",
        confidence=1.0,
    )


# --------------------------------------------------------------------------
# Excel
# --------------------------------------------------------------------------


def _workbook(content: bytes, doc_text: str) -> list[Finding]:
    z = zipfile.ZipFile(BytesIO(content))
    names = set(z.namelist())
    out = _office_common(z, doc_text, "xl/embeddings/")

    book = z.read("xl/workbook.xml").decode("utf-8", errors="replace")
    hidden = []
    for tag in re.findall(r"<sheet\b[^>]*>", book):
        if re.search(r'\bstate="(?:hidden|veryHidden)"', tag):
            name = re.search(r'\bname="([^"]*)"', tag)
            hidden.append(_unescape(name.group(1)) if name else "unnamed")
    if hidden:
        shown = ", ".join(f'"{h}"' for h in hidden[:5])
        out.append(Finding(
            severity=Severity.SUBSTANTIVE,
            category="leftovers",
            title=(f"The workbook has a hidden sheet: {shown}" if len(hidden) == 1
                   else f"The workbook has {len(hidden)} hidden sheets: {shown}"),
            explanation=(
                "A hidden sheet is one right-click away from being read. Workings, "
                "earlier figures and internal notes are what usually sit there. "
                "Delete it, or send values only."
            ),
            anchor="",
            confidence=0.95,
        ))

    count, authors = 0, set()
    for name in names:
        # Excel writes xl/comments1.xml; other writers xl/comments/comment1.xml.
        if re.match(r"^xl/comments(?:\d*|/[^/]+)\.xml$", name):
            xml = z.read(name).decode("utf-8", errors="replace")
            count += len(re.findall(r"<comment\b", xml))
            authors |= {_unescape(a) for a in re.findall(r"<author>([^<]+)</author>", xml)}
        elif re.match(r"^xl/threadedComments/[^/]+\.xml$", name) and count == 0:
            xml = z.read(name).decode("utf-8", errors="replace")
            count += len(re.findall(r"<threadedComment\b", xml))
    if count:
        out.append(_comments_finding(count, authors, "workbook"))

    if not any(f.title.startswith("The file links") for f in out):
        linked = sorted(n for n in names if re.match(r"^xl/externalLinks/externalLink\d+\.xml$", n))
        if linked:
            out.append(Finding(
                severity=Severity.SUBSTANTIVE,
                category="leftovers",
                title=f"Formulas link to {len(linked)} other workbook"
                      f"{'s' if len(linked) != 1 else ''}",
                explanation=(
                    "Linked formulas carry the other workbook's file name and its "
                    "last values, and break on the recipient's machine. Paste values "
                    "before this goes out."
                ),
                anchor="",
                confidence=0.9,
            ))
    return out


def _unescape(text: str) -> str:
    from html import unescape

    return unescape(text)


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------

# Mark-up a reviewer adds on top of the page, by what a lawyer calls it. The
# same table the PDF clean copy removes them by.
_PDF_KIND = {
    "/Text": "comment", "/FreeText": "comment", "/Caret": "comment",
    "/Highlight": "highlight", "/Underline": "highlight", "/Squiggly": "highlight",
    "/StrikeOut": "strike-out",
    "/Ink": "drawing", "/Line": "drawing", "/Square": "drawing", "/Circle": "drawing",
    "/Polygon": "drawing", "/PolyLine": "drawing",
    "/Stamp": "stamp", "/FileAttachment": "attached file", "/Sound": "sound clip",
}
_NOBODY = {"", "anonymous", "unknown", "user", "author", "admin", "administrator"}
_PATHLIKE_TITLE = re.compile(
    r"[\\/]|^Microsoft (?:Word|PowerPoint|Excel) - |\.(?:docx?|rtf|odt|xlsx?|pptx?|txt)\s*$",
    re.IGNORECASE)


def _pdf(content: bytes, doc_text: str) -> list[Finding]:
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(content))
    if reader.is_encrypted:
        return []
    out: list[Finding] = []

    kinds: dict[str, list[int]] = {}
    authors: set[str] = set()
    for number, page in enumerate(reader.pages, 1):
        for ref in page.get("/Annots") or []:
            try:
                annot = ref.get_object()
            except Exception:  # noqa: BLE001 - a broken annotation is not mark-up
                log.debug("unreadable annotation on page %d", number)
                continue
            subtype = str(annot.get("/Subtype", ""))
            kind = _PDF_KIND.get(subtype)
            if kind is None:
                continue
            kinds.setdefault(kind, []).append(number)
            who = str(annot.get("/T", "") or "").strip()
            if who and who.lower() not in _NOBODY:
                authors.add(who)
    if kinds:
        comments = kinds.get("comment", [])
        parts = []
        for kind in ("comment", "highlight", "strike-out", "drawing", "stamp",
                     "attached file", "sound clip"):
            n = len(kinds.get(kind, []))
            if n:
                parts.append(f"{n} {kind}{'s' if n != 1 else ''}")
        pages = sorted({p for ps in kinds.values() for p in ps})
        from lra.pipeline.checks import _join

        out.append(Finding(
            # A sticky note on a PDF going out is the PDF's version of a Word
            # comment left in, and as bad. A highlight on its own is a look.
            severity=Severity.BLOCKER if comments else Severity.SUBSTANTIVE,
            category="leftovers",
            title=f"The PDF still carries mark-up: {_join(parts)}",
            explanation=(
                f"On page{'s' if len(pages) != 1 else ''} "
                f"{_join([str(p) for p in pages[:8]])}"
                + (f", from {', '.join(sorted(authors))}" if authors else "")
                + ". Anyone who opens the PDF sees them, and comments list in the "
                "side panel. Export the PDF again from the clean document, or delete "
                "the mark-up, before it goes out."
            ),
            anchor="",
            confidence=1.0,
        ))

    root = reader.trailer["/Root"].get_object()
    names_tree = root.get("/Names")
    attached = False
    if names_tree is not None:
        names_tree = names_tree.get_object()
        attached = "/EmbeddedFiles" in names_tree
        scripted = "/JavaScript" in names_tree
    else:
        scripted = False
    if attached:
        files = []
        try:
            files = list(reader.attachments.keys())
        except Exception:  # noqa: BLE001 - reported without the names
            log.debug("could not list the PDF's attached files")
        out.append(Finding(
            severity=Severity.SUBSTANTIVE,
            category="leftovers",
            title="The PDF has a file attached inside it"
                  + (f": {', '.join(files[:3])}" if files else ""),
            explanation=(
                "A file attached to a PDF travels with it and opens from the "
                "attachments panel. Workings and earlier drafts are what usually "
                "end up there."
            ),
            anchor="",
            confidence=0.95,
        ))
    if scripted or "/OpenAction" in root and "/JS" in str(root.get("/OpenAction")):
        out.append(Finding(
            severity=Severity.SUBSTANTIVE,
            category="leftovers",
            title="The PDF runs a script when it is opened",
            explanation=(
                "Firms' mail filters and clients' security teams often block PDFs "
                "with scripts, and the recipient may not receive this at all."
            ),
            anchor="",
            confidence=0.9,
        ))

    info = reader.metadata or {}
    author = str(info.get("/Author", "") or "").strip()
    title = str(info.get("/Title", "") or "").strip()
    seen_props: list[str] = []
    if author and author.lower() not in _NOBODY:
        seen_props.append(f"{author} as its author")
    if title and _PATHLIKE_TITLE.search(title):
        seen_props.append(f'the file it was made from, "{title}"')
    if seen_props:
        from lra.pipeline.checks import _join

        out.append(Finding(
            severity=Severity.STYLE,
            category="leftovers",
            title="The PDF's properties name " + _join(seen_props),
            explanation=(
                "Document properties travel with the PDF and show in its properties "
                "window. Worth clearing before this goes outside the firm."
            ),
            anchor="",
            confidence=1.0,
        ))

    # The same test as for Word: a company in the title, subject or keywords
    # that the document never names.
    if doc_text.strip():
        from lra.pipeline import checks

        haystack = doc_text.lower()
        for key in ("/Title", "/Subject", "/Keywords"):
            value = str(info.get(key, "") or "")
            for m in checks._ENTITY_ANY_CASE.finditer(value):
                base = checks._entity_base(m.group(1))
                if base and m.group(2).upper() != "LLP" and not _named_in(base, haystack):
                    name = f"{base} {m.group(2)}"
                    out.append(Finding(
                        severity=Severity.SUBSTANTIVE,
                        category="leftovers",
                        title=f"The PDF's properties name {name}, which the document "
                              "never mentions",
                        explanation=(
                            f"{name} is in its {key.strip('/').lower()} property, which "
                            "travels with the file. It usually means the PDF was made "
                            "from another matter's document."
                        ),
                        anchor="",
                        question=(f"The PDF's properties name {name}, which this "
                                  "document never mentions. Clear them before it goes out?"),
                        confidence=0.8,
                    ))
                    break
    return out
