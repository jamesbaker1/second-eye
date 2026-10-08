"""A clean copy of a PDF: the pages, and nothing travelling with them.

The Word clean copy (`clean.py`) is half the send-moment job, because what the
other side receives is as often a PDF: an export that still carries the
drafter's name in its properties, the sticky notes from the partner's read,
the spreadsheet somebody attached to it, and a producer string naming the
firm's document system.

Everything here is done on the PDF's object tree with pypdf. The page content
streams, which are what the reader sees, are never opened or rewritten.

What comes out:

- Document properties that name people or software (Author, Creator,
  Producer), custom keys a document system adds, and the Title when it is a
  file name or path. An ordinary title and the dates stay.
- XMP metadata, wherever it is attached: the same details stored a second way.
- Comments and mark-up: sticky notes, free text, highlights, strike-outs,
  drawings, stamps, and the pop-ups that belong to them. Links and form
  fields are not comments and stay, values and all.
- Embedded files, in the attachments list or pinned to a page.
- JavaScript and actions that run by themselves (on open, on page view).
- Private application data (PieceInfo) that editors leave behind.

The same two rules as the Word clean copy. **Never change the words**: every
page's text is read before and after and must match, and the copy is re-read
from scratch and every object in it checked for anything that should have
gone. **Refuse rather than guess**: a password-protected, signed or
part-redacted PDF is declined in a sentence rather than half cleaned.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from io import BytesIO

from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DictionaryObject,
    IndirectObject,
    NameObject,
    PdfObject,
)

from secondeye.pipeline.clean import CannotClean, CleanCopy

log = logging.getLogger(__name__)

# Mark-up annotations, by what a lawyer would call them. Everything here is
# something a reviewer added on top of the page; links, form fields and
# anything we do not recognise are left alone.
_MARKUP: dict[str, str] = {
    "/Text": "comment", "/FreeText": "comment", "/Caret": "comment",
    "/Highlight": "highlight", "/Underline": "highlight",
    "/Squiggly": "highlight", "/StrikeOut": "highlight",
    "/Ink": "drawing", "/Line": "drawing", "/Square": "drawing",
    "/Circle": "drawing", "/Polygon": "drawing", "/PolyLine": "drawing",
    "/Stamp": "stamp",
    "/FileAttachment": "attached file",
    "/Sound": "sound clip",
}
_KINDS = ("comment", "highlight", "drawing", "stamp", "attached file", "sound clip")

# Actions that do something other than move the reader around the document.
_ACTIVE = {"/JavaScript", "/Launch", "/ImportData", "/RichMediaExecute"}

# Info keys that are not people, software or somebody's own description.
_KEPT_INFO = {"/Title", "/CreationDate", "/ModDate", "/Trapped"}
_PEOPLE_INFO = {"/Author": "author", "/Creator": "producer", "/Producer": "producer",
                "/Subject": "subject", "/Keywords": "keywords"}

# A Title that is really a file name: "C:\Users\jb\Draft v3.docx",
# "/Volumes/Matters/NDA.pdf", or Word's own "Microsoft Word - NDA v2.docx".
_PATHLIKE = re.compile(
    r"[\\/]|^Microsoft (Word|PowerPoint|Excel) - |\.(docx?|pdf|rtf|odt|xlsx?|pptx?|txt)\s*$",
    re.IGNORECASE)


@dataclass
class _Tally:
    info: set[str] = field(default_factory=set)       # "author", "producer", ...
    custom_info: bool = False
    path_title: bool = False
    xmp: bool = False
    marks: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    mark_authors: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    files: list[str] = field(default_factory=list)
    scripts: int = 0
    auto_actions: int = 0
    private_data: bool = False


def clean(content: bytes) -> CleanCopy:
    """Return the PDF with its metadata, mark-up, attachments and scripts removed."""
    try:
        reader = PdfReader(BytesIO(content))
        encrypted = reader.is_encrypted
    except Exception as e:
        raise CannotClean("I could not open that PDF, so I have not cleaned it.") from e
    if encrypted:
        raise CannotClean("It's password-protected; send it unlocked and I'll clean it.")

    try:
        before = _page_text(reader)
        writer = PdfWriter(clone_from=reader)
    except Exception as e:
        log.warning("could not read the PDF to clean it: %s", e)
        raise CannotClean("I could not read that PDF well enough to check a clean "
                          "copy of it, so I have not made one.") from e

    root = writer.root_object
    _refuse_signed_or_redacted(writer)

    tally = _Tally()
    _clean_info(reader, writer, tally)

    # Record everything before anything is removed, so that a script or file
    # reached only through something that is about to go is still counted.
    _survey(root, tally, set())

    gone = _strip_annotations(writer)
    _strip_catalog(root)
    _sweep(root, gone, set())
    _drop_unreachable(writer)

    removed = _describe(tally)
    if not removed:
        return CleanCopy(content=content, removed=[])

    out = BytesIO()
    writer.write(out)
    cleaned = out.getvalue()
    _check(cleaned, before)
    return CleanCopy(content=cleaned, removed=removed)


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


def _refuse_signed_or_redacted(writer: PdfWriter) -> None:
    form = _resolve(writer.root_object.get("/AcroForm"))
    if isinstance(form, DictionaryObject) and int(_resolve(form.get("/SigFlags", 0)) or 0) & 1:
        raise CannotClean("It is digitally signed, and cleaning it would break the "
                          "signature, so I have left it alone.")
    for page in writer.pages:
        for ref in _resolve(page.get("/Annots")) or []:
            annot = _resolve(ref)
            if isinstance(annot, DictionaryObject) and annot.get("/Subtype") == "/Redact":
                # Removing the mark would leave the text it covers in plain view.
                raise CannotClean("It has redactions marked but not applied. Apply them "
                                  "and send it again, and I'll clean it.")


# --------------------------------------------------------------------------
# Document properties
# --------------------------------------------------------------------------


def _clean_info(reader: PdfReader, writer: PdfWriter, tally: _Tally) -> None:
    info = reader.metadata or {}
    kept: dict[str, str] = {}
    for key, value in info.items():
        text = str(value).strip()
        if key in _KEPT_INFO:
            if key == "/Title" and text and _PATHLIKE.search(text):
                tally.path_title = True
            elif text:
                kept[key] = value
        elif not text:
            continue
        elif key in _PEOPLE_INFO:
            tally.info.add(_PEOPLE_INFO[key])
        else:
            tally.custom_info = True
    writer.metadata = None
    if kept:
        writer.add_metadata(kept)
        # pypdf names itself as the producer when it writes; it is not asked to.
        writer._info.pop(NameObject("/Producer"), None)


# --------------------------------------------------------------------------
# Survey, then removal
# --------------------------------------------------------------------------


def _survey(node: PdfObject, tally: _Tally, seen: set[int]) -> None:
    if isinstance(node, IndirectObject):
        if node.idnum in seen:
            return
        seen.add(node.idnum)
    node = _resolve(node)
    if isinstance(node, ArrayObject):
        for item in node:
            _survey(item, tally, seen)
        return
    if not isinstance(node, DictionaryObject):
        return

    subtype = node.get("/Subtype")
    if "/Rect" in node and subtype in _MARKUP:
        kind = _MARKUP[subtype]
        tally.marks[kind] += 1
        author = str(_resolve(node.get("/T")) or "").strip()
        if author:
            tally.mark_authors[kind].add(author)
    action = node.get("/S")
    if action == "/JavaScript":
        tally.scripts += 1
    elif action in _ACTIVE:
        tally.auto_actions += 1
    if "/EF" in node:
        tally.files.append(_file_name(node))
    if "/Metadata" in node:
        tally.xmp = True
    if "/PieceInfo" in node:
        tally.private_data = True

    for key, value in node.items():
        if key in ("/Parent", "/P"):      # back up the tree; reached from above
            continue
        _survey(value, tally, seen)


def _strip_annotations(writer: PdfWriter) -> set[int]:
    """Drop mark-up and its pop-ups from every page. Returns the removed ids."""
    gone: set[int] = set()
    for page in writer.pages:
        annots = _resolve(page.get("/Annots"))
        if not annots:
            continue
        keep, dropped = ArrayObject(), []
        for ref in annots:
            annot = _resolve(ref)
            if isinstance(annot, DictionaryObject) and annot.get("/Subtype") in _MARKUP:
                dropped.append(ref)
            else:
                keep.append(ref)
        dropped_ids = {r.idnum for r in dropped if isinstance(r, IndirectObject)}
        final = ArrayObject()
        for ref in keep:
            annot = _resolve(ref)
            if isinstance(annot, DictionaryObject) and annot.get("/Subtype") == "/Popup":
                parent = annot.get("/Parent")
                if parent is None or (isinstance(parent, IndirectObject)
                                      and parent.idnum in dropped_ids):
                    if isinstance(ref, IndirectObject):
                        dropped_ids.add(ref.idnum)
                    continue
            final.append(ref)
        gone |= dropped_ids
        if final:
            page[NameObject("/Annots")] = final
        else:
            del page["/Annots"]
    return gone


def _strip_catalog(root: DictionaryObject) -> None:
    opening = _resolve(root.get("/OpenAction"))
    if isinstance(opening, DictionaryObject):     # an action, not a destination
        del root["/OpenAction"]
    names = _resolve(root.get("/Names"))
    if isinstance(names, DictionaryObject):
        for key in ("/JavaScript", "/EmbeddedFiles"):
            names.pop(NameObject(key), None)
        if not names:
            del root["/Names"]
    root.pop(NameObject("/Collection"), None)     # a PDF portfolio's file list


def _sweep(node: PdfObject, gone: set[int], seen: set[int]) -> None:
    """Remove what hangs off any object: XMP, private data, automatic
    actions, embedded file streams, active actions, and references to the
    annotations that have gone."""
    if isinstance(node, IndirectObject):
        if node.idnum in seen:
            return
        seen.add(node.idnum)
    node = _resolve(node)
    if isinstance(node, ArrayObject):
        node[:] = [i for i in node
                   if not (isinstance(i, IndirectObject) and i.idnum in gone)]
        for item in node:
            _sweep(item, gone, seen)
        return
    if not isinstance(node, DictionaryObject):
        return
    for key in ("/Metadata", "/PieceInfo", "/AA", "/EF"):
        node.pop(NameObject(key), None)
    for key in ("/A", "/Next", "/OpenAction"):
        action = _resolve(node.get(key))
        if isinstance(action, DictionaryObject) and action.get("/S") in _ACTIVE:
            del node[key]
    for key in list(node):
        value = node[key]
        if isinstance(value, IndirectObject) and value.idnum in gone:
            del node[key]
        elif key not in ("/Parent", "/P"):
            _sweep(value, gone, seen)


def _drop_unreachable(writer: PdfWriter) -> None:
    """Forget every object nothing points at any more.

    Unhooking a comment from its page does not take it out of the file: pypdf
    writes every object it holds, so the note's text would still be in the
    bytes for anyone who opens them in an editor. pypdf's own orphan removal
    is a single pass and keeps a chain (the note, then its pop-up) alive.
    """
    reachable: set[int] = set()
    info = writer._info
    stack: list = [writer.root_object.indirect_reference,
                   info.indirect_reference if info is not None else None]
    while stack:
        node = stack.pop()
        if isinstance(node, IndirectObject):
            if node.pdf is not writer or node.idnum in reachable:
                continue
            reachable.add(node.idnum)
            node = node.get_object()
        if isinstance(node, DictionaryObject):
            stack.extend(node.values())
        elif isinstance(node, ArrayObject):
            stack.extend(node)
    for i in range(len(writer._objects)):
        if i + 1 not in reachable:
            writer._objects[i] = None


# --------------------------------------------------------------------------
# What to tell the lawyer, and the proof
# --------------------------------------------------------------------------


def _describe(t: _Tally) -> list[str]:
    out: list[str] = []
    for kind in _KINDS:
        n = t.marks.get(kind, 0)
        if n:
            who = t.mark_authors.get(kind)
            out.append(f"{_count(n, kind)}{f' from {_names(who)}' if who else ''}")
    if t.files:
        names = _names(t.files)
        out.append(f"An embedded file: {names}" if len(t.files) == 1
                   else f"{len(t.files)} embedded files: {names}")
    people = [f for f in ("author", "producer") if f in t.info]
    if people:
        out.append(_join(people).capitalize() + " names")
    described = [f for f in ("subject", "keywords") if f in t.info]
    if described:
        out.append(_join(described).capitalize())
    if t.path_title:
        out.append("A title that gave away a file name")
    if t.custom_info:
        out.append("Document-system properties (matter and document numbers)")
    if t.xmp:
        out.append("XMP metadata (the same details, stored a second way)")
    if t.scripts:
        out.append("A script" if t.scripts == 1 else f"{t.scripts} scripts")
    if t.auto_actions:
        out.append(_count(t.auto_actions, "action") + " set to run by itself")
    if t.private_data:
        out.append("Private data left by the editing software")
    return out


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'s' if n != 1 else ''}"


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _names(names) -> str:
    return _join(sorted(set(names)) if not isinstance(names, list) else names)


def _file_name(spec: DictionaryObject) -> str:
    for key in ("/UF", "/F"):
        name = _resolve(spec.get(key))
        if name:
            return str(name)
    return "unnamed"


def _page_text(reader: PdfReader) -> list[str]:
    return [" ".join((page.extract_text() or "").split()) for page in reader.pages]


def _check(cleaned: bytes, before: list[str]) -> None:
    """Re-read the copy from its bytes, as a recipient's viewer would."""
    try:
        reader = PdfReader(BytesIO(cleaned))
        after = _page_text(reader)
        leftovers = _leftovers(reader)
    except Exception as e:
        log.error("clean PDF failed to re-open: %s", e)
        raise CannotClean("I could not produce a clean copy I was sure would "
                          "open, so I have not sent one.") from e
    if after != before:
        log.error("clean PDF text differs from the original")
        raise CannotClean("Cleaning that PDF would have changed its words, "
                          "so I stopped. Nothing was altered.")
    if leftovers:
        log.error("clean PDF still carries: %s", sorted(leftovers))
        raise CannotClean("I could not remove everything from that PDF, so "
                          "I have not sent a copy that only looks clean.")


def _leftovers(reader: PdfReader) -> set[str]:
    """Every object in the file, not just the ones reachable from its pages."""
    found: set[str] = set()
    info = reader.trailer.get("/Info")
    if info is not None:
        keys = set(_resolve(info).keys())
        if keys - _KEPT_INFO:
            found.add("info")
    ids: set[tuple[int, int]] = set()
    for gen, table in reader.xref.items():
        ids |= {(idnum, gen) for idnum in table}
    ids |= {(idnum, 0) for idnum in reader.xref_objStm}
    for idnum, gen in ids:
        obj = reader.get_object(IndirectObject(idnum, gen, reader))
        if not isinstance(obj, DictionaryObject):
            continue
        if "/Rect" in obj and obj.get("/Subtype") in _MARKUP:
            found.add("markup")
        if obj.get("/S") in _ACTIVE or "/JS" in obj:
            found.add("script")
        if "/EF" in obj or obj.get("/Type") == "/EmbeddedFile":
            found.add("file")
        for key in ("/Metadata", "/PieceInfo", "/AA", "/JavaScript", "/EmbeddedFiles"):
            if key in obj:
                found.add(key)
    return found


def _resolve(obj):
    return obj.get_object() if isinstance(obj, IndirectObject) else obj
