"""OOXML revision markup: the mechanics of a real Word tracked change.

This is the load-bearing file in the repository. Everything else is plumbing
that has been built before. If this is wrong, the product is worth nothing,
because a redline a lawyer has to re-key is worse than no redline at all.

A tracked change in Word is not an overlay. It is markup inside
`word/document.xml`:

    deleted text   <w:del w:id w:author w:date><w:r><w:delText>old</w:delText>
                   </w:r></w:del>
    inserted text  <w:ins w:id w:author w:date><w:r><w:t>new</w:t></w:r></w:ins>

The failure mode that ruins naive implementations is run splitting. Word breaks
a sentence into arbitrary runs based on formatting, spell-check state and
editing history, so the phrase you want to replace routinely spans three runs
with a bold word in the middle. Editing text without splitting and reassembling
runs correctly produces a file that loses formatting or does not open.

Everything here works at the lxml level for that reason. python-docx's `.text`
setter collapses a paragraph's runs into one and discards their formatting,
which is exactly the corruption we are avoiding.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph
from docx.text.run import Run
from lxml import etree

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"


class AnchorNotFound(LookupError):
    """The quoted text is not in the document. Never guess a nearby location."""


class AnchorAmbiguous(LookupError):
    """The quoted text appears more than once, so we cannot know which is meant."""


class AnchorSpansParagraphs(LookupError):
    """The quoted text crosses a paragraph boundary. Too risky to rewrite."""


class AnchorNotSafelyEditable(LookupError):
    """The span is located, but editing it would damage something.

    Raised when the span touches a run that also carries an image, a tab or a
    break; when it overlaps a hyperlink, a content control or the result of a
    field; or when its runs are not one unbroken stretch of siblings, which is
    what happens where the text runs into another author's tracked change.

    Splitting such a run duplicates the non-text content, pulling a hyperlink's
    run into a w:del detaches the link, rewriting a field result is undone by
    the next field update, and moving runs out from around something that sits
    between them reorders the paragraph. All four produce a document that looks
    fine in a diff and is wrong in Word, so the edit is refused and reported in
    the email instead. Carries a sentence redline.apply() shows the lawyer, so
    the message is written to be read by one.
    """


@dataclass
class Revision:
    """One tracked change, ready to be written."""

    anchor: str
    replacement: str | None  # None means pure deletion
    author: str
    comment: str = ""


class RevisionWriter:
    """Applies revisions to a document, allocating unique revision ids.

    Word requires every `w:id` in the document to be distinct. Reusing one makes
    the review pane behave unpredictably, which is the sort of bug that surfaces
    only in front of a client.
    """

    def __init__(self, document, author: str) -> None:
        self.doc = document
        self.author = author
        self.date = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._next_id = self._highest_existing_id() + 1
        # The last paragraph inserted after a given anchor, so a second
        # insertion after the same anchor lands after the first rather than
        # between it and the anchor. Keyed by anchor text, which _locate has
        # already proved resolves to exactly one place. Holding the element
        # here also keeps it alive.
        self._last_insert: dict[str, etree._Element] = {}
        # The first and last element of the most recent change, so that
        # comment_last_change() can put the reason for it beside it.
        self._last_change: tuple[etree._Element, etree._Element] | None = None

    def _highest_existing_id(self) -> int:
        """Respect revision ids already in the file from other authors.

        Every story part, not just the body. Headers, footers and footnotes are
        separate XML parts with their own revisions, and this writer edits them
        too: scanning only `word/document.xml` meant a document whose header
        carried another author's `w:ins w:id="7000"` still started allocating
        from 1, and the writer could emit a header revision reusing an id that
        already existed a few elements away.
        """
        highest = 0
        for element in _story_elements(self.doc):
            for el in element.iter():
                raw = el.get(qn("w:id"))
                if raw is not None:
                    try:
                        highest = max(highest, int(raw))
                    except ValueError:
                        continue
        return highest

    def _id(self) -> str:
        self._next_id += 1
        return str(self._next_id)

    # ----------------------------------------------------------------------

    def apply(self, revision: Revision) -> Paragraph:
        """Write one revision. Raises rather than guessing if the anchor is unclear."""
        self._last_change = None
        para, start, end = self._locate(revision.anchor)

        # Snapshot the paragraph before anything is touched. _split_for_span
        # refuses up front for every failure we know how to predict, but this is
        # the load-bearing operation in the product and an unpredicted failure
        # part way through would leave the paragraph half-rewritten while
        # redline.apply() reported the finding as merely "skipped" -- a damaged
        # contract described to the lawyer as an edit that was not made. The
        # audit found exactly that: a span mixing a run inside another author's
        # w:ins with a plain run raised ValueError after the w:ins and w:del had
        # already been spliced in, and the document came back reading
        # "thirty (30) days days after closing".
        snapshot = _deepcopy(para._p)
        try:
            self._write(revision, para, start, end)
        except Exception:
            _restore(para._p, snapshot)
            raise
        return para

    def _write(self, revision: Revision, para: Paragraph, start: int, end: int) -> None:
        runs = _split_for_span(para, start, end)
        if not runs:
            raise AnchorNotFound(revision.anchor)

        first = runs[0]
        parent = first.getparent()
        index = parent.index(first)

        if revision.replacement:
            ins = self._make_ins(revision.replacement, template=first)
            parent.insert(index, ins)
            index += 1

        delete = etree.SubElement(parent, qn("w:del"))
        delete.set(qn("w:id"), self._id())
        delete.set(qn("w:author"), self.author)
        delete.set(qn("w:date"), self.date)
        parent.remove(delete)
        parent.insert(index, delete)

        for run in runs:
            parent.remove(run)
            _to_deleted_text(run)
            delete.append(run)

        self._last_change = (ins if revision.replacement else delete, delete)

    def _make_ins(self, text: str, template=None) -> etree._Element:
        ins = etree.Element(qn("w:ins"))
        ins.set(qn("w:id"), self._id())
        ins.set(qn("w:author"), self.author)
        ins.set(qn("w:date"), self.date)

        run = etree.SubElement(ins, qn("w:r"))
        # Carry the formatting of the text being replaced, so an inserted phrase
        # does not arrive in a different font from the sentence around it.
        props = template.find(qn("w:rPr")) if template is not None else None
        if props is not None:
            run.append(_deepcopy(props))
        # A tab in the new words is a w:tab, as Word writes one. The character
        # itself inside a w:t is not shown as a tab, so "9.4<TAB>The Buyer"
        # would arrive as "9.4The Buyer" or worse.
        for i, piece in enumerate(text.split("\t")):
            if i:
                etree.SubElement(run, qn("w:tab"))
            if piece or i == 0 and "\t" not in text:
                node = etree.SubElement(run, qn("w:t"))
                node.set(XML_SPACE, "preserve")
                node.text = piece
        return ins

    def insert_paragraph_after(self, anchor: str, text: str) -> Paragraph:
        """Add a new paragraph after the one containing the anchor, tracked.

        Needed for drafting: "add a force majeure clause after clause 7" cannot
        be expressed as a replacement. The paragraph is inserted as a revision,
        so rejecting it removes the whole thing.

        The detail that is easy to get wrong is the paragraph mark. A w:ins
        inside w:pPr/w:rPr marks the mark itself as inserted; without it, Word
        rejects the text but leaves an empty paragraph behind, so the document
        the lawyer ends up with is not the one they had before.

        Formatting is inherited from the anchor's paragraph, so a clause added
        after a numbered clause joins the numbering and looks like its
        neighbours rather than arriving as unstyled body text.
        """
        self._last_change = None
        para, _, _ = self._locate(anchor)
        new = self._new_paragraph(para, text)

        # Chain after the previous insertion for this anchor. addnext puts the
        # new paragraph immediately after its target, so inserting 7A, 7B and
        # 7C after clause 7 without chaining wrote them into the document as
        # 7C, 7B, 7A -- in the reverse of the order the lawyer asked for, with
        # every gate passing and the email listing them the right way round.
        self._last_insert.get(anchor, para._p).addnext(new)
        self._last_insert[anchor] = new
        inserted = new.find(qn("w:ins"))
        self._last_change = (inserted, inserted)
        return Paragraph(new, para._parent)

    def _new_paragraph(self, para: Paragraph, text: str) -> etree._Element:
        """A tracked-insertion paragraph styled like `para`, not yet attached."""
        source = para._p

        new = etree.Element(qn("w:p"))

        # Inherit the neighbour's paragraph properties, then mark the paragraph
        # mark as inserted inside them.
        source_props = source.find(qn("w:pPr"))
        props = _deepcopy(source_props) if source_props is not None else etree.Element(qn("w:pPr"))
        # A section break belongs to the paragraph that ends the section, never
        # to a clause copied from it.
        for section in props.findall(qn("w:sectPr")):
            props.remove(section)
        run_props = props.find(qn("w:rPr"))
        if run_props is None:
            run_props = etree.SubElement(props, qn("w:rPr"))
        # Nor does the neighbour's own revision of its paragraph mark. Beside a
        # paragraph that was itself just inserted (or deleted) the copy carried
        # that mark's w:ins (or w:del) and its id, so two new paragraphs in a
        # row failed verification as a duplicate revision id, and a paragraph
        # added after a deleted one read as both inserted and deleted.
        for revision in run_props.findall(qn("w:ins")) + run_props.findall(qn("w:del")):
            run_props.remove(revision)
        mark = etree.Element(qn("w:ins"))
        mark.set(qn("w:id"), self._id())
        mark.set(qn("w:author"), self.author)
        mark.set(qn("w:date"), self.date)
        run_props.insert(0, mark)
        new.append(props)

        template = next(
            (r for r in _runs(para) if _run_text(r)),
            None,
        )
        new.append(self._make_ins(text, template))
        return new

    # ----------------------------------------------------------------------
    # Edits by position rather than by quoted text. Comparison needs these:
    # it already knows which paragraph and which characters differ, and a
    # phrase that changed in clause 4 usually also appears, unchanged, in
    # clause 9, so an anchor would be refused as ambiguous exactly where the
    # comparison is most useful. Same refusals, same snapshot, same markup.
    # ----------------------------------------------------------------------

    def replace_span(self, para: Paragraph, start: int, end: int,
                     replacement: str | None) -> None:
        """Replace characters [start, end) of one paragraph. None deletes."""
        snapshot = _deepcopy(para._p)
        try:
            self._write(Revision("", replacement, self.author), para, start, end)
        except Exception:
            _restore(para._p, snapshot)
            raise

    def insert_at(self, para: Paragraph, offset: int, text: str) -> None:
        """Insert text at a character offset, changing nothing either side."""
        snapshot = _deepcopy(para._p)
        try:
            total = len(_paragraph_text(para))
            if total == 0:
                para._p.append(self._make_ins(text))
                return
            # The runs either side of the offset are the ones a split touches,
            # so they have to pass the same test as the ends of a replacement.
            _isolate_tabs(para, max(0, offset - 1), min(total, offset + 1))
            _assert_safe_to_split(para, max(0, offset - 1), min(total, offset + 1))
            _split_at(para, offset)

            position, target, last = 0, None, None
            for run in _runs(para):
                run_text = _run_text(run)
                if not run_text:
                    continue
                if position == offset:
                    target = run
                    break
                position += len(run_text)
                last = run
            neighbour = target if target is not None else last
            if neighbour is None or (target is None and position != offset):
                raise AnchorNotFound(f"offset {offset}")
            if neighbour.getparent() is not para._p:
                # Inside another author's insertion, a w:ins of ours would
                # nest in theirs, which Word does not allow.
                raise AnchorNotSafelyEditable(
                    "the place to insert is inside another author's tracked change"
                )
            ins = self._make_ins(text, template=neighbour)
            if target is not None:
                target.addprevious(ins)
            else:
                last.addnext(ins)
        except Exception:
            _restore(para._p, snapshot)
            raise

    def delete_paragraph(self, para: Paragraph) -> None:
        """Mark a whole paragraph, and its paragraph mark, as deleted.

        The mark matters for the same reason it does on an insertion: without a
        w:del inside w:pPr/w:rPr, accepting the change removes the words and
        leaves an empty numbered clause behind.

        Nothing is split, so a tab or a line break can ride along inside the
        deletion. Anything that is not plain runs -- a hyperlink, a field, a
        content control, an image, another author's revision -- refuses.
        """
        element = para._p
        snapshot = _deepcopy(element)
        try:
            field_runs = _field_runs(para)
            allowed = _PLAIN_TEXT_CHILDREN | set(_TEXT_EQUIVALENT)
            for child in element:
                if not isinstance(child.tag, str):
                    continue
                if child.tag in _INERT_BETWEEN or child.tag in (qn("w:pPr"), qn("w:del")):
                    continue
                if child.tag != qn("w:r"):
                    raise AnchorNotSafelyEditable(
                        "the paragraph holds a hyperlink, a field, a content "
                        "control or another author's tracked change"
                    )
                if child in field_runs or any(
                    isinstance(c.tag, str) and c.tag not in allowed for c in child
                ):
                    raise AnchorNotSafelyEditable(
                        "the paragraph holds an image, a field or another object"
                    )

            for run in [c for c in element if c.tag == qn("w:r")]:
                delete = etree.Element(qn("w:del"))
                delete.set(qn("w:id"), self._id())
                delete.set(qn("w:author"), self.author)
                delete.set(qn("w:date"), self.date)
                run.addprevious(delete)
                _to_deleted_text(run)
                delete.append(run)

            props = element.find(qn("w:pPr"))
            if props is None:
                props = etree.Element(qn("w:pPr"))
                element.insert(0, props)
            run_props = props.find(qn("w:rPr"))
            if run_props is None:
                run_props = etree.Element(qn("w:rPr"))
                # The schema wants w:rPr ahead of a section break.
                later = next((c for c in props
                              if c.tag in (qn("w:sectPr"), qn("w:pPrChange"))), None)
                if later is not None:
                    later.addprevious(run_props)
                else:
                    props.append(run_props)
            mark = etree.Element(qn("w:del"))
            mark.set(qn("w:id"), self._id())
            mark.set(qn("w:author"), self.author)
            mark.set(qn("w:date"), self.date)
            run_props.insert(0, mark)
        except Exception:
            _restore(element, snapshot)
            raise

    def insert_paragraph_beside(self, para: Paragraph, text: str,
                                before: bool = False) -> Paragraph:
        """A tracked new paragraph next to `para`, styled like it."""
        new = self._new_paragraph(para, text)
        if before:
            para._p.addprevious(new)
        else:
            para._p.addnext(new)
        return Paragraph(new, para._parent)

    def comment(self, anchor: str, text: str, initials: str | None = None) -> None:
        """Attach a Word comment to the span, without changing the text.

        This is how "asks when unsure" reaches the document. A question that
        only appears in the covering email makes the lawyer hunt for the clause
        it refers to; a comment is anchored to the words it is about, shows up
        in Word's review pane beside the tracked changes, and can be replied to.

        Deliberately separate from apply(): a comment asserts nothing and
        changes nothing, so it is safe in places an edit is not. It is allowed
        inside hyperlinks and alongside images, where apply() refuses.

        The initials Word shows in the margin are the author's own, so a
        redline signed "Jane Associate" is marked "JA", not with our name.
        """
        para, start, end = self._locate(anchor)
        runs = _runs_overlapping(para, start, end)
        if not runs:
            raise AnchorNotFound(anchor)
        wrapped = [Run(r, para) for r in runs]
        self.doc.add_comment(
            runs=wrapped, text=text, author=self.author,
            initials=author_initials(self.author) if initials is None else initials,
        )

    def comment_last_change(self, text: str, initials: str | None = None) -> bool:
        """Put `text` in the margin beside the change just written.

        The reason for a substantive change belongs next to it: a lawyer
        deciding whether to accept a tracked change in Word should not have to
        go back to the email to find out why it was made. The comment spans the
        whole change, inserted and deleted text together, which is where Word
        itself anchors a comment made on a selection that holds both.

        The range markers and the reference run sit beside the revision, never
        inside it, so the w:ins and w:del are exactly what apply() wrote and
        accepting or rejecting the change does not touch the comment. For a
        new paragraph they sit inside that paragraph, around its insertion.

        Returns False, writing nothing, when there is no change to annotate or
        the change is outside the main body: Word keeps comments in the body's
        story, and a comment range in a header is not something to risk for a
        note the email already carries.
        """
        if self._last_change is None:
            return False
        first, last = self._last_change
        if first.getroottree().getroot() is not self.doc.element:
            return False
        comment = self.doc.comments.add_comment(
            text=text, author=self.author,
            initials=author_initials(self.author) if initials is None else initials,
        )
        cid = str(comment.comment_id)
        start = etree.Element(qn("w:commentRangeStart"))
        start.set(qn("w:id"), cid)
        end = etree.Element(qn("w:commentRangeEnd"))
        end.set(qn("w:id"), cid)
        reference = etree.Element(qn("w:r"))
        props = etree.SubElement(reference, qn("w:rPr"))
        etree.SubElement(props, qn("w:rStyle")).set(qn("w:val"), "CommentReference")
        etree.SubElement(reference, qn("w:commentReference")).set(qn("w:id"), cid)
        first.addprevious(start)
        last.addnext(end)
        end.addnext(reference)
        self._last_change = None
        return True

    # ----------------------------------------------------------------------

    def _locate(self, anchor: str) -> tuple[Paragraph, int, int]:
        """Find the anchor, tolerating whitespace differences but nothing else.

        A model quoting a document will normalize runs of whitespace and may
        substitute straight quotes for curly ones. Those are safe to forgive.
        Anything beyond that is a sign the anchor is wrong, and rewriting the
        wrong span of a client's contract is the worst thing this code can do.
        """
        needle = _normalize(anchor)
        if not needle:
            raise AnchorNotFound(anchor)
        pattern = _anchor_pattern(needle)

        hits: list[tuple[Paragraph, int, int]] = []
        for para in _all_paragraphs(self.doc):
            raw = _paragraph_text(para)
            # Almost no paragraph holds the anchor. Building the character map
            # for every one of them, for every finding, was most of the time a
            # long agreement took to mark up; the regex normalisation gives the
            # same text (test_ooxml pins that) at a fraction of the cost.
            if not pattern.search(_normalize(raw)):
                continue
            flat, index_map = _normalize_with_map(raw)
            for m in pattern.finditer(flat):
                if m.start() >= len(index_map) or m.end() > len(index_map):
                    continue
                hits.append((para, index_map[m.start()], index_map[m.end() - 1] + 1))

        if not hits:
            if pattern.search(_normalize(_document_text(self.doc))):
                raise AnchorSpansParagraphs(anchor)
            raise AnchorNotFound(anchor)
        if len(hits) > 1:
            raise AnchorAmbiguous(anchor)
        return hits[0]


def author_initials(author: str) -> str:
    """The initials Word shows beside a comment, taken from the author's name.

    "Reviewer" is "R" and "Jane Associate" is "JA". The author is configurable
    so that a forwarded redline does not advertise the tool that made it, and
    initials hard-coded to ours would have undone that in every comment.
    """
    words = re.findall(r"[^\W_]+", author or "")
    return "".join(w[0].upper() for w in words)[:4]


# --------------------------------------------------------------------------
# Run surgery
# --------------------------------------------------------------------------


def _runs_overlapping(para: Paragraph, start: int, end: int) -> list[etree._Element]:
    """Runs touching the span, without splitting anything.

    A comment anchors to whole runs, so it can be a little wider than the exact
    phrase. That is the trade that makes commenting safe where editing is not.
    """
    out: list[etree._Element] = []
    position = 0
    for run in _runs(para):
        text = _run_text(run)
        run_start, run_end = position, position + len(text)
        position = run_end
        if text and run_start < end and run_end > start:
            out.append(run)
    return out


def _split_for_span(para: Paragraph, start: int, end: int) -> list[etree._Element]:
    """Split runs so that the span [start, end) is covered by whole runs.

    Returns those runs in document order. This is the operation everything else
    depends on, and the reason it is done at the element level.

    Refuses before touching anything if the edit cannot be made safely, so a
    failure never leaves the paragraph half-modified.
    """
    _isolate_tabs(para, start, end)
    _assert_safe_to_split(para, start, end)
    _split_at(para, end)
    _split_at(para, start)

    covered: list[etree._Element] = []
    offset = 0
    for run in _runs(para):
        text = _run_text(run)
        run_start, run_end = offset, offset + len(text)
        offset = run_end
        if not text:
            continue
        if run_start >= start and run_end <= end:
            covered.append(run)
    return covered


def _assert_safe_to_split(para: Paragraph, start: int, end: int) -> None:
    """Check every run the edit would touch, before any of them is modified.

    Done as a separate pass on purpose. The writer used to split runs and then
    discover a problem, leaving the paragraph mutated and the finding reported
    as merely "skipped" while the document had already been damaged.

    Refusing is always acceptable here. A wrong edit to a client's contract
    never is, so every rule below errs towards the note in the email.
    """
    field_runs = _field_runs(para)
    overlapping: list[etree._Element] = []
    position = 0
    for run in _runs(para):
        text = _run_text(run)
        run_start, run_end = position, position + len(text)
        position = run_end
        if not text:
            continue
        overlaps = run_start < end and run_end > start
        if not overlaps:
            continue
        overlapping.append(run)
        if _inside_hyperlink(run, para._p):
            raise AnchorNotSafelyEditable("the text to change is inside a hyperlink")
        if run in field_runs:
            raise AnchorNotSafelyEditable(
                "the text to change is produced by a cross-reference or other field"
            )
        if _ancestor(run, para._p, qn("w:sdt")) is not None:
            raise AnchorNotSafelyEditable(
                "the text to change is inside a content control"
            )
        if _carries_non_text(run) and not _is_lone_tab(run):
            raise AnchorNotSafelyEditable(
                "the text to change shares a run with an image, a line break "
                "or a field"
            )
    _assert_one_contiguous_parent(overlapping)


def _is_lone_tab(run: etree._Element) -> bool:
    """A run that is one tab and its formatting, as _isolate_tabs leaves it.

    Safe to delete whole: inside a w:del it is a deleted tab, which Word shows
    struck through and puts back on reject. It is never split, being one
    character long, so nothing about it can be duplicated.
    """
    children = [c for c in run if isinstance(c.tag, str) and c.tag != qn("w:rPr")]
    return len(children) == 1 and children[0].tag == qn("w:tab")


def _isolate_tabs(para: Paragraph, start: int, end: int) -> None:
    """Give every tab in the runs the span touches a run of its own.

    Firm documents that are not auto-numbered hold a clause as one run,
    "9.3<TAB>The liability shall not exceed ...", with the tab a w:tab child
    of the same run as the words. The split below rewrites a run's text as a
    single w:t, which would drop the tab, so such runs used to be refused
    outright and no word of the clause could ever be changed. Cut at the tab
    boundaries instead, each piece carrying a copy of the run's formatting,
    and the paragraph reads and looks exactly as it did. Afterwards a tab
    outside the span is untouched by the edit, and one inside it is a whole
    run the deletion can take.

    Only runs whose other children are plain text qualify; a run that also
    holds an image, a break or a field character is left for the refusal.
    The caller snapshots the paragraph, so a later refusal restores it.
    """
    allowed = _PLAIN_TEXT_CHILDREN | {qn("w:tab")}
    position = 0
    for run in _runs(para):
        text = _run_text(run)
        run_start, run_end = position, position + len(text)
        position = run_end
        if not (run_start < end and run_end > start) or len(text) < 2:
            continue
        children = [c for c in run if isinstance(c.tag, str)]
        if not any(c.tag == qn("w:tab") for c in children):
            continue
        if any(c.tag not in allowed for c in children):
            continue
        props = run.find(qn("w:rPr"))
        pieces: list[list[etree._Element]] = [[]]
        for child in run:
            if child is props:
                continue
            if child.tag == qn("w:tab"):
                if pieces[-1]:
                    pieces.append([])
                pieces[-1].append(child)
                pieces.append([])
            else:
                pieces[-1].append(child)
        pieces = [piece for piece in pieces if piece]
        anchor = run
        for piece in pieces[1:]:
            clone = etree.Element(qn("w:r"))
            for name, value in run.attrib.items():
                clone.set(name, value)
            if props is not None:
                clone.append(_deepcopy(props))
            for child in piece:
                clone.append(child)       # moves it out of the original run
            anchor.addnext(clone)
            anchor = clone


def _assert_one_contiguous_parent(runs: list[etree._Element]) -> None:
    """Refuse a span whose runs are not a single unbroken stretch of siblings.

    apply() splices the w:ins and w:del in at the position of the first run and
    then moves each run into the w:del. That is only correct when every run has
    the same parent and nothing that matters sits between them.

    Both halves of this have been observed to corrupt a document. Mixed parents
    -- a plain run next to one inside another author's w:ins -- raised
    ValueError from `parent.remove(run)` after the markup was already spliced
    in. And when text the model cannot see sits between two covered runs (a
    content control, or another author's w:del), moving the runs out from around
    it reorders the paragraph: reject-all then reassembles the sentence in the
    wrong order.
    """
    if not runs:
        return
    parent = runs[0].getparent()
    if any(run.getparent() is not parent for run in runs):
        raise AnchorNotSafelyEditable(
            "the text to change straddles a field, content control or another "
            "author's tracked change"
        )
    for before, after in itertools.pairwise(runs):
        for between in parent[parent.index(before) + 1: parent.index(after)]:
            if not isinstance(between.tag, str):
                continue                      # comments and processing instructions
            if between.tag in _INERT_BETWEEN:
                continue
            # An empty run carrying nothing but formatting is harmless; one
            # carrying an image or a break is exactly what we are refusing.
            if (between.tag == qn("w:r") and not _run_text(between)
                    and not _carries_non_text(between)):
                continue
            raise AnchorNotSafelyEditable(
                "something the edit cannot move sits in the middle of the text "
                "to change"
            )


def _field_runs(para: Paragraph) -> set[etree._Element]:
    """Every run belonging to a field: its instruction, its switches, its result.

    A complex field is a flat run of siblings bracketed by w:fldChar begin and
    end, with the visible result between `separate` and `end`. That result is an
    ordinary w:r, so the writer would happily move "4.2" into a w:del and leave
    ` REF _Ref12345 \\r \\h ` behind: the next field update regenerates the old
    number and the tracked change the lawyer accepted evaporates. Cross
    references are one of the headline deterministic checks, so this is the
    exact intersection the product hits.
    """
    out: set[etree._Element] = set()
    depth = 0
    for run in _runs(para):
        if _ancestor(run, para._p, qn("w:fldSimple")) is not None:
            out.add(run)
            continue
        chars = run.findall(qn("w:fldChar"))
        if depth > 0 or chars:
            out.add(run)
        for char in chars:
            kind = char.get(qn("w:fldCharType"))
            if kind == "begin":
                depth += 1
            elif kind == "end":
                depth = max(0, depth - 1)
    return out


def _split_at(para: Paragraph, offset: int) -> None:
    """Split whichever run straddles `offset` into two runs at that point."""
    if offset <= 0:
        return
    position = 0
    for run in _runs(para):
        text = _run_text(run)
        if not text:
            continue
        run_start, run_end = position, position + len(text)
        position = run_end
        if run_start < offset < run_end:
            cut = offset - run_start
            # Built from the run's formatting rather than deep-copied. A copy
            # carried every non-text child with it -- images, breaks, field
            # characters, text boxes -- and the duplicates landed outside any
            # w:ins/w:del, so neither Accept All nor Reject All could remove
            # them. _assert_safe_to_split refuses such runs before we get here;
            # constructing the clone instead of copying means the duplication
            # cannot happen even if it ever stops doing so.
            clone = etree.Element(qn("w:r"))
            props = run.find(qn("w:rPr"))
            if props is not None:
                clone.append(_deepcopy(props))
            _set_run_text(clone, text[:cut])
            _set_run_text(run, text[cut:])
            run.getparent().insert(run.getparent().index(run), clone)
            return


def _restore(element: etree._Element, snapshot: etree._Element) -> None:
    """Put an element back exactly as it was, keeping its identity in the tree."""
    for child in list(element):
        element.remove(child)
    element.attrib.clear()
    element.attrib.update(snapshot.attrib)
    for child in list(snapshot):
        element.append(child)


def _to_deleted_text(run: etree._Element) -> None:
    """Convert a run's w:t nodes to w:delText, as Word requires inside w:del."""
    for node in run.findall(qn("w:t")):
        node.tag = qn("w:delText")
        node.set(XML_SPACE, "preserve")


def _set_run_text(run: etree._Element, text: str) -> None:
    nodes = run.findall(qn("w:t")) + run.findall(qn("w:delText"))
    if not nodes:
        node = etree.SubElement(run, qn("w:t"))
        nodes = [node]
    nodes[0].text = text
    nodes[0].set(XML_SPACE, "preserve")
    for extra in nodes[1:]:
        extra.getparent().remove(extra)


# Children of a run that Word renders as text. python-docx's own CT_R.text does
# exactly this, and the two models have to agree: extract.py feeds the agent and
# the deterministic checks para.text, and every anchor they produce is matched
# against _paragraph_text(). A tab rendered as nothing here, where para.text
# renders "\t", made the numbering of every ordinary agreement ("2.<TAB>Fees.")
# unlocatable, and offsets drift silently wherever the two disagree.
_TEXT_EQUIVALENT = {
    qn("w:tab"): "\t",
    qn("w:ptab"): "\t",
    qn("w:br"): "\n",
    qn("w:cr"): "\n",
    qn("w:noBreakHyphen"): "-",
}

# The only children a run may have if the writer is going to split or delete it.
# Anything else -- an image, a break, a field character, a text box -- is either
# duplicated by a split or swallowed by a deletion the lawyer never asked for.
# A whitelist rather than a blacklist on purpose: an element nobody here has
# thought of should refuse the edit, not sail through it.
_PLAIN_TEXT_CHILDREN = {
    qn("w:rPr"), qn("w:t"), qn("w:delText"), qn("w:annotationRef"),
    # Layout cache Word rewrites on its own; harmless in either half of a split.
    qn("w:lastRenderedPageBreak"),
}

# Markers that may sit between two runs of the span without meaning anything is
# there. Everything else between them refuses the edit.
_INERT_BETWEEN = {
    qn("w:bookmarkStart"), qn("w:bookmarkEnd"), qn("w:proofErr"),
    qn("w:commentRangeStart"), qn("w:commentRangeEnd"),
    qn("w:permStart"), qn("w:permEnd"),
    qn("w:moveFromRangeStart"), qn("w:moveFromRangeEnd"),
    qn("w:moveToRangeStart"), qn("w:moveToRangeEnd"),
}

# Containers whose runs are part of the visible text of a paragraph. Missing any
# of these means our text model disagrees with what Word displays, which makes
# offsets wrong and defeats the ambiguity guard: the text on either side of the
# container becomes adjacent in our model, so an anchor a lawyer can see is not
# contiguous matches anyway, and a phrase they can see twice is counted once.
# Making them visible does not make them editable -- _assert_safe_to_split
# refuses fields, content controls and hyperlinks -- it makes them countable.
_TRANSPARENT = (
    qn("w:ins"), qn("w:moveTo"), qn("w:hyperlink"), qn("w:smartTag"),
    qn("w:fldSimple"), qn("w:customXml"), qn("w:bdo"), qn("w:dir"),
)


def _run_text(run: etree._Element) -> str:
    """The text Word displays for one run.

    Direct children only. _set_run_text writes direct children, so reading
    descendants instead meant the two disagreed for a run holding a floating
    text box, the offsets drifted, and the split wrote the wrong slice.
    """
    parts: list[str] = []
    for child in run:
        if child.tag in (qn("w:t"), qn("w:delText")):
            parts.append(child.text or "")
        else:
            equivalent = _TEXT_EQUIVALENT.get(child.tag)
            if equivalent is not None:
                parts.append(equivalent)
    return "".join(parts)


def _runs(para: Paragraph) -> list[etree._Element]:
    """Every run that contributes visible text, in document order.

    Descends into insertions, moved-in text, hyperlinks, smart tags, simple
    fields, custom XML and structured-document content, because Word renders all
    of those as ordinary text. Runs inside a w:del or a w:moveFrom are excluded:
    that text is already deleted, so it is not part of the visible document and
    must not be matched against.
    """
    out: list[etree._Element] = []

    def walk(node) -> None:
        for child in node:
            if not isinstance(child.tag, str):
                continue
            if child.tag == qn("w:r"):
                out.append(child)
            elif child.tag == qn("w:sdt"):
                # Only w:sdtContent holds the control's text; w:sdtPr holds its
                # binding and its placeholder, neither of which is displayed.
                for grandchild in child:
                    if grandchild.tag == qn("w:sdtContent"):
                        walk(grandchild)
            elif child.tag in _TRANSPARENT:
                walk(child)

    walk(para._p)
    return out


def _carries_non_text(run: etree._Element) -> bool:
    """True when a run holds something a split would duplicate or a delete eat."""
    return any(
        child.tag not in _PLAIN_TEXT_CHILDREN
        for child in run
        if isinstance(child.tag, str)
    )


def _ancestor(node: etree._Element, stop, tag: str):
    """The nearest ancestor of `node` with `tag`, searching no further than `stop`."""
    parent = node.getparent()
    while parent is not None and parent is not stop:
        if parent.tag == tag:
            return parent
        parent = parent.getparent()
    return None


def _inside_hyperlink(run: etree._Element, stop) -> bool:
    return _ancestor(run, stop, qn("w:hyperlink")) is not None


def _paragraph_text(para: Paragraph) -> str:
    return "".join(_run_text(r) for r in _runs(para))



# --------------------------------------------------------------------------
# Traversal and text helpers
# --------------------------------------------------------------------------


# Content types of the parts that hold a story: text Word displays and that can
# therefore carry revisions of its own.
_STORY_CONTENT_TYPES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml."
    f"{kind}+xml"
    for kind in ("header", "footer", "footnotes", "endnotes")
}

