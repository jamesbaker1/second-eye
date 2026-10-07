"""Validate a .docx against the rules Word actually enforces.

The honest gap in this project is that its output has never been opened in real
Microsoft Word. Structural checks are not the same thing as a human opening the
file, and that will remain outstanding until someone does it.

What this module does is close as much of that gap as can be closed offline: it
checks the specific constraints in the OOXML specification that revision markup
has to satisfy, which are the ones a tracked-changes writer is most likely to
violate. A file that fails any of these will either refuse to open or will lose
content silently, which is worse.

Sources for the rules below: ECMA-376 Part 1, sections 17.13.5.14 (w:del),
17.13.5.18 (w:ins) and 17.3.3.32 (w:delText).
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from io import BytesIO

from lxml import etree

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

REQUIRED_PARTS = {"[Content_Types].xml", "word/document.xml"}

# The parts that hold a story: text Word displays, and therefore text that can
# carry revision markup. The writer edits headers and footers as readily as the
# body, so checking only word/document.xml meant a malformed header revision was
# never seen by the gate that exists to stop us sending one.
STORY_PART = re.compile(
    r"^word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml$"
)

# Elements that may legally appear as a child of w:p alongside runs.
_PARAGRAPH_CHILDREN = {
    f"{W}pPr", f"{W}r", f"{W}ins", f"{W}del", f"{W}hyperlink", f"{W}bookmarkStart",
    f"{W}bookmarkEnd", f"{W}proofErr", f"{W}commentRangeStart", f"{W}commentRangeEnd",
    f"{W}smartTag", f"{W}fldSimple", f"{W}sdt", f"{W}moveFrom", f"{W}moveTo",
    f"{W}moveFromRangeStart", f"{W}moveFromRangeEnd", f"{W}moveToRangeStart",
    f"{W}moveToRangeEnd", f"{W}customXml", f"{W}subDoc", f"{W}permStart",
    f"{W}permEnd", f"{W}oMath", f"{W}oMathPara",
}

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?$")


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    revisions: int = 0
    authors: set[str] = field(default_factory=set)

    @property
    def ok(self) -> bool:
        return not self.errors

    def __str__(self) -> str:
        if self.ok and not self.warnings:
            return f"valid ({self.revisions} revisions by {', '.join(sorted(self.authors)) or 'nobody'})"
        parts = []
        if self.errors:
            parts.append(f"{len(self.errors)} error(s): " + "; ".join(self.errors[:4]))
        if self.warnings:
            parts.append(f"{len(self.warnings)} warning(s): " + "; ".join(self.warnings[:3]))
        return " | ".join(parts)


def validate(content: bytes) -> Report:
    """Check a document against the constraints Word enforces on revisions."""
    report = Report()

    try:
        package = zipfile.ZipFile(BytesIO(content))
    except Exception as e:  # noqa: BLE001
        report.errors.append(f"not a readable zip package: {e}")
        return report

    corrupt = package.testzip()
    if corrupt is not None:
        report.errors.append(f"corrupt entry in package: {corrupt}")
        return report

    names = set(package.namelist())
    for part in REQUIRED_PARTS - names:
        report.errors.append(f"missing required part: {part}")
    if report.errors:
        return report

    # One id map across every part: Word wants revision ids distinct throughout
    # the document, and the writer now allocates them from a high-water mark
    # taken across all of them.
    seen_ids: dict[str, str] = {}
    for name in sorted(n for n in names if STORY_PART.match(n)):
        try:
            tree = etree.fromstring(package.read(name))
        except etree.XMLSyntaxError as e:
            report.errors.append(f"{_label(name)} is not well-formed: {e}")
            continue

        _check_revisions(tree, report, seen_ids, name)
        _check_paragraph_children(tree, report)
        _check_relationships(package, names, tree, report, name)
    _check_comments(package, names, report)
    return report


# --------------------------------------------------------------------------
# Comments and their threads
# --------------------------------------------------------------------------

W14 = "{http://schemas.microsoft.com/office/word/2010/wordml}"
W15 = "{http://schemas.microsoft.com/office/word/2012/wordml}"
W16CID = "{http://schemas.microsoft.com/office/word/2016/wordml/cid}"
_COMMENT_PARTS = {
    "word/comments.xml": "comments+xml",
    "word/commentsExtended.xml": "commentsExtended+xml",
    "word/commentsIds.xml": "commentsIds+xml",
    "word/people.xml": "people+xml",
}


def _check_comments(package, names, report: Report) -> None:
    """A comment is spread over several parts, and a reply or a "Resolved"
    only shows in Word when they agree (pipeline/wordcomments.py). What is
    checked, and why each is an error rather than a warning:

    - every comment marker in the body names a comment that exists: a
      dangling commentReference is a file Word says it has to repair;
    - comment ids are unique;
    - each commentEx and commentId names a comment paragraph by paraId,
      and a reply's paraIdParent names a comment that is not itself a reply:
      Word threads are one level deep, and a thread it cannot resolve is
      shown flat, or not at all;
    - each comments part present has a content type and a relationship.
    """
    if "word/comments.xml" not in names:
        return
    try:
        comments = etree.fromstring(package.read("word/comments.xml"))
    except etree.XMLSyntaxError as e:
        report.errors.append(f"comments.xml is not well-formed: {e}")
        return

    ids: set[str] = set()
    para_ids: set[str] = set()
    for comment in comments.iter(f"{W}comment"):
        cid = comment.get(f"{W}id")
        if cid in ids:
            report.errors.append(f"duplicate comment id {cid}")
        ids.add(cid)
        for p in comment.iter(f"{W}p"):
            pid = p.get(f"{W14}paraId")
            if pid:
                if pid in para_ids:
                    report.errors.append(f"comment paragraph id {pid} is used twice")
                para_ids.add(pid)

    try:
        body = etree.fromstring(package.read("word/document.xml"))
    except etree.XMLSyntaxError:
        return      # already reported by the story checks
    for tag in ("commentRangeStart", "commentRangeEnd", "commentReference"):
        for marker in body.iter(f"{W}{tag}"):
            if marker.get(f"{W}id") not in ids:
                report.errors.append(
                    f"w:{tag} names comment {marker.get(f'{W}id')}, which does not exist")
    referenced = {m.get(f"{W}id") for m in body.iter(f"{W}commentReference")}
    for cid in sorted(ids - referenced):
        report.warnings.append(f"comment {cid} is not referenced from the document")

    if "word/commentsExtended.xml" in names:
        try:
            extended = etree.fromstring(package.read("word/commentsExtended.xml"))
        except etree.XMLSyntaxError as e:
            report.errors.append(f"commentsExtended.xml is not well-formed: {e}")
            extended = None
        if extended is not None:
            parent_of = {}
            for ex in extended.iter(f"{W15}commentEx"):
                pid = ex.get(f"{W15}paraId")
                if pid not in para_ids:
                    report.errors.append(
                        f"commentsExtended names paragraph {pid}, which no comment has")
                if ex.get(f"{W15}done", "0") not in ("0", "1", "true", "false"):
                    report.errors.append(f"comment {pid} has done={ex.get(f'{W15}done')!r}")
                parent_of[pid] = ex.get(f"{W15}paraIdParent")
            for pid, parent in parent_of.items():
                if parent is None:
                    continue
                if parent not in parent_of:
                    report.errors.append(f"reply {pid} names a parent {parent} that "
                                         "is not a comment")
                elif parent_of[parent]:
                    report.errors.append(f"reply {pid} replies to a reply ({parent}); "
                                         "Word threads are one level deep")

    if "word/commentsIds.xml" in names:
        try:
            cids = etree.fromstring(package.read("word/commentsIds.xml"))
        except etree.XMLSyntaxError as e:
            report.errors.append(f"commentsIds.xml is not well-formed: {e}")
            cids = None
        if cids is not None:
            durable: set[str] = set()
            for entry in cids.iter(f"{W16CID}commentId"):
                if entry.get(f"{W16CID}paraId") not in para_ids:
                    report.errors.append("commentsIds names paragraph "
                                         f"{entry.get(f'{W16CID}paraId')}, which no "
                                         "comment has")
                did = entry.get(f"{W16CID}durableId")
                if did in durable:
                    report.errors.append(f"durable comment id {did} is used twice")
                durable.add(did)

    types = package.read("[Content_Types].xml").decode("utf-8", errors="replace")
    rels = (package.read("word/_rels/document.xml.rels").decode("utf-8", errors="replace")
            if "word/_rels/document.xml.rels" in names else "")
    for part, content_type in _COMMENT_PARTS.items():
        if part not in names:
            continue
        if f'PartName="/{part}"' not in types or content_type not in types:
            report.errors.append(f"{_label(part)} has no content type")
        if f'Target="{_label(part)}"' not in rels and f'Target="/{part}"' not in rels:
            report.errors.append(f"{_label(part)} is not related to the document")


def _label(part_name: str) -> str:
    return part_name.removeprefix("word/")


def _check_revisions(tree, report: Report, seen_ids: dict[str, str],
                     part_name: str = "word/document.xml") -> None:
    where = _label(part_name)

    for element in tree.iter():
        tag = element.tag
        if tag not in (f"{W}ins", f"{W}del", f"{W}moveFrom", f"{W}moveTo"):
            continue

        report.revisions += 1
        kind = tag.replace(W, "w:")

        # Every revision needs an id, an author and a date.
        rev_id = element.get(f"{W}id")
        author = element.get(f"{W}author")
        date = element.get(f"{W}date")

        if rev_id is None:
            report.errors.append(f"{kind} without w:id")
        elif rev_id in seen_ids:
            previous = seen_ids[rev_id]
            if previous.endswith(f" in {where}"):
                report.errors.append(
                    f"duplicate revision id {rev_id} on {kind} (also on {previous})"
                )
            else:
                # Across parts this is a warning, not an error. Word itself
                # sometimes restarts ids in a header, and refusing the document
                # would degrade a whole review to a memo over something the
                # lawyer would never notice. Our own output does not do it.
                report.warnings.append(
                    f"revision id {rev_id} on {kind} in {where} is also used on "
                    f"{previous}"
                )
        else:
            seen_ids[rev_id] = f"{kind} in {where}"

        if author is None:
            report.errors.append(f"{kind} id={rev_id} has no w:author")
        else:
            report.authors.add(author)

        if date is None:
            report.warnings.append(f"{kind} id={rev_id} has no w:date")
        elif not _ISO_DATE.match(date):
            report.errors.append(f"{kind} id={rev_id} has a malformed w:date: {date!r}")

        # The rule that matters most: text inside a deletion must be w:delText,
        # and text inside an insertion must be w:t. Getting this wrong makes
        # Word either refuse the file or silently drop the text.
        if tag in (f"{W}del", f"{W}moveFrom"):
            for run in element.iter(f"{W}r"):
                if _inside_nested_insert(run, element):
                    continue
                if run.find(f"{W}t") is not None:
                    report.errors.append(
                        f"{kind} id={rev_id} contains a run with w:t; deleted text "
                        "must use w:delText"
                    )
        if tag in (f"{W}ins", f"{W}moveTo"):
            for run in element.findall(f"{W}r"):
                if run.find(f"{W}delText") is not None:
                    report.errors.append(
                        f"{kind} id={rev_id} contains w:delText outside a w:del"
                    )

    # Deleted text that is empty means the original content was lost, not marked
    # as deleted. That is silent data loss and the worst failure mode here.
    for node in tree.iter(f"{W}delText"):
        if not (node.text or ""):
            report.warnings.append("empty w:delText: deleted text may have been lost")


def _inside_nested_insert(run, ancestor) -> bool:
    """A run inside a w:ins nested within this w:del follows the insert rules."""
    parent = run.getparent()
    while parent is not None and parent is not ancestor:
        if parent.tag == f"{W}ins":
            return True
        parent = parent.getparent()
    return False


def _check_paragraph_children(tree, report: Report) -> None:
    for paragraph in tree.iter(f"{W}p"):
        for child in paragraph:
            if not isinstance(child.tag, str):
                continue                      # comments and processing instructions
            if child.tag not in _PARAGRAPH_CHILDREN:
                report.warnings.append(
                    f"unexpected child of w:p: {child.tag.replace(W, 'w:')}"
                )


def _check_relationships(package, names, tree, report: Report,
                         part_name: str = "word/document.xml") -> None:
    """Every r:id referenced in a part must exist in that part's relationships.

    A dangling relationship is how a hyperlink or an image becomes a file Word
    will not open. Relationships are per-part, so a header's r:ids are declared
    in word/_rels/header1.xml.rels and nowhere else.
    """
    rels_part = f"word/_rels/{_label(part_name)}.rels"
    if rels_part not in names:
        if any(el.get(f"{R}id") for el in tree.iter()):
            report.errors.append(
                f"{_label(part_name)} references relationships but has no rels part"
            )
        return

    try:
        rels = etree.fromstring(package.read(rels_part))
    except etree.XMLSyntaxError as e:
        report.errors.append(f"relationship part is not well-formed: {e}")
        return

    declared = {r.get("Id") for r in rels}
    for element in tree.iter():
        ref = element.get(f"{R}id") or element.get(f"{R}embed")
        if ref and ref not in declared:
            report.errors.append(
                f"{element.tag.replace(W, 'w:')} references relationship {ref}, "
                "which is not declared"
            )
