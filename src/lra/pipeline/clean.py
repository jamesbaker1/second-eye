"""A clean copy: the document with nothing travelling in it but the document.

`checks.leftovers` tells a lawyer their file still carries tracked changes, a
colleague's comments and the name of whoever drafted it. That is half a job:
the other half is handing back a copy without them, and a list of exactly what
came out so nothing was removed that they did not know about.

Done on the package itself rather than through python-docx, because most of
what has to go is not text: relationship entries, content-type overrides,
property parts, revision-session ids. Every part is rewritten from parsed XML,
never by string substitution.

Two rules, the same two as the redliner.

**Never change the words.** Accepting tracked changes and dropping hidden text
are the only edits to what the document says, both are listed in the reply, and
the result is checked against an independent reading of the original before it
is returned. Anything else differing is a bug, and the clean copy does not ship.

**Refuse rather than guess.** A deleted table cell or a merged-cell revision
cannot be accepted without rebuilding the table grid, so a document carrying
one is declined with a sentence, not approximated.
"""

from __future__ import annotations

import logging
import re
import zipfile
from dataclasses import dataclass, field
from io import BytesIO

from lxml import etree

log = logging.getLogger(__name__)

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CP = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
DC = "http://purl.org/dc/elements/1.1/"
EP = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"


def _w(tag: str) -> str:
    return f"{{{W}}}{tag}"


_WORD_PART = re.compile(
    r"^word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml$"
)

# Parts that exist only to hold comments, or to say who wrote them.
_COMMENT_PARTS = re.compile(
    r"^word/(comments|commentsExtended|commentsIds|commentsExtensible|people)\.xml$"
)

# A record of what formatting used to be. Dropping one accepts the change.
_FORMAT_CHANGES = {
    _w(t) for t in ("rPrChange", "pPrChange", "sectPrChange", "tblPrChange",
                    "trPrChange", "tcPrChange", "tblGridChange", "tblPrExChange",
                    "numberingChange")
}
_COMMENT_MARKS = {_w("commentRangeStart"), _w("commentRangeEnd")}
_MOVE_MARKS = {
    _w(t) for t in ("moveFromRangeStart", "moveFromRangeEnd",
                    "moveToRangeStart", "moveToRangeEnd")
}

# Core properties that say who, or describe the file in somebody's own words.
_CORE_PEOPLE = (f"{{{DC}}}creator", f"{{{CP}}}lastModifiedBy")
_CORE_DESCRIPTIVE = (
    f"{{{DC}}}title", f"{{{DC}}}subject", f"{{{DC}}}description",
    f"{{{CP}}}keywords", f"{{{CP}}}category", f"{{{CP}}}contentStatus",
)
_APP_FIELDS = ("Company", "Manager", "HyperlinkBase")


class CannotClean(Exception):
    """Carries a sentence we are willing to email back verbatim."""


@dataclass
class CleanCopy:
    content: bytes
    # One plain sentence per kind of thing removed, for the reply.
    removed: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.removed)


@dataclass
class _Tally:
    insertions: int = 0
    deletions: int = 0
    format_changes: int = 0
    authors: set[str] = field(default_factory=set)
    comments: int = 0
    comment_authors: set[str] = field(default_factory=set)
    hidden_runs: int = 0
    rsids: bool = False