# Elements that can hold paragraphs. Deliberately not w:drawing or w:pict: a
# text box is a separate story that extract.py does not show the agent, so
# neither the anchor it quotes nor the count of occurrences should come from
# one.
_PARAGRAPH_CONTAINERS = {
    qn("w:tbl"), qn("w:tr"), qn("w:tc"), qn("w:sdt"), qn("w:sdtContent"),
    qn("w:customXml"),
    # A text box holds real paragraphs, and the extractor already reads them,
    # so the agent can anchor on their text. Without this the writer could not
    # find that anchor, and worse, the two disagreed about how many times a
    # phrase occurs -- which is what the ambiguity guard is computed from.
    qn("w:txbxContent"),
}

# Elements that wrap a text box. Walked into so the paragraphs inside are
# reached; a text box can be sized to nothing or placed behind the text, which
# makes it the best hiding place in the format.
_MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
_TEXTBOX_WRAPPERS = {
    qn("w:pict"), qn("w:drawing"), qn("w:object"),
    # python-docx's nsmap has no "mc" prefix, so this one is spelled out.
    f"{_MC}AlternateContent", f"{_MC}Choice", f"{_MC}Fallback",
}


def _story_parts(document):
    """The header, footer and note parts that already exist in the package.

    Deliberately not section.header / section.first_page_footer and friends:
    those accessors are get-or-create, so merely searching a document for an
    anchor materialised six header and footer parts and wrote six references
    into its sectPr. samples/simple.docx grew by three kilobytes and gained
    parts it never had, on every document the tool touched, purely from looking
    at it. "Preserve headers, footers, numbering, styles and embedded objects"
    is the first non-negotiable in docs/redlining.md.
    """
    seen: set[str] = set()
    for part in document.part.related_parts.values():
        if getattr(part, "content_type", None) not in _STORY_CONTENT_TYPES:
            continue
        name = str(getattr(part, "partname", ""))
        if name in seen:
            continue
        seen.add(name)
        yield part


