"""Findings to Word tracked changes.

The whole product rests on this file. A redline the recipient has to re-key is
worthless; a redline that opens in Word with a working Accept/Reject pane is the
entire value proposition.

Two rules govern everything here.

**Never guess.** If a finding's anchor cannot be located exactly once, the edit
is not made. It is raised in the email instead. A misplaced edit in a client's
contract is far worse than a missing one.

**Never send what we have not proved readable.** Every output is round-tripped
through a parser and its revision markup is checked before it can be attached.
If that fails the reply degrades to a memo.
"""

from __future__ import annotations

import hashlib
import logging
import re
import zipfile
from dataclasses import dataclass, field
from io import BytesIO

from docx import Document

from secondeye.models import Finding, ReviewResult, Severity
from secondeye.pipeline.ooxml import (
    AnchorAmbiguous,
    AnchorNotFound,
    AnchorNotSafelyEditable,
    AnchorSpansParagraphs,
    Revision,
    RevisionWriter,
)
from secondeye.pipeline.validate import STORY_PART, validate

log = logging.getLogger(__name__)

AUTHOR = "Reviewer"  # the default of settings().redline_author


def configured_author() -> str:
    """The configured revision author, read when writing so a setting takes.

    The writer also runs inside the sandbox as a skill, where the config module
    is not bundled; there it signs as the default.
    """
    try:
        from secondeye.config import settings

        return settings().redline_author or AUTHOR
    except ImportError:
        return AUTHOR



@dataclass
class RedlineOutput:
    content: bytes
    applied: list[Finding] = field(default_factory=list)
    skipped: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    commented: list[Finding] = field(default_factory=list)
    # Applied findings whose reason was also put in the margin beside the
    # change. Separate from `commented`, which holds questions left for the
    # lawyer and is what the email counts as "asked in the document".
    explained: list[Finding] = field(default_factory=list)


def apply(original: bytes, result: ReviewResult, autopilot: bool = False,
          author: str | None = None, comment_questions: bool = True) -> RedlineOutput:
    """Apply findings to the document as tracked changes.

    `autopilot=False` (the default) writes only findings the reviewer marked
    `auto_apply`, which means unambiguous corrections that could not plausibly
    be a deliberate choice. Everything with legal meaning is raised in the email
    for the lawyer to decide. This stays the default until the accept rate on
    real documents justifies loosening it.
    """
    candidates, skipped = _partition(result.findings, autopilot)
    # Findings the agent could not resolve become Word comments anchored where
    # they apply, so the lawyer reads the question beside the clause rather
    # than hunting for it from an email.
    questions = [
        f for f in result.findings
        if comment_questions and f.question and f.anchor and f not in candidates
    ]
    if not candidates and not questions:
        return RedlineOutput(content=original, applied=[], skipped=skipped)

    try:
        document = Document(BytesIO(original))
    except Exception:
        log.exception("could not open the document to redline it")
        return RedlineOutput(content=original, applied=[],
                             skipped=skipped + candidates,
                             notes=["The document could not be opened for editing."])

    author = author or configured_author()
    writer = RevisionWriter(document, author=author)
    applied: list[Finding] = []
    commented: list[Finding] = []
    explained: list[Finding] = []
    notes: list[str] = []
    unexpected = False

    for finding in candidates:
        try:
            if finding.edit_kind == "insert_after":
                # Drafting: a new paragraph after the anchor's, rather than a
                # swap. Rejecting it removes the whole paragraph.
                writer.insert_paragraph_after(finding.anchor, finding.suggested_text)
            else:
                writer.apply(Revision(anchor=finding.anchor,
                                      replacement=finding.suggested_text,
                                      author=author))
            applied.append(finding)
            if _explain(writer, finding):
                explained.append(finding)
        except AnchorAmbiguous:
            skipped.append(finding)
            notes.append(
                f'"{_short(finding.anchor)}" appears more than once, so I did not '
                "change it automatically."
            )
        except AnchorSpansParagraphs:
            skipped.append(finding)
            notes.append(
                f'"{_short(finding.anchor)}" runs across paragraphs, so I left it '
                "for you rather than risk the formatting."
            )
        except AnchorNotSafelyEditable as e:
            skipped.append(finding)
            notes.append(
                f'I left "{_short(finding.anchor)}" alone because {e}, and changing '
                "it automatically could have damaged the document."
            )
        except AnchorNotFound:
            skipped.append(finding)
            # Neither the anchor nor the title reaches the log. The anchor is
            # text quoted verbatim from a client's contract; a model-written
            # title routinely names the parties and the clause. Anchor misses
            # are not rare -- this module exists to refuse them -- so this line
            # runs on ordinary documents at the default INFO level, and stdout
            # on a firm deployment is shipped to a log backend that has no
            # deletion path of its own.
            log.info("anchor not found for a %s finding (%s)",
                     finding.category, _reference(finding))
        except Exception:
            skipped.append(finding)
            unexpected = True
            log.exception("failed to apply a %s finding (%s)",
                          finding.category, _reference(finding))

    if unexpected:
        # Every refusal this writer knows how to make is a typed exception. An
        # untyped one means something happened that nobody predicted, and a
        # prediction is what the "the paragraph is untouched" guarantee rests
        # on. The writer rolls the paragraph back, but the cost of being wrong
        # about that is a damaged clause in a client's contract described to
        # the lawyer as an edit that simply was not made. So the whole redline
        # is abandoned and the reply degrades to a memo, which is recoverable.
        log.error("abandoning the redline: an edit failed in an unexpected way")
        return RedlineOutput(
            content=original, applied=[], skipped=skipped + applied,
            notes=notes + [("Something went wrong while editing, so I have not "
                            "attached a marked-up copy.")],
        )

    for finding in questions:
        try:
            body = finding.question or finding.title
            if finding.options:
                body += "  (" + " / ".join(finding.options) + ")"
            writer.comment(finding.anchor, body)
            commented.append(finding)
        except (AnchorNotFound, AnchorAmbiguous, AnchorSpansParagraphs):
            continue
        except Exception:
            log.exception("could not attach a comment to a %s finding (%s)",
                          finding.category, _reference(finding))

    if not applied and not commented:
        return RedlineOutput(content=original, applied=[], skipped=skipped, notes=notes)

    buffer = BytesIO()
    document.save(buffer)
    content = buffer.getvalue()

    ok, reason = verify(content, expect_revisions=bool(applied))
    if not ok:
        log.error("redline failed verification (%s); falling back to the original", reason)
        return RedlineOutput(
            content=original, applied=[], skipped=skipped + applied,
            notes=notes + ["I could not produce a safe edited copy, so this is a memo only."],
        )

    return RedlineOutput(content=content, applied=applied, skipped=skipped,
                         notes=notes, commented=commented, explained=explained)


