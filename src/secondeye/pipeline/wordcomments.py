"""Word comments as Word itself threads them: read, reply, resolve.

A comment in a .docx is spread over up to five parts, and a reply or a
"Resolved" that Word will show needs all of them to agree:

    word/comments.xml           the comment: id, author, date, initials, text.
                                Its last paragraph carries a w14:paraId, the
                                key every other part below uses.
    word/commentsExtended.xml   w15:commentEx per comment: w15:paraIdParent
                                makes it a reply in its parent's thread, and
                                w15:done="1" is Word's "Resolved".
    word/commentsIds.xml        w16cid:commentId: a durable id per paraId,
                                which Word 2019+ uses to keep a thread's
                                identity across saves.
    word/commentsExtensible.xml w16cex: the UTC date. Only kept up to date when
                                the file already has one; Word adds it itself.
    word/people.xml             w15:person per author, so the reply is shown
                                under the author's name rather than a guest.

The document body anchors each comment with commentRangeStart,
commentRangeEnd and a run holding commentReference. Word writes a reply's
markers beside its parent's, so this does the same.

python-docx writes comments.xml and nothing else, which is enough for a
single comment and not for a thread; so this module works on the package
itself, part by part, and leaves every part it does not need untouched.

Two rules. **Never delete a comment**: there is no function here that
removes one, and turning.examine() checks that every comment still exists.
**Never guess an id**: a reply to a comment that is not there raises.
"""

from __future__ import annotations

import posixpath
import random
import re
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BytesIO

from lxml import etree

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W14_NS = "http://schemas.microsoft.com/office/word/2010/wordml"
W15_NS = "http://schemas.microsoft.com/office/word/2012/wordml"
W16CID_NS = "http://schemas.microsoft.com/office/word/2016/wordml/cid"
W16CEX_NS = "http://schemas.microsoft.com/office/word/2018/wordml/cex"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"

W = f"{{{W_NS}}}"
W14 = f"{{{W14_NS}}}"
W15 = f"{{{W15_NS}}}"
W16CID = f"{{{W16CID_NS}}}"
W16CEX = f"{{{W16CEX_NS}}}"
MC_IGNORABLE = f"{{{MC_NS}}}Ignorable"

_OFFICE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_CT_PREFIX = "application/vnd.openxmlformats-officedocument.wordprocessingml."

# kind -> (relationship type, content type, default part name, root tag, prefix, ns)
PARTS = {
    "comments": (f"{_OFFICE_REL}/comments", _CT_PREFIX + "comments+xml",
                 "word/comments.xml", W + "comments", "w", W_NS),
    "extended": ("http://schemas.microsoft.com/office/2011/relationships/commentsExtended",
                 _CT_PREFIX + "commentsExtended+xml", "word/commentsExtended.xml",
                 W15 + "commentsEx", "w15", W15_NS),
    "ids": ("http://schemas.microsoft.com/office/2016/09/relationships/commentsIds",
            _CT_PREFIX + "commentsIds+xml", "word/commentsIds.xml",
            W16CID + "commentsIds", "w16cid", W16CID_NS),
    "extensible": ("http://schemas.microsoft.com/office/2018/08/relationships/commentsExtensible",
                   _CT_PREFIX + "commentsExtensible+xml",
                   "word/commentsExtensible.xml", W16CEX + "commentsExtensible",
                   "w16cex", W16CEX_NS),
    "people": ("http://schemas.microsoft.com/office/2011/relationships/people",
               _CT_PREFIX + "people+xml", "word/people.xml", W15 + "people", "w15", W15_NS),
}

DOCUMENT = "word/document.xml"


class CommentError(LookupError):
    """A comment that is not there, or a package with no comments at all.
    The message is written to be read by the model that asked."""


# --------------------------------------------------------------------------
# The package: every part as bytes, the ones we touch as trees
# --------------------------------------------------------------------------