def _story_elements(document):
    """The root element of every part whose text Word displays."""
    yield document.element
    for part in _story_parts(document):
        element = getattr(part, "element", None)
        if element is not None and hasattr(element, "iter"):
            yield element


def _paragraphs_under(element, parent):
    """Paragraphs in one story, in document order, descending into containers."""
    for child in element:
        if not isinstance(child.tag, str):
            continue
        if child.tag == qn("w:p"):
            yield Paragraph(child, parent)
            # A run in this paragraph may itself wrap a text box.
            for nested in child.iter():
                if isinstance(nested.tag, str) and nested.tag == qn("w:txbxContent"):
                    yield from _paragraphs_under(nested, parent)
        elif child.tag in _PARAGRAPH_CONTAINERS or child.tag in _TEXTBOX_WRAPPERS:
            yield from _paragraphs_under(child, parent)


def _all_paragraphs(document):
    """Every paragraph in the document, once each.

    Walked from the XML rather than through python-docx's collections, because
    those repeat themselves. `row.cells` returns one entry per grid position, so
    a cell merged across three columns yields the same lxml element three times,
    and a section whose header is linked to the previous one returns the
    previous section's header again. _locate() counted each repeat as another
    occurrence and refused a unique anchor as ambiguous -- telling the lawyer
    that "Schedule of Fees payable on Closing" appears more than once in a
    document where it appears exactly once, which is a claim they can check.

    The walk also reaches places the old one did not: nested tables, and
    paragraphs inside a block-level content control. Text the writer cannot see
    is worse than text it cannot edit, because an anchor that occurs twice looks
    unique and gets edited.
    """
    # Dedup by element identity, keeping a strong reference to every element
    # we have seen. `id()` alone is not safe here: lxml hands out proxy objects
    # that are garbage-collected once nothing holds them, and CPython reuses
    # the freed address, so an id already in the set can belong to a different
    # element. That made a header paragraph look already-seen and get skipped,
    # which passed on one machine and failed in CI on the same commit. Holding
    # the element pins the address for as long as the set relies on it.
    seen: set[int] = set()
    keep_alive: list = []

    def fresh(paragraph):
        element = paragraph._p
        key = id(element)
        if key in seen:
            return False
        seen.add(key)
        keep_alive.append(element)
        return True

    for paragraph in _paragraphs_under(document.element.body, document):
        if fresh(paragraph):
            yield paragraph
    for part in _story_parts(document):
        element = getattr(part, "element", None)
        if element is None:
            continue
        for paragraph in _paragraphs_under(element, part):
            if fresh(paragraph):
                yield paragraph


