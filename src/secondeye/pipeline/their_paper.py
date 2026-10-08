"""Whose paper is this: the firm's own draft, or the other side's.

DECISIONS #4 makes "forward the counterparty's draft" a first-class way in, and
open question O17 asks what the review should do with one. A review written
for the firm's own draft is the wrong review of theirs. Forty tracked changes
fixing their typos and their "shall"/"will" drift is noise in a document the
lawyer is about to negotiate, and marking up their house style is not the
firm's job. What the lawyer needs from their paper is where it departs from
the firm's positions, and what to say back.

Detection is deterministic and conservative, in this order:

  1. The lawyer's own words ("their draft", "opposing counsel's", or "our
     draft" the other way). Only the words they wrote: the forwarded message
     below them is the other side's, and "please find our draft attached" from
     opposing counsel must not read as the lawyer claiming it.
  2. A forward of an email from someone outside the firm, with the document
     attached.
  3. The document's own metadata: author, last saved by, or tracked-change
     authors, when one of them is an email address outside the firm. A name
     cannot be judged ("J Smith" could be anyone), so names are never guessed
     at.

A document already sent (BCC_SENT) is never theirs: the lawyer is sending it,
and the covering note to the client talking about "their draft" is not about
the attachment.
"""

from __future__ import annotations

import logging
import re
import zipfile
from dataclasses import dataclass
from io import BytesIO

from lxml import etree

from secondeye.config import settings
from secondeye.models import Attachment, Finding, InboundEmail, Mode, ReviewResult, Severity
from secondeye.pipeline.identity import EntryMode, is_agent
from secondeye.pipeline.router import strip_reply
from secondeye.pipeline.validate import STORY_PART

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Provenance:
    """Whether the document is the other side's, and why we think so."""

    theirs: bool
    # "words", "forward" or "metadata"; empty when nothing said either way.
    source: str = ""
    # Why, in words that finish "I read this as their draft because ...".
    reason: str = ""


OURS = Provenance(False)

_APOS = r"(?:'|’)"

# The lawyer saying it is theirs. Each phrase has to name the document as the
# other side's; "before it goes to opposing counsel" is about our draft, so the
# bare names of the other side are not enough.
_THEIRS = re.compile(
    r"\b(?:"
    r"their (?:draft|paper|mark-?up|turn|version|form|first draft)"
    r"|on their paper"
    rf"|(?:the )?other side{_APOS}s (?:draft|paper|mark-?up|version|turn|comments)"
    r"|from the other side"
    rf"|opposing counsel{_APOS}s"
    r"|from opposing counsel"
    rf"|(?:the )?counterparty{_APOS}s (?:draft|paper|mark-?up|version)"
    r"|from the counterparty"
    r")\b",
    re.IGNORECASE,
)

# The lawyer saying it is ours. Wins over "their" when both appear only if the
# "their" phrase is a negation of this one; otherwise both together is read as
# ambiguous and the evidence decides.
_OURS = re.compile(
    r"\b(?:"
    r"(?:our|my) (?:own )?(?:draft|paper|mark-?up|version|form|template|turn)"
    r"|(?:we|i) drafted"
    r"|not their (?:draft|paper)"
    r")\b",
    re.IGNORECASE,
)

_NOT_THEIRS = re.compile(r"\bnot their (?:draft|paper)\b", re.IGNORECASE)

# A forward, as the lawyer's client marks one. Outlook's "-----Original
# Message-----" separator is also used for replies, so on its own it is not a
# forward; the "FW:" subject is what says so.
_FORWARD_SUBJECT = re.compile(r"^\s*(?:(?:re|aw|sv)\s*:\s*)*(?:fwd?|fw)\s*:", re.IGNORECASE)
_FORWARD_MARKER = re.compile(
    r"-{3,}\s*Forwarded message|Begin forwarded message:", re.IGNORECASE)
_FROM_LINE = re.compile(r"^\s*[*>]*\s*From:\*?\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_ADDRESS = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")

_CORE = "docProps/core.xml"
_CORE_FIELDS = {
    "{http://purl.org/dc/elements/1.1/}creator": "its author is recorded as",
    "{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}"
    "lastModifiedBy": "it was last saved by",
}
_REVISION_AUTHOR = re.compile(r'<w:(?:ins|del|moveFrom|moveTo)\b[^>]*\bw:author="([^"]+)"')


# The modes a their-paper review changes. A proofread of their draft is asked
# for in so many words, and their typos are exactly what was asked for; a
# question is answered as asked.
APPLIES_TO = (Mode.REDLINE, Mode.MEMO_ONLY)

DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def detect(email: InboundEmail, content: bytes | None, entry: EntryMode,
           mode: Mode = Mode.REDLINE) -> Provenance:
    """Whose paper the attached document is. See the module docstring."""
    if entry is EntryMode.BCC_SENT or mode not in APPLIES_TO:
        return OURS

    said = from_words(strip_reply(email.body))
    if said is not None:
        return said

    mine = _ours(email)
    sender = forwarded_from(email)
    if sender and _outside(sender, mine):
        return Provenance(True, "forward", f"you forwarded it from {sender}")

    if content:
        for label, address in metadata_addresses(content):
            if _outside(address, mine):
                return Provenance(True, "metadata", f"{label} {address}")
    return OURS