class Package:
    """A .docx opened for surgery on a few parts, preserving all the rest
    byte for byte, in their original order and compression."""

    def __init__(self, content: bytes) -> None:
        try:
            archive = zipfile.ZipFile(BytesIO(content))
        except zipfile.BadZipFile as e:
            raise CommentError("that is not a Word document (not a zip package)") from e
        self.infos = archive.infolist()
        self.blobs = {i.filename: archive.read(i.filename) for i in self.infos}
        self.trees: dict[str, etree._Element] = {}

    def has(self, name: str) -> bool:
        return name in self.blobs

    def xml(self, name: str) -> etree._Element:
        if name not in self.trees:
            self.trees[name] = etree.fromstring(self.blobs[name])
        return self.trees[name]

    def replace_root(self, name: str, root: etree._Element) -> None:
        self.trees[name] = root

    def save(self) -> bytes:
        for name, tree in self.trees.items():
            self.blobs[name] = etree.tostring(tree, xml_declaration=True,
                                              encoding="UTF-8", standalone=True)
        out = BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            written = set()
            for info in self.infos:
                clone = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                clone.compress_type = info.compress_type
                clone.external_attr = info.external_attr
                z.writestr(clone, self.blobs[info.filename])
                written.add(info.filename)
            for name, blob in self.blobs.items():
                if name not in written:
                    z.writestr(name, blob)
        return out.getvalue()

    # -- relationships and content types ------------------------------------

    def _rels_name(self, part: str = DOCUMENT) -> str:
        folder, base = posixpath.split(part)
        return f"{folder}/_rels/{base}.rels"

    def part_for(self, kind: str) -> str | None:
        """The part a relationship of this kind from the body points at."""
        rel_type = PARTS[kind][0]
        rels = self._rels_name()
        if not self.has(rels):
            return None
        for rel in self.xml(rels):
            if rel.get("Type") == rel_type and rel.get("TargetMode") != "External":
                target = rel.get("Target", "")
                name = (target.lstrip("/") if target.startswith("/")
                        else posixpath.normpath(posixpath.join("word", target)))
                return name if self.has(name) else None
        return None

    def ensure_part(self, kind: str) -> str:
        """The part of this kind, created empty and wired in if missing."""
        existing = self.part_for(kind)
        if existing:
            return existing
        rel_type, content_type, name, root_tag, prefix, ns = PARTS[kind]
        while self.has(name):   # a stray part of that name nobody points at
            stem, ext = name.rsplit(".", 1)
            name = f"{stem}1.{ext}"
        root = etree.Element(root_tag, nsmap={prefix: ns, "mc": MC_NS})
        root.set(MC_IGNORABLE, prefix)
        self.blobs[name] = b""
        self.trees[name] = root

        rels_name = self._rels_name()
        if not self.has(rels_name):
            self.blobs[rels_name] = b""
            self.trees[rels_name] = etree.Element(f"{{{REL_NS}}}Relationships",
                                                  nsmap={None: REL_NS})
        rels = self.xml(rels_name)
        taken = {r.get("Id") for r in rels}
        n = 1
        while f"rId{n}" in taken:
            n += 1
        rel = etree.SubElement(rels, f"{{{REL_NS}}}Relationship")
        rel.set("Id", f"rId{n}")
        rel.set("Type", rel_type)
        rel.set("Target", posixpath.relpath(name, "word"))

        types = self.xml("[Content_Types].xml")
        override = etree.SubElement(types, f"{{{CT_NS}}}Override")
        override.set("PartName", "/" + name)
        override.set("ContentType", content_type)
        return name


def _with_namespaces(pkg: Package, name: str, wanted: dict[str, str]) -> etree._Element:
    """The part's root, redeclared with `wanted` prefixes and each of them
    listed in mc:Ignorable, as Word writes it. lxml cannot add a namespace
    declaration to an existing element, so the root is rebuilt around its
    children when one is missing."""
    root = pkg.xml(name)
    missing = {p: ns for p, ns in {**wanted, "mc": MC_NS}.items()
               if root.nsmap.get(p) != ns}
    if missing:
        nsmap = dict(root.nsmap)
        nsmap.update(missing)
        rebuilt = etree.Element(root.tag, nsmap=nsmap)
        for key, value in root.attrib.items():
            rebuilt.set(key, value)
        for child in list(root):
            rebuilt.append(child)
        root = rebuilt
        pkg.replace_root(name, root)
    ignorable = (root.get(MC_IGNORABLE) or "").split()
    for prefix in wanted:
        if prefix not in ignorable:
            ignorable.append(prefix)
    root.set(MC_IGNORABLE, " ".join(ignorable))
    return root


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