def _document_text(document) -> str:
    return "\n".join(_paragraph_text(p) for p in _all_paragraphs(document))


_QUOTES = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'",
                         "–": "-", "—": "-", " ": " "})


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.translate(_QUOTES)).strip()


def _anchor_pattern(needle: str) -> re.Pattern[str]:
    """A single-word anchor matches whole words; a phrase matches as written.

    A bare substring match is wrong in both directions for a single word.
    "Indemnitee" is reported as appearing more than once in a document that says
    "the Indemnitee shall notify the Indemnitees", so a unique anchor is refused
    as ambiguous; and where the standalone occurrence sits somewhere the writer
    will not edit, the only surviving match is the one inside the longer word
    and the edit lands there, turning "the Indemnitees listed in Schedule 2"
    into "the Indemnified Partys listed in Schedule 2". The one deterministic
    check that sets auto_apply anchors on a bare word, so this is on by default.

    Only for a single word, though. checks.py deliberately pads a phrase anchor
    with the characters around it to make it unique, and those pads start and
    end mid-word on purpose ("he term is thirty (13) months from the E").
    Requiring a word boundary there would make the padding unlocatable, which
    trades a rare wrong edit for a common silent miss. A phrase anchor is long
    enough that landing inside a longer word is not a realistic failure.
    """
    pattern = re.escape(needle)
    if " " in needle:
        return re.compile(pattern)
    if needle[:1].isalnum() or needle[:1] == "_":
        pattern = r"\b" + pattern
    if needle[-1:].isalnum() or needle[-1:] == "_":
        pattern = pattern + r"\b"
    return re.compile(pattern)