def clean(content: bytes) -> CleanCopy:
    """Return the document with revisions accepted and its metadata removed."""
    try:
        package = zipfile.ZipFile(BytesIO(content))
        names = package.namelist()
        parts = {name: package.read(name) for name in names}
    except Exception as e:
        raise CannotClean("I could not open that file as a Word document.") from e
    if "word/document.xml" not in parts:
        raise CannotClean("I can only make a clean copy of a Word document.")

    tally = _Tally()
    removed: list[str] = []
    before = _flat_text(parts["word/document.xml"], original=True)

    for name in names:
        if _WORD_PART.match(name):
            tree = _parse(parts[name], name)
            _accept_revisions(tree, tally)
            _strip_comment_marks(tree)
            _strip_hidden(tree, tally)
            _strip_rsids(tree, tally)
            parts[name] = _serialise(tree)

    comment_parts = [n for n in names if _COMMENT_PARTS.match(n)]
    if "word/comments.xml" in parts:
        comments = _parse(parts["word/comments.xml"], "comments")
        found = comments.findall(_w("comment"))
        tally.comments = len(found)
        tally.comment_authors = {c.get(_w("author")) for c in found if c.get(_w("author"))}
    dropped = set(comment_parts)

    custom = [n for n in names if n.startswith("docProps/custom")]
    dropped |= set(custom)

    if "word/settings.xml" in parts:
        parts["word/settings.xml"], template = _clean_settings(
            parts["word/settings.xml"], tally, removed)
        if template:
            rels = "word/_rels/settings.xml.rels"
            if rels in parts:
                parts[rels] = _drop_relationships(parts[rels], ids={template})

    for name in ("word/styles.xml", "word/numbering.xml"):
        if name in parts:
            tree = _parse(parts[name], name)
            _strip_rsids(tree, tally)
            parts[name] = _serialise(tree)

    if "docProps/core.xml" in parts:
        parts["docProps/core.xml"] = _clean_core(parts["docProps/core.xml"], removed)
    if "docProps/app.xml" in parts:
        parts["docProps/app.xml"] = _clean_app(parts["docProps/app.xml"], removed)

    # Every mention of a dropped part has to go with it, or Word reports the
    # file as damaged: the relationship that points at it, and its content type.
    for name in list(parts):
        if name.endswith(".rels"):
            parts[name] = _drop_relationships(parts[name], targets=dropped, rels_name=name)
    parts["[Content_Types].xml"] = _drop_content_types(parts["[Content_Types].xml"], dropped)
    for name in dropped:
        parts.pop(name, None)
        parts.pop(_rels_for(name), None)

    removed = _describe(tally, bool(custom)) + removed

    out = BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name in names:                      # original order: [Content_Types] first
            if name in parts:
                z.writestr(name, parts[name])
    cleaned = out.getvalue()

    _check(cleaned, before)
    return CleanCopy(content=cleaned, removed=removed)


# --------------------------------------------------------------------------
# Revisions
# --------------------------------------------------------------------------


def _accept_revisions(tree, tally: _Tally) -> None:
    for element in tree.iter(_w("cellDel"), _w("cellMerge")):
        raise CannotClean(
            "This document has a tracked change that deletes or merges table "
            "cells. Accepting that means rebuilding the table, which I will not "
            "guess at. Accept the table changes in Word and send it again."
        )

    # Rows first: a deleted row takes everything in it, including revisions
    # that would otherwise be counted and unwrapped below.
    for row in list(tree.iter(_w("tr"))):
        props = row.find(_w("trPr"))
        if props is None:
            continue
        if props.find(_w("del")) is not None:
            _note_author(props.find(_w("del")), tally)
            tally.deletions += 1
            row.getparent().remove(row)
        elif props.find(_w("ins")) is not None:
            _note_author(props.find(_w("ins")), tally)
            tally.insertions += 1
            props.remove(props.find(_w("ins")))

    # Paragraph marks. A deleted mark joins the paragraph to the one after it,
    # and Word gives the result the following paragraph's formatting.
    for paragraph in list(tree.iter(_w("p"))):
        mark_props = paragraph.find(f"{_w('pPr')}/{_w('rPr')}")
        if mark_props is None:
            continue
        inserted = mark_props.find(_w("ins"))
        if inserted is not None:
            _note_author(inserted, tally)
            mark_props.remove(inserted)
        deleted = mark_props.find(_w("del"))
        if deleted is None:
            continue
        _note_author(deleted, tally)
        mark_props.remove(deleted)
        following = paragraph.getnext()
        if following is None or following.tag != _w("p"):
            continue        # nothing to join to; the mark simply stays
        content = [c for c in paragraph if c.tag != _w("pPr")]
        anchor = following.find(_w("pPr"))
        position = 0 if anchor is None else list(following).index(anchor) + 1
        for offset, child in enumerate(content):
            following.insert(position + offset, child)
        paragraph.getparent().remove(paragraph)

    for element in list(tree.iter(_w("del"), _w("moveFrom"))):
        if element.getparent() is None:
            continue
        _note_author(element, tally)
        tally.deletions += 1
        element.getparent().remove(element)

    for element in list(tree.iter(_w("ins"), _w("moveTo"))):
        parent = element.getparent()
        if parent is None:
            continue
        _note_author(element, tally)
        tally.insertions += 1
        index = parent.index(element)
        for offset, child in enumerate(list(element)):
            parent.insert(index + offset, child)
        parent.remove(element)

    for element in list(tree.iter(*_FORMAT_CHANGES, _w("cellIns"))):
        _note_author(element, tally)
        tally.format_changes += 1
        element.getparent().remove(element)

    for element in list(tree.iter(*_MOVE_MARKS)):
        element.getparent().remove(element)

    # A paragraph emptied by accepting its deletion, with a deleted mark and no
    # neighbour to join, is left as Word leaves it: an empty paragraph.


def _note_author(element, tally: _Tally) -> None:
    author = element.get(_w("author"))
    if author:
        tally.authors.add(author)