@dataclass
class Comment:
    id: str
    author: str
    initials: str
    date: str
    text: str
    anchored: str = ""
    clause: str = ""
    # The id of the comment this one replies to, or "" for a thread's first.
    parent: str = ""
    replies: list[str] = field(default_factory=list)
    # Word's "Resolved". A reply carries its thread's state.
    done: bool = False
    para_id: str = ""

    def as_dict(self) -> dict:
        return {"id": self.id, "author": self.author, "date": self.date,
                "text": self.text, "anchored": self.anchored, "clause": self.clause,
                "parent": self.parent or None, "replies": self.replies, "done": self.done}


def _text(element) -> str:
    """What a comment says, a paragraph per line."""
    lines = []
    for p in element.iter(W + "p"):
        parts = []
        for node in p.iter(W + "t", W + "tab", W + "br"):
            parts.append(node.text or "" if node.tag == W + "t" else
                         "\t" if node.tag == W + "tab" else "\n")
        lines.append("".join(parts))
    return "\n".join(lines).strip()


def _last_para_id(comment) -> str:
    paras = comment.findall(W + "p")
    return (paras[-1].get(W14 + "paraId") or "") if paras else ""


def _extended(pkg: Package) -> dict[str, tuple[str, bool]]:
    """paraId -> (parent paraId, done)."""
    name = pkg.part_for("extended")
    if not name:
        return {}
    out = {}
    for ex in pkg.xml(name).iter(W15 + "commentEx"):
        out[ex.get(W15 + "paraId", "")] = (ex.get(W15 + "paraIdParent", "") or "",
                                           ex.get(W15 + "done", "0") in ("1", "true"))
    return out


def read(content: bytes) -> list[Comment]:
    """Every comment in the document, in the order comments.xml holds them,
    with the text it is anchored to, the clause that text is in, its thread
    and whether it is resolved."""
    pkg = Package(content)
    name = pkg.part_for("comments")
    if not name:
        return []
    comments: list[Comment] = []
    for element in pkg.xml(name).iter(W + "comment"):
        comments.append(Comment(
            id=element.get(W + "id", ""), author=element.get(W + "author", "") or "",
            initials=element.get(W + "initials", "") or "",
            date=element.get(W + "date", "") or "", text=_text(element),
            para_id=_last_para_id(element),
        ))

    extended = _extended(pkg)
    by_para = {c.para_id: c for c in comments if c.para_id}
    for c in comments:
        parent_para, done = extended.get(c.para_id, ("", False))
        c.done = done
        parent = by_para.get(parent_para)
        if parent is not None and parent is not c:
            c.parent = parent.id
            parent.replies.append(c.id)
    for c in comments:
        # Word resolves a thread by marking its first comment; a reply reads
        # as resolved when its thread is.
        root = _root_of(c, {x.id: x for x in comments})
        c.done = c.done or root.done

    anchors = _anchors(pkg, content)
    for c in comments:
        c.anchored, c.clause = anchors.get(c.id, ("", ""))
        if c.parent and not c.anchored:
            parent = next(x for x in comments if x.id == c.parent)
            c.anchored, c.clause = parent.anchored, parent.clause
    return comments


def _root_of(c: Comment, by_id: dict[str, Comment]) -> Comment:
    seen = set()
    while c.parent and c.parent in by_id and c.id not in seen:
        seen.add(c.id)
        c = by_id[c.parent]
    return c


def _anchors(pkg: Package, content: bytes) -> dict[str, tuple[str, str]]:
    """comment id -> (the visible text between its range markers, the clause
    that text starts in). Deleted text is not part of what a comment is
    about as the reader sees it, so it is left out."""
    body = pkg.xml(DOCUMENT)
    labels = clause_labels(content, body)
    open_ranges: dict[str, list[str]] = {}
    text: dict[str, list[str]] = {}
    clause: dict[str, str] = {}
    paragraph_index = -1
    stack: list[int] = []

    for event, node in etree.iterwalk(body, events=("start", "end")):
        tag = node.tag
        if not isinstance(tag, str):
            continue
        if event == "start":
            if tag == W + "p":
                paragraph_index += 1
                stack.append(paragraph_index)
            elif tag == W + "commentRangeStart":
                cid = node.get(W + "id", "")
                open_ranges[cid] = text.setdefault(cid, [])
                # A range opened between paragraphs belongs to the next one.
                following = paragraph_index + 1
                clause[cid] = (labels[stack[-1]] if stack else
                               labels[following] if following < len(labels) else "")
            elif tag == W + "commentRangeEnd":
                open_ranges.pop(node.get(W + "id", ""), None)
            elif tag == W + "commentReference":
                cid = node.get(W + "id", "")
                if cid not in clause and stack:
                    clause[cid] = labels[stack[-1]]
                    text.setdefault(cid, [])
            elif tag == W + "t" and open_ranges and not _deleted(node):
                for buffer in open_ranges.values():
                    buffer.append(node.text or "")
            elif tag == W + "tab" and open_ranges and node.getparent().tag == W + "r":
                for buffer in open_ranges.values():
                    buffer.append("\t")
        elif tag == W + "p":
            for buffer in open_ranges.values():
                buffer.append("\n")
            stack.pop()
    return {cid: (re.sub(r"[ \t]+", " ", "".join(parts)).strip(), clause.get(cid, ""))
            for cid, parts in text.items()}