def _normalize_with_map(text: str) -> tuple[str, list[int]]:
    """Normalized text plus, for each normalized character, its original index."""
    translated = text.translate(_QUOTES)
    out: list[str] = []
    index_map: list[int] = []
    previous_was_space = True  # strips leading whitespace, matching _normalize
    for i, ch in enumerate(translated):
        if ch.isspace():
            if previous_was_space:
                continue
            out.append(" ")
            index_map.append(i)
            previous_was_space = True
        else:
            out.append(ch)
            index_map.append(i)
            previous_was_space = False
    while out and out[-1] == " ":
        out.pop()
        index_map.pop()
    return "".join(out), index_map


def _deepcopy(element: etree._Element) -> etree._Element:
    return etree.fromstring(etree.tostring(element))


# --------------------------------------------------------------------------
# Someone else's tracked changes: listed, accepted and rejected one by one
# --------------------------------------------------------------------------
#
# Turning the other side's markup is deciding, change by change, which of their
# edits we take. Word does it with Accept and Reject on one revision at a time;
# this does the same by revision id, and touches nothing else: every revision
# not named keeps its id, author, date and text, which turning.examine()
# checks. Revisions that belong together (a new paragraph's text and its
# paragraph mark, the two halves of a move) are one decision, as in Word.


class RevisionNotFound(LookupError):
    """No tracked change has that id."""