def _strip_comment_marks(tree) -> None:
    for element in list(tree.iter(*_COMMENT_MARKS)):
        element.getparent().remove(element)
    for reference in list(tree.iter(_w("commentReference"))):
        run = reference.getparent()
        run.remove(reference)
        # The run existed to hold the reference mark. Leave it if it holds
        # anything else.
        if run.tag == _w("r") and all(c.tag == _w("rPr") for c in run):
            run.getparent().remove(run)


def _is_on(flag) -> bool:
    return flag is not None and flag.get(_w("val"), "true") not in ("0", "false", "off")


def _strip_hidden(tree, tally: _Tally) -> None:
    for run in list(tree.iter(_w("r"))):
        props = run.find(_w("rPr"))
        if props is not None and _is_on(props.find(_w("vanish"))):
            tally.hidden_runs += 1
            run.getparent().remove(run)


def _strip_rsids(tree, tally: _Tally) -> None:
    """Revision-session ids fingerprint which edits were made in one sitting."""
    for element in tree.iter():
        if not isinstance(element.tag, str):
            continue
        for attribute in [a for a in element.attrib if a.startswith(f"{{{W}}}rsid")]:
            del element.attrib[attribute]
            tally.rsids = True


# --------------------------------------------------------------------------
# Settings and properties
# --------------------------------------------------------------------------


def _clean_settings(raw: bytes, tally: _Tally, removed: list[str]) -> tuple[bytes, str | None]:
    tree = _parse(raw, "settings")
    template_rel = None
    for tag in ("rsids", "trackRevisions", "docVars"):
        for element in tree.findall(_w(tag)):
            if tag == "rsids":
                tally.rsids = True
            if tag == "docVars":
                removed.append(f"{_count(len(element), 'document variable')} set by a "
                               "template or document system.")
            tree.remove(element)
    for element in tree.findall(_w("attachedTemplate")):
        template_rel = element.get(f"{{{R}}}id")
        removed.append("The path of the template the document was built from.")
        tree.remove(element)
    return _serialise(tree), template_rel


def _clean_core(raw: bytes, removed: list[str]) -> bytes:
    tree = _parse(raw, "core properties")
    people = sorted({
        (tree.findtext(tag) or "").strip() for tag in _CORE_PEOPLE
    } - {""})
    if people:
        removed.append("Author and last-editor names: " + ", ".join(people) + ".")
    described = [
        (tree.findtext(tag) or "").strip() for tag in _CORE_DESCRIPTIVE
        if (tree.findtext(tag) or "").strip()
    ]
    if described:
        removed.append("Title, subject and keyword properties: "
                       + "; ".join(f"“{d}”" for d in described) + ".")
    for tag in _CORE_PEOPLE + _CORE_DESCRIPTIVE + (f"{{{CP}}}lastPrinted",):
        for element in tree.findall(tag):
            tree.remove(element)
    for element in tree.findall(f"{{{CP}}}revision"):
        element.text = "1"
    return _serialise(tree)


def _clean_app(raw: bytes, removed: list[str]) -> bytes:
    tree = _parse(raw, "application properties")
    found = []
    for name in _APP_FIELDS:
        for element in tree.findall(f"{{{EP}}}{name}"):
            if (element.text or "").strip():
                found.append(f"{name.lower()} “{element.text.strip()}”")
            tree.remove(element)
    for element in tree.findall(f"{{{EP}}}Template"):
        if (element.text or "").strip() not in ("", "Normal.dotm", "Normal"):
            found.append(f"template “{element.text.strip()}”")
        element.text = "Normal.dotm"
    for element in tree.findall(f"{{{EP}}}TotalTime"):
        if (element.text or "0").strip() not in ("", "0"):
            found.append(f"total editing time ({element.text.strip()} minutes)")
        element.text = "0"
    if found:
        removed.append("Application properties: " + ", ".join(found) + ".")
    return _serialise(tree)


# --------------------------------------------------------------------------
# Package bookkeeping
# --------------------------------------------------------------------------


def _rels_for(part: str) -> str:
    folder, _, leaf = part.rpartition("/")
    return f"{folder}/_rels/{leaf}.rels" if folder else f"_rels/{leaf}.rels"


def _drop_relationships(raw: bytes, targets: set[str] | None = None,
                        ids: set[str] | None = None, rels_name: str = "") -> bytes:
    tree = _parse(raw, "relationships")
    # Targets are relative to the folder of the part the .rels file belongs to.
    base = rels_name.rsplit("_rels/", 1)[0] if rels_name else ""
    for relationship in list(tree):
        if ids and relationship.get("Id") in ids:
            tree.remove(relationship)
            continue
        if not targets or relationship.get("TargetMode") == "External":
            continue
        target = relationship.get("Target", "")
        resolved = target.lstrip("/") if target.startswith("/") else _join(base, target)
        if resolved in targets:
            tree.remove(relationship)
    return _serialise(tree)