def _deleted(node) -> bool:
    parent = node.getparent()
    while parent is not None:
        if parent.tag in (W + "del", W + "moveFrom"):
            return True
        parent = parent.getparent()
    return False


_TYPED_NUMBER = re.compile(
    r"^\s*(?:(?:clause|section|article|paragraph)\s+)?"
    r"(\d+(?:\.\d+)*|\([a-z0-9]{1,4}\)|[A-Z]\.|schedule\s+\d+)[.)]?(?=\s)",
    re.IGNORECASE)


def clause_labels(content: bytes, body=None) -> list[str]:
    """For each paragraph in the body, in document order, the clause it
    belongs to: its own number as Word draws it, or a number typed at its
    start, or else the nearest numbered paragraph before it. "" before the
    first number.

    The drawn number comes from extract._ListNumbers, the same code the
    review reads clause numbers with, so "cl. 9.4" here is the 9.4 the
    lawyer sees and the review talks about.
    """
    from docx import Document

    from secondeye.pipeline.extract import _ListNumbers, visible_text

    doc = Document(BytesIO(content))
    numbers = _ListNumbers(doc)
    labels: list[str] = []
    last = ""
    for p in doc.element.body.iter(W + "p"):
        drawn, _ = numbers.label(p)
        drawn = drawn.strip().rstrip(".") if drawn else ""
        if not drawn:
            typed = _TYPED_NUMBER.match(visible_text(p))
            if typed:
                drawn = typed.group(1).rstrip(".")
        if drawn:
            # A sub-paragraph "(a)" under 9.4 reads as 9.4(a).
            if drawn.startswith("(") and last and not last.startswith("("):
                base = re.sub(r"\(.*$", "", last)
                drawn = f"{base}{drawn}"
            last = drawn
        labels.append(last)
    if body is not None:
        count = sum(1 for _ in body.iter(W + "p"))
        labels += [last] * (count - len(labels))
    return labels


# --------------------------------------------------------------------------
# Writing: replies and resolved
# --------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hex_ids(pkg: Package) -> set[str]:
    """Every paraId, textId and durableId anywhere in the package: a new one
    must collide with none of them."""
    taken: set[str] = set()
    for name, blob in pkg.blobs.items():
        if not name.endswith(".xml"):
            continue
        source = etree.tostring(pkg.trees[name]) if name in pkg.trees else blob
        taken.update(m.decode().upper() for m in re.findall(
            rb'(?:paraId|textId|durableId)="([0-9A-Fa-f]{1,8})"', source))
    return taken


def _fresh_hex(taken: set[str], rng: random.Random) -> str:
    """Eight hex digits below 0x80000000, as Word requires of paraId and
    durableId, and not already in use."""
    while True:
        value = f"{rng.randrange(1, 0x7FFFFFFF):08X}"
        if value not in taken:
            taken.add(value)
            return value