class CannotDecide(ValueError):
    """A tracked change this will not accept or reject by itself. The message
    says so in words the model can pass on."""


_PROPERTY_CHANGES = {qn("w:rPrChange"): qn("w:rPr"), qn("w:pPrChange"): qn("w:pPr")}
_OTHER_CHANGES = (qn("w:tblPrChange"), qn("w:tcPrChange"), qn("w:trPrChange"),
                  qn("w:sectPrChange"), qn("w:tblGridChange"), qn("w:cellIns"),
                  qn("w:cellDel"), qn("w:cellMerge"), qn("w:numberingChange"))
# Marks that must survive the text around them being removed: a comment's
# range, and the bookmarks cross-references point at.
_KEEP_MARKS = (qn("w:commentRangeStart"), qn("w:commentRangeEnd"),
               qn("w:bookmarkStart"), qn("w:bookmarkEnd"))


@dataclass
class TrackedChange:
    """One decision: a revision, or the several Word treats as one."""

    id: str
    # replaced | inserted | deleted | new paragraph | removed paragraph |
    # moved | formatting | inserted row | deleted row | other
    kind: str
    author: str
    date: str
    text: str
    part: str
    elements: list

    def as_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "author": self.author,
                "date": self.date, "text": self.text, "part": self.part}


def _part_name(document, element) -> str:
    if element is document.element:
        return "document.xml"
    for part in _story_parts(document):
        if getattr(part, "element", None) is element:
            return str(part.partname).rsplit("/", 1)[-1]
    return "?"