def _join(base: str, target: str) -> str:
    parts = [p for p in base.split("/") if p]
    for piece in target.split("/"):
        if piece == "..":
            if parts:
                parts.pop()
        elif piece and piece != ".":
            parts.append(piece)
    return "/".join(parts)


def _drop_content_types(raw: bytes, dropped: set[str]) -> bytes:
    tree = _parse(raw, "content types")
    for override in list(tree.findall(f"{{{CT}}}Override")):
        if override.get("PartName", "").lstrip("/") in dropped:
            tree.remove(override)
    return _serialise(tree)


def _parse(raw: bytes, label: str):
    try:
        return etree.fromstring(raw)
    except etree.XMLSyntaxError as e:
        raise CannotClean(
            f"Part of that document ({label}) is damaged, so I have not tried to "
            "clean it."
        ) from e


def _serialise(tree) -> bytes:
    return etree.tostring(tree, xml_declaration=True, encoding="UTF-8", standalone=True)


# --------------------------------------------------------------------------
# What to tell the lawyer, and the proof
# --------------------------------------------------------------------------


def _describe(tally: _Tally, custom_properties: bool) -> list[str]:
    """What came out, one short line each, naming whose changes were accepted.

    Whose matters more than how many: accepting every tracked change also
    accepts the other side's, and a lawyer who sees a counterparty's name here
    knows to look before this goes anywhere.
    """
    out: list[str] = []
    revisions = tally.insertions + tally.deletions + tally.format_changes
    if revisions:
        who = f" by {_names(tally.authors)}" if tally.authors else ""
        out.append(f"{_count(revisions, 'tracked change')}{who}, accepted")
    if tally.comments:
        who = f" from {_names(tally.comment_authors)}" if tally.comment_authors else ""
        out.append(f"{_count(tally.comments, 'comment')}{who}")
    if tally.hidden_runs:
        out.append(f"{_count(tally.hidden_runs, 'piece')} of hidden text")
    if custom_properties:
        out.append("Document-system properties (matter and document numbers)")
    if tally.rsids:
        out.append("Hidden editing history")
    return out


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'s' if n != 1 else ''}"


def _names(names) -> str:
    names = sorted(names)
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _flat_text(document_xml: bytes, original: bool) -> str:
    """Every visible character of the body, whitespace and structure ignored.

    For the original: as it reads with revisions accepted and hidden text gone.
    Written separately from the code that does the accepting, on purpose, so
    that the check is a second opinion rather than the first one repeated.
    """
    tree = etree.fromstring(document_xml)
    gone = {_w("del"), _w("moveFrom")}
    out: list[str] = []

    def dropped_row(node) -> bool:
        props = node.find(_w("trPr"))
        return props is not None and props.find(_w("del")) is not None

    def walk(node) -> None:
        for child in node:
            if not isinstance(child.tag, str):
                continue
            if original and (child.tag in gone or
                             (child.tag == _w("tr") and dropped_row(child))):
                continue
            if child.tag == _w("r") and original:
                props = child.find(_w("rPr"))
                if props is not None and _is_on(props.find(_w("vanish"))):
                    continue
            if child.tag == _w("t"):
                out.append(child.text or "")
            walk(child)

    walk(tree)
    return re.sub(r"\s+", "", "".join(out))


_FORBIDDEN = (b"<w:ins ", b"<w:del ", b"<w:moveFrom ", b"<w:moveTo ",
              b"<w:commentReference", b"<w:commentRangeStart")


def _check(cleaned: bytes, expected_text: str) -> None:
    from lra.pipeline import redline

    ok, reason = redline.verify(cleaned)
    if not ok:
        log.error("clean copy failed verification: %s", reason)
        raise CannotClean("I could not produce a clean copy I was sure would "
                          "open, so I have not sent one.")

    package = zipfile.ZipFile(BytesIO(cleaned))
    body = package.read("word/document.xml")
    stories = [package.read(n) for n in package.namelist() if _WORD_PART.match(n)]
    if any(marker in story for story in stories for marker in _FORBIDDEN):
        log.error("clean copy still carries revision or comment markup")
        raise CannotClean("I could not remove everything from that document, so "
                          "I have not sent a copy that only looks clean.")

    if _flat_text(body, original=False) != expected_text:
        log.error("clean copy text differs from the accepted original")
        raise CannotClean("Cleaning that document would have changed its words, "
                          "so I stopped. Nothing was altered.")