def from_words(text: str) -> Provenance | None:
    """What the lawyer said about whose draft it is, or None if nothing clear."""
    if not text:
        return None
    if _NOT_THEIRS.search(text):
        return Provenance(False, "words", "you said so")
    theirs, ours = bool(_THEIRS.search(text)), bool(_OURS.search(text))
    if theirs and not ours:
        return Provenance(True, "words", "you said so")
    if ours and not theirs:
        return Provenance(False, "words", "you said so")
    return None


def forwarded_from(email: InboundEmail) -> str | None:
    """The address of whoever sent the message the lawyer forwarded, if this
    is a forward and the header gives an address. The first forwarded header
    is the most recent message, which is the one the attachment came on."""
    body = email.body or ""
    if not (_FORWARD_SUBJECT.match(email.subject or "") or _FORWARD_MARKER.search(body)):
        return None
    # The lawyer's own words never hold a "From:" line with an address in
    # them: strip_reply ends their words at exactly that line. So the first
    # one in the body is the forwarded header.
    m = _FROM_LINE.search(body)
    if not m:
        return None
    address = _ADDRESS.search(m.group(1))
    return address.group(0).lower() if address else None


def metadata_addresses(content: bytes) -> list[tuple[str, str]]:
    """Email addresses in the document's author fields and tracked-change
    authors, each with the words that say where it was found. Names are left
    out: they cannot be told apart from a colleague's."""
    try:
        package = zipfile.ZipFile(BytesIO(content))
        names = package.namelist()
    except Exception:  # noqa: BLE001 - not a Word file, nothing to read
        return []
    found: list[tuple[str, str]] = []
    if _CORE in names:
        try:
            root = etree.fromstring(package.read(_CORE))
            for tag, label in _CORE_FIELDS.items():
                el = root.find(tag)
                if el is not None and el.text:
                    found += [(label, a.lower()) for a in _ADDRESS.findall(el.text)]
        except Exception:
            log.debug("could not read the document properties", exc_info=True)
    for name in names:
        if not STORY_PART.match(name):
            continue
        try:
            xml = package.read(name).decode("utf-8", errors="replace")
        except Exception:
            log.debug("could not read %s", name, exc_info=True)
            continue
        for author in _REVISION_AUTHOR.findall(xml):
            found += [("its tracked changes are by", a.lower())
                      for a in _ADDRESS.findall(author)]
    return found


def _ours(email: InboundEmail) -> set[str]:
    """Addresses that are never the other side, even outside the firm's
    domains: the lawyer (production's lawyer writes from a personal address)
    and everyone on the allowlist."""
    return {email.from_address.lower(), *settings().allowlist}


def _outside(address: str, ours: set[str]) -> bool:
    address = address.lower()
    return not (address in ours or is_agent(address) or settings().is_internal(address))


# --------------------------------------------------------------------------
# What changes about the review on their paper.
# --------------------------------------------------------------------------

COSMETIC = (Severity.STYLE, Severity.FORMATTING)


def withhold_cosmetic(findings: list[Finding]) -> tuple[list[Finding], int]:
    """The findings to report on their paper, and how many cosmetic ones were
    left out.

    Their typos and their house style are not ours to mark up. Removing them
    here, before the writer runs, is what guarantees none becomes a tracked
    change in their text: the redline the session wrote from its own list is
    compared against the local writer's and discarded if it differs. A blocker
    is never withheld, whatever it is about.
    """
    kept = [f for f in findings if f.severity not in COSMETIC]
    return kept, len(findings) - len(kept)


def points(findings: list[Finding]) -> list[Finding]:
    """The points to push back on: every substantive finding, in order. These
    are the issues list's rows and the number in the verdict."""
    return [f for f in findings if f.severity is Severity.SUBSTANTIVE]


def note(provenance: Provenance, withheld: int) -> str:
    """One line saying the review was read as their paper, and why, so a
    wrong guess is visible and can be corrected."""
    line = f"I read this as their draft, because {provenance.reason}, so I have not marked up their wording"
    if withheld:
        line += (f" ({withheld} typo and style point{'s' if withheld != 1 else ''} "
                 "left alone)")
    line += "."
    if provenance.source != "words":
        line += ' If it is yours, send it again saying "our draft".'
    return line


def settle(result: ReviewResult, provenance: Provenance, filename: str,
           max_bytes: int) -> tuple[list[str], list[Attachment]]:
    """Make a finished review of their paper into what the lawyer gets: the
    cosmetic findings withheld (before the writer runs), the flag set, and the
    issues list built. Returns the notes for the reply and the attachments.
    Does nothing on the firm's own paper."""
    if not provenance.theirs:
        return [], []
    from secondeye.pipeline import issues_list

    result.their_paper = True
    result.findings, withheld = withhold_cosmetic(result.findings)
    notes = [note(provenance, withheld)]
    rows = points(result.findings)
    content = issues_list.build(rows, filename) if rows else None
    if content is None or len(content) > max_bytes:
        return notes, []
    listed = issues_list.name(filename)
    n = len(rows)
    what = f"the {n} points" if n != 1 else "the point"
    notes.append(f"{listed} has {what} as a table: what their draft says, our "
                 "position, and a proposed response.")
    return notes, [Attachment(filename=listed, content_type=DOCX_TYPE,
                              size_bytes=len(content), content=content)]