def _revision_text(element, deleted: bool) -> str:
    wanted = qn("w:delText") if deleted else qn("w:t")
    parts = []
    for node in element.iter():
        if node.tag == wanted:
            parts.append(node.text or "")
        elif node.tag == qn("w:tab"):
            parts.append("\t")
    return "".join(parts)


def tracked_changes(document) -> list[TrackedChange]:
    """Every tracked change in every story, grouped as the decisions a
    lawyer makes in Word's Accept/Reject."""
    out: list[TrackedChange] = []
    for story in _story_elements(document):
        part = _part_name(document, story)
        taken: set = set()
        keep: list = []     # holds the elements so their ids stay theirs

        def add(kind, lead, elements, text, part=part, taken=taken, keep=keep):
            for e in elements:
                taken.add(id(e))
                keep.append(e)
            out.append(TrackedChange(
                id=lead.get(qn("w:id"), ""), kind=kind,
                author=lead.get(qn("w:author"), "") or "",
                date=lead.get(qn("w:date"), "") or "", text=text, part=part,
                elements=list(elements)))

        for row_props in story.iter(qn("w:trPr")):
            for mark in row_props:
                if mark.tag in (qn("w:ins"), qn("w:del")):
                    row = row_props.getparent()
                    add("inserted row" if mark.tag == qn("w:ins") else "deleted row",
                        mark, [mark], " | ".join(_revision_text(tc, mark.tag == qn("w:del"))
                                                 for tc in row.findall(qn("w:tc"))))

        # A paragraph mark, with the same author's revision of that
        # paragraph's text: "the new clause" is one decision.
        for mark in list(story.iter(qn("w:ins"), qn("w:del"))):
            holder = mark.getparent()
            if holder is None or holder.tag != qn("w:rPr"):
                continue
            props = holder.getparent()
            if props is None or props.tag != qn("w:pPr"):
                continue
            paragraph = props.getparent()
            same = [r for r in paragraph.iter(mark.tag)
                    if r is not mark and r.getparent().tag != qn("w:rPr")
                    and r.get(qn("w:author")) == mark.get(qn("w:author"))
                    and id(r) not in taken]
            deleted = mark.tag == qn("w:del")
            add("removed paragraph" if deleted else "new paragraph", mark, same + [mark],
                "".join(_revision_text(r, deleted) for r in same))

        # Moves, paired by the name on their range markers.
        moves: dict[str, list] = {}
        for move in story.iter(qn("w:moveFrom"), qn("w:moveTo")):
            if id(move) not in taken:
                moves.setdefault(_move_name(move), []).append(move)
        for group in moves.values():
            source = next((m for m in group if m.tag == qn("w:moveFrom")), group[0])
            add("moved", source, group, _revision_text(source, source.tag == qn("w:moveFrom")))

        for revision in story.iter(qn("w:ins"), qn("w:del")):
            if id(revision) in taken or revision.getparent().tag in (qn("w:trPr"), qn("w:rPr")):
                continue
            partner = _replacement_partner(revision)
            if partner is not None and id(partner) not in taken:
                # "informaton" struck and "information" inserted beside it, by
                # the same hand: one replacement, as a lawyer reads it.
                old, new = ((revision, partner) if revision.tag == qn("w:del")
                            else (partner, revision))
                add("replaced", revision, [revision, partner],
                    f"{_revision_text(old, True)} → {_revision_text(new, False)}")
                continue
            deleted = revision.tag == qn("w:del")
            add("deleted" if deleted else "inserted", revision, [revision],
                _revision_text(revision, deleted))

        for change in story.iter(*_PROPERTY_CHANGES):
            owner = change.getparent().getparent()
            add("formatting", change, [change], _revision_text(owner, False)[:120])

        for change in story.iter(*_OTHER_CHANGES):
            add("other", change, [change], "")
    return out


def _replacement_partner(revision):
    """The deletion (or insertion) right beside this insertion (or deletion),
    by the same author, with nothing but inert markers between: the other
    half of a replacement. None if there is no such neighbour."""
    other = qn("w:del") if revision.tag == qn("w:ins") else qn("w:ins")
    for step in ("getnext", "getprevious"):
        node = getattr(revision, step)()
        while node is not None and (not isinstance(node.tag, str)
                                    or node.tag in _INERT_BETWEEN):
            node = getattr(node, step)()
        if (node is not None and node.tag == other
                and node.get(qn("w:author")) == revision.get(qn("w:author"))):
            return node
    return None


def _move_name(move) -> str:
    """The move a moveFrom or moveTo belongs to: the name on the nearest
    range start before it, in document order."""
    start_tag = (qn("w:moveFromRangeStart") if move.tag == qn("w:moveFrom")
                 else qn("w:moveToRangeStart"))
    node = move
    while node is not None:
        sibling = node.getprevious()
        while sibling is not None:
            if isinstance(sibling.tag, str):
                if sibling.tag == start_tag:
                    return sibling.get(qn("w:name"), "")
                found = list(sibling.iter(start_tag))
                if found:
                    return found[-1].get(qn("w:name"), "")
            sibling = sibling.getprevious()
        node = node.getparent()
    return f"#{move.get(qn('w:id'))}"