class _Writer:
    def __init__(self, content: bytes) -> None:
        self.pkg = Package(content)
        self.comments_part = self.pkg.part_for("comments")
        if not self.comments_part:
            raise CommentError("the document has no comments")
        self.root = _with_namespaces(self.pkg, self.comments_part, {"w14": W14_NS})
        self.taken = _hex_ids(self.pkg)
        self.rng = random.Random()

    def comment(self, cid: str):
        for element in self.root.iter(W + "comment"):
            if element.get(W + "id") == str(cid):
                return element
        known = [e.get(W + "id") for e in self.root.iter(W + "comment")]
        raise CommentError(f"there is no comment with id {cid}; the ids are "
                           + (", ".join(known) or "none"))

    def para_id(self, comment) -> str:
        """The comment's key in the other parts, given one if it has none."""
        paras = comment.findall(W + "p")
        if not paras:
            paras = [etree.SubElement(comment, W + "p")]
        last = paras[-1]
        if not last.get(W14 + "paraId"):
            last.set(W14 + "paraId", _fresh_hex(self.taken, self.rng))
            last.set(W14 + "textId", "77777777")
        return last.get(W14 + "paraId")

    def extended(self) -> etree._Element:
        name = self.pkg.ensure_part("extended")
        return _with_namespaces(self.pkg, name, {"w15": W15_NS})

    def entry(self, para_id: str) -> etree._Element:
        """The commentEx for a paraId, created (open, no parent) if missing."""
        root = self.extended()
        for ex in root.iter(W15 + "commentEx"):
            if ex.get(W15 + "paraId") == para_id:
                return ex
        ex = etree.SubElement(root, W15 + "commentEx")
        ex.set(W15 + "paraId", para_id)
        ex.set(W15 + "done", "0")
        return ex

    def thread_root(self, comment):
        """The first comment of the thread this one is in. Word threads are
        one level deep: every reply points at the first comment."""
        extended = _extended(self.pkg)
        by_para = {_last_para_id(c): c for c in self.root.iter(W + "comment")
                   if _last_para_id(c)}
        seen = set()
        while True:
            own = _last_para_id(comment)
            parent_para = extended.get(own, ("", False))[0] if own else ""
            parent = by_para.get(parent_para) if parent_para else None
            if parent is None or parent is comment or parent_para in seen:
                return comment
            seen.add(parent_para)
            comment = parent

    def durable(self, para_id: str) -> str | None:
        """Give the paraId a durable id in commentsIds.xml, if missing."""
        name = self.pkg.ensure_part("ids")
        root = _with_namespaces(self.pkg, name, {"w16cid": W16CID_NS})
        for entry in root.iter(W16CID + "commentId"):
            if entry.get(W16CID + "paraId") == para_id:
                return entry.get(W16CID + "durableId")
        entry = etree.SubElement(root, W16CID + "commentId")
        entry.set(W16CID + "paraId", para_id)
        durable = _fresh_hex(self.taken, self.rng)
        entry.set(W16CID + "durableId", durable)
        return durable

    def person(self, author: str) -> None:
        name = self.pkg.ensure_part("people")
        root = _with_namespaces(self.pkg, name, {"w15": W15_NS})
        if any(p.get(W15 + "author") == author for p in root.iter(W15 + "person")):
            return
        person = etree.SubElement(root, W15 + "person")
        person.set(W15 + "author", author)
        presence = etree.SubElement(person, W15 + "presenceInfo")
        presence.set(W15 + "providerId", "None")
        presence.set(W15 + "userId", author)

    def save(self) -> bytes:
        return self.pkg.save()