# Categories whose fix is its own explanation. A margin full of "corrected the
# spelling" beside every typo buries the three comments that matter.
MECHANICAL = frozenset({
    "typo", "spelling", "punctuation", "formatting", "format", "style",
    "capitalisation", "capitalization", "whitespace", "grammar",
})


def needs_explaining(f: Finding) -> bool:
    """Whether an applied change should carry its reason in the margin.

    A lawyer decides on a substantive change by reading why it was made. In
    Word, that reason has to be beside the change, or they are back in the
    email looking for it. A mechanical fix needs no reason and gets none.
    """
    if not (f.explanation or "").strip():
        return False
    category = (f.category or "").strip().lower()
    if category in MECHANICAL:
        return False
    if (f.edit_kind == "replace" and f.suggested_text
            and f.anchor.strip().lower() == f.suggested_text.strip().lower()):
        return False                  # a defined term's case, and nothing else
    return (f.severity in (Severity.BLOCKER, Severity.SUBSTANTIVE)
            or category == "drafting")


def margin_note(explanation: str, limit: int = 200) -> str:
    """The first sentence or two of an explanation, short enough for a margin."""
    text = " ".join(explanation.split())
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z\"'(])", text)
    note = sentences[0]
    if len(sentences) > 1 and len(note) + 1 + len(sentences[1]) <= limit:
        note += " " + sentences[1]
    if len(note) > limit:
        cut = note[: limit - 1].rsplit(" ", 1)[0].rstrip(",;:")
        note = cut + "…"
    return note


def _explain(writer: RevisionWriter, f: Finding) -> bool:
    """Comment on the change just written. Never fails the edit it annotates."""
    if not needs_explaining(f):
        return False
    try:
        return writer.comment_last_change(margin_note(f.explanation))
    except Exception:  # the change stands; the email has the reason
        log.exception("could not explain a %s change in the margin (%s)",
                      f.category, _reference(f))
        return False


def _partition(findings: list[Finding], autopilot: bool) -> tuple[list[Finding], list[Finding]]:
    candidates, skipped = [], []
    for f in findings:
        if f.suggested_text and (autopilot or f.auto_apply) and _replacement_is_sane(f):
            candidates.append(f)
        else:
            skipped.append(f)
    return candidates, skipped