def decide(document, accept=(), reject=()) -> dict[str, list[str]]:
    """Accept some tracked changes and reject others, by id.

    Refuses before touching anything when an id is unknown, is named on
    both sides, or is a change this will not decide by itself (table cell
    and table or section property changes). Any id of a grouped change
    decides the whole group. Returns the lead ids handled, by outcome.
    """
    accept = [str(i).strip() for i in accept if str(i).strip()]
    reject = [str(i).strip() for i in reject if str(i).strip()]
    changes = tracked_changes(document)
    by_id: dict[str, TrackedChange] = {}
    for change in changes:
        for key in [change.id, f"{change.part}:{change.id}"] + [
                e.get(qn("w:id"), "") for e in change.elements]:
            by_id.setdefault(key, change)

    plan: list[tuple[TrackedChange, bool]] = []
    decided: dict[int, bool] = {}
    for ids, accepting in ((accept, True), (reject, False)):
        for rid in ids:
            change = by_id.get(rid)
            if change is None:
                raise RevisionNotFound(
                    f"there is no tracked change with id {rid}; list them again")
            if id(change) in decided:
                if decided[id(change)] != accepting:
                    raise CannotDecide(f"change {change.id} is named to be both "
                                       "accepted and rejected")
                continue
            if change.kind == "other":
                raise CannotDecide(f"change {rid} changes a table's cells or a table's "
                                   "or section's settings; that has to be decided in Word")
            decided[id(change)] = accepting
            plan.append((change, accepting))

    done: dict[str, list[str]] = {"accepted": [], "rejected": []}
    for change, accepting in plan:
        _decide_one(change, accepting)
        done["accepted" if accepting else "rejected"].append(change.id)
    return done


def _decide_one(change: TrackedChange, accepting: bool) -> None:
    kind = change.kind
    if kind in ("inserted row", "deleted row"):
        mark = change.elements[0]
        row = mark.getparent().getparent()
        if (kind == "deleted row") == accepting:
            _remove_keeping_marks(row)
        else:
            mark.getparent().remove(mark)
        return
    if kind == "formatting":
        change_el = change.elements[0]
        props = change_el.getparent()
        props.remove(change_el)
        if not accepting:
            old = change_el.find(_PROPERTY_CHANGES[change_el.tag])
            # A paragraph's mark formatting and section break are not part
            # of what a paragraph-formatting change records.
            kept = ([c for c in props if c.tag in (qn("w:rPr"), qn("w:sectPr"))]
                    if props.tag == qn("w:pPr") else [])
            for child in list(props):
                props.remove(child)
            for child in (list(old) if old is not None else []):
                props.append(child)
            for child in kept:
                props.append(child)
        return

    marks = [e for e in change.elements
             if e.getparent() is not None and e.getparent().tag == qn("w:rPr")]
    move_names: set[str] = set()
    root = change.elements[0]
    if kind == "moved":
        move_names = {_move_name(e) for e in change.elements}
        while root.getparent() is not None:
            root = root.getparent()
    for element in change.elements:
        if element in marks or element.getparent() is None:
            continue
        inserted = element.tag in (qn("w:ins"), qn("w:moveTo"))
        if inserted == accepting:
            _unwrap(element)
        else:
            _remove_keeping_marks(element)
    for mark in marks:
        props = mark.getparent()
        paragraph = props.getparent().getparent()
        props.remove(mark)
        if len(props) == 0:
            props.getparent().remove(props)
        if (mark.tag == qn("w:ins")) != accepting:
            _join_with_next(paragraph)
    if kind == "moved":
        _drop_move_ranges(root, move_names)


def _unwrap(element) -> None:
    """Keep what the revision holds, as ordinary text."""
    parent = element.getparent()
    index = parent.index(element)
    if element.tag in (qn("w:del"), qn("w:moveFrom")):
        for node in element.iter(qn("w:delText")):
            node.tag = qn("w:t")
        for node in element.iter(qn("w:delInstrText")):
            node.tag = qn("w:instrText")
    for offset, child in enumerate(list(element)):
        parent.insert(index + offset, child)
    parent.remove(element)


def _remove_keeping_marks(element) -> None:
    """Remove a revision and its text, leaving every comment anchor and
    bookmark inside it where it was: taking out someone's words must never
    take out the comment on them."""
    in_paragraph = element.tag != qn("w:tr")
    for mark in list(element.iter(*_KEEP_MARKS)):
        mark.getparent().remove(mark)
        if in_paragraph:
            element.addprevious(mark)
        else:
            _first_paragraph_after(element).insert(0, mark)
    for reference in list(element.iter(qn("w:commentReference"))):
        run = etree.Element(qn("w:r"))
        etree.SubElement(etree.SubElement(run, qn("w:rPr")),
                         qn("w:rStyle")).set(qn("w:val"), "CommentReference")
        reference.getparent().remove(reference)
        run.append(reference)
        if in_paragraph:
            element.addprevious(run)
        else:
            _first_paragraph_after(element).append(run)
    element.getparent().remove(element)


def _first_paragraph_after(row):
    """Where a deleted row's comment anchors go: the first paragraph after
    the table, or a new empty one."""
    table = row.getparent()
    following = table.getnext()
    while following is not None and following.tag != qn("w:p"):
        following = following.getnext()
    if following is None:
        following = etree.Element(qn("w:p"))
        table.addnext(following)
    return following


def _join_with_next(paragraph) -> None:
    """A paragraph mark is gone: this paragraph's content joins the next
    paragraph, which keeps its own formatting, as Word does. An empty one
    simply goes; with nothing after it to join, it stays."""
    following = paragraph.getnext()
    content = [c for c in paragraph if c.tag != qn("w:pPr")]
    if following is None or following.tag != qn("w:p"):
        if not content:
            paragraph.getparent().remove(paragraph)
        return
    anchor = following.find(qn("w:pPr"))
    position = 0 if anchor is None else list(following).index(anchor) + 1
    for offset, child in enumerate(content):
        following.insert(position + offset, child)
    paragraph.getparent().remove(paragraph)


def _drop_move_ranges(root, names: set[str]) -> None:
    """A decided move leaves no range markers behind."""
    for start_tag, end_tag in ((qn("w:moveFromRangeStart"), qn("w:moveFromRangeEnd")),
                               (qn("w:moveToRangeStart"), qn("w:moveToRangeEnd"))):
        for marker in list(root.iter(start_tag)):
            if marker.get(qn("w:name")) not in names:
                continue
            rid = marker.get(qn("w:id"))
            for end in list(root.iter(end_tag)):
                if end.get(qn("w:id")) == rid:
                    end.getparent().remove(end)
            marker.getparent().remove(marker)