def reply(content: bytes, comment_id: str, text: str, author: str,
          initials: str | None = None, resolve: bool | None = None,
          date: str | None = None) -> tuple[bytes, str]:
    """Add a reply to a comment's thread. Returns (document, the reply's id).

    A reply to a reply joins the same thread, under its first comment, as
    Word does. `resolve` True or False sets the thread's state at the same
    time; None leaves it as it was.
    """
    from secondeye.pipeline.ooxml import author_initials

    text = (text or "").strip()
    if not text:
        raise CommentError("a reply needs some text")
    w = _Writer(content)
    target = w.comment(comment_id)
    root_comment = w.thread_root(target)
    root_para = w.para_id(root_comment)
    root_entry = w.entry(root_para)

    # Above every w:id in the comments and the body, revision ids included:
    # the two are separate in the schema, but a reader that confuses them
    # should never find two things with one id.
    ids = [int(v) for v in re.findall(r'\bw:id="(-?\d+)"', etree.tostring(
        w.pkg.xml(DOCUMENT), encoding="unicode") + etree.tostring(w.root, encoding="unicode"))]
    new_id = str(max(ids, default=-1) + 1)
    when = date or _now()

    comment = etree.SubElement(w.root, W + "comment")
    comment.set(W + "id", new_id)
    comment.set(W + "author", author)
    comment.set(W + "date", when)
    comment.set(W + "initials", author_initials(author) if initials is None else initials)
    lines = [line for line in text.splitlines()] or [text]
    for i, line in enumerate(lines):
        p = etree.SubElement(comment, W + "p")
        p.set(W14 + "paraId", _fresh_hex(w.taken, w.rng))
        p.set(W14 + "textId", "77777777")
        props = etree.SubElement(p, W + "pPr")
        etree.SubElement(props, W + "pStyle").set(W + "val", "CommentText")
        if i == 0:
            mark = etree.SubElement(p, W + "r")
            etree.SubElement(etree.SubElement(mark, W + "rPr"),
                             W + "rStyle").set(W + "val", "CommentReference")
            etree.SubElement(mark, W + "annotationRef")
        run = etree.SubElement(p, W + "r")
        t = etree.SubElement(run, W + "t")
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        t.text = line
    reply_para = _last_para_id(comment)

    ex = w.entry(reply_para)
    ex.set(W15 + "paraIdParent", root_para)
    done = root_entry.get(W15 + "done", "0") if resolve is None else ("1" if resolve else "0")
    w.durable(root_para)
    reply_durable = w.durable(reply_para)
    w.person(author)
    _extensible(w, reply_durable, when)

    _anchor_beside(w.pkg, root_comment.get(W + "id"), new_id)
    _set_done(w, root_comment, done)
    return w.save(), new_id


def resolve(content: bytes, comment_id: str, done: bool = True) -> bytes:
    """Mark a comment's thread resolved (Word's "Resolve"), or open again."""
    w = _Writer(content)
    target = w.comment(comment_id)
    _set_done(w, w.thread_root(target), "1" if done else "0")
    return w.save()


def _set_done(w: _Writer, root_comment, done: str) -> None:
    """Resolved is set on the thread's first comment and every reply in it,
    so a reader that looks only at the comment it was given agrees."""
    root_para = w.para_id(root_comment)
    w.entry(root_para).set(W15 + "done", done)
    for ex in w.extended().iter(W15 + "commentEx"):
        if ex.get(W15 + "paraIdParent") == root_para:
            ex.set(W15 + "done", done)


def _extensible(w: _Writer, durable: str | None, when: str) -> None:
    name = w.pkg.part_for("extensible")
    if not name or not durable:
        return
    root = _with_namespaces(w.pkg, name, {"w16cex": W16CEX_NS})
    entry = etree.SubElement(root, W16CEX + "commentExtensible")
    entry.set(W16CEX + "durableId", durable)
    entry.set(W16CEX + "dateUtc", when)


def _anchor_beside(pkg: Package, parent_id: str, new_id: str) -> None:
    """The reply's range and reference, next to its parent's, as Word writes
    them. A parent with no range (a point comment) gets a reference only."""
    body = pkg.xml(DOCUMENT)
    start = end = reference_run = None
    for node in body.iter(W + "commentRangeStart", W + "commentRangeEnd",
                          W + "commentReference"):
        if node.get(W + "id") != parent_id:
            continue
        if node.tag == W + "commentRangeStart":
            start = node
        elif node.tag == W + "commentRangeEnd":
            end = node
        else:
            reference_run = node.getparent()

    if start is not None and end is not None:
        new_start = etree.Element(W + "commentRangeStart")
        new_start.set(W + "id", new_id)
        start.addnext(new_start)
        new_end = etree.Element(W + "commentRangeEnd")
        new_end.set(W + "id", new_id)
        end.addnext(new_end)

    run = etree.Element(W + "r")
    etree.SubElement(etree.SubElement(run, W + "rPr"),
                     W + "rStyle").set(W + "val", "CommentReference")
    etree.SubElement(run, W + "commentReference").set(W + "id", new_id)
    if reference_run is not None and reference_run.tag == W + "r":
        reference_run.addnext(run)
    elif end is not None:
        after = end.getnext()
        (after if after is not None and after.tag == W + "commentRangeEnd" else end).addnext(run)
    else:
        raise CommentError(f"comment {parent_id} is not anchored anywhere in the body")


__all__ = ["Comment", "CommentError", "Package", "clause_labels", "read", "reply", "resolve"]