def _replacement_is_sane(f: Finding) -> bool:
    """Reject a replacement that would clearly destroy text.

    The anchor is the span the writer deletes, so a finding whose anchor carries
    padding context while its replacement is only the corrected fragment would
    delete the surrounding words. That is a document-corrupting bug, and it is
    cheap to refuse here rather than trust every check to get it right.
    """
    anchor = (f.anchor or "").strip()
    replacement = (f.suggested_text or "").strip()
    if not anchor or not replacement:
        return False
    if f.edit_kind == "insert_after":
        # An insertion's text is new language that has nothing to do with the
        # paragraph it follows, so the length comparison below is meaningless
        # here. The anchor is a position, not something being replaced.
        return True
    if anchor == replacement:
        return False
    # A replacement drastically shorter than its anchor, where the anchor is not
    # simply being deleted, means the anchor is carrying context it should not.
    if len(anchor) > 24 and len(replacement) < len(anchor) / 2:
        # Lengths only. The anchor is verbatim privileged document text and
        # must not reach application logs.
        log.warning(
            "refusing suspicious replacement for a %s finding (%s): "
            "anchor %d chars, replacement %d",
            f.category, _reference(f), len(anchor), len(replacement),
        )
        return False
    return True


# --------------------------------------------------------------------------
# Verification. Nothing is attached to an email without passing this.
# --------------------------------------------------------------------------


def verify(content: bytes, expect_revisions: bool = False) -> tuple[bool, str]:
    """Prove the document is still a readable Word file before we send it."""
    try:
        package = zipfile.ZipFile(BytesIO(content))
    except Exception:  # noqa: BLE001
        return False, "not a valid zip package"

    bad = package.testzip()
    if bad is not None:
        return False, f"corrupt entry: {bad}"

    required = {"word/document.xml", "[Content_Types].xml"}
    missing = required - set(package.namelist())
    if missing:
        return False, f"missing parts: {', '.join(sorted(missing))}"

    stories: dict[str, str] = {}
    for name in sorted(package.namelist()):
        if not STORY_PART.match(name):
            continue
        try:
            stories[name] = package.read(name).decode("utf-8", errors="strict")
        except Exception:  # noqa: BLE001
            return False, f"{name} is not readable utf-8"

    if expect_revisions:
        # Every part that can hold a story, not just word/document.xml. The
        # writer edits headers and footers too -- removing a DRAFT marker from a
        # header is one of the cases the product advertises -- and that markup
        # lands in word/header1.xml. Looking only at the body meant a correct
        # header redline was thrown away and the lawyer was told "I could not
        # produce a safe edited copy", unless some unrelated body edit happened
        # to be in the same batch.
        xml = "".join(stories.values())
        if "<w:ins " not in xml and "<w:del " not in xml:
            return False, "no revision markup was written"
        if "<w:delText" not in xml and "<w:del " in xml:
            return False, "deleted runs are missing w:delText"

    try:
        Document(BytesIO(content))
    except Exception as e:  # noqa: BLE001
        return False, f"python-docx could not parse it: {e}"

    # The constraints Word itself enforces on revision markup. Structural
    # parsing succeeding is not the same as the file being valid.
    report = validate(content)
    if not report.ok:
        return False, str(report)

    return True, "ok"


def verify_opens(content: bytes) -> bool:
    """Never email a document we have not proved is still readable."""
    return verify(content)[0]


def revision_authors(content: bytes) -> set[str]:
    """Who the tracked changes in a document are attributed to.

    Across every story part, because another author's revisions live in the
    header as readily as the body, and "never strip pre-existing tracked
    changes" has to be checkable wherever they are.
    """
    try:
        package = zipfile.ZipFile(BytesIO(content))
        xml = "".join(
            package.read(name).decode("utf-8", errors="replace")
            for name in package.namelist()
            if STORY_PART.match(name)
        )
    except Exception:  # noqa: BLE001
        return set()
    return set(re.findall(r'w:author="([^"]+)"', xml))


def _reference(f: Finding) -> str:
    """A stable handle for one finding that carries none of its text.

    Enough to match a log line to a finding in the same run, or to the same
    finding across runs of the same document, without writing a word of the
    client's contract to stdout.
    """
    return hashlib.sha256((f.title or "").encode("utf-8")).hexdigest()[:10]


def _short(text: str, limit: int = 45) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
