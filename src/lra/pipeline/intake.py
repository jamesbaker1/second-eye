"""Turn a raw inbound email into a job, or reject it with a human reason.

This is the UX gate. Everything here is about being forgiving: no command
syntax, no required subject line, no portal. If we can guess, we guess and say
what we guessed.
"""

from __future__ import annotations

import re

from lra.config import settings
from lra.models import InboundEmail, Mode
from lra.pipeline.filetype import (
    REDLINEABLE,
    REVIEWABLE,
    SANDBOX_CONVERTIBLE,
    Kind,
    identify,
    message,
)

# Every hint has to read as an instruction about this document, which is why
# these are phrases rather than words. A bare "memo" matched "Memorandum of
# Understanding" in a subject line, "memorialise the agreed cap" and "see my
# memo from Tuesday", and each of those silently returned an MOU with no
# tracked changes and nothing in the reply explaining the missing attachment.
_MEMO_HINTS = (
    r"don'?t edit", r"do not edit", r"no edits?\b", r"without edits?\b",
    r"just tell me", r"flag only", r"no redline", r"without a redline",
    r"comments only", r"notes only", r"\bmemo only\b", r"just a memo",
    r"send me a memo", r"\bas a memo\b", r"\bin a memo\b",
)
# Narrower than it looks on purpose. "Can you check for typos and also whether
# the indemnity is acceptable?" used to match on the word "typos" alone and
# silently drop the indemnity question, and a review that is smaller than the
# one that was asked for is the expensive direction to be wrong in.
_PROOF_HINTS = (
    r"proof-?read", r"typos only", r"just (?:the )?typos", r"only typos",
    r"formatting only", r"clean up the formatting",
)

# Asking for a comparison. Phrases again: "compare" alone is safe, but a bare
# "changes" appears in almost every covering note ever written.
_COMPARE_HINTS = (
    r"\bcompar(?:e|ison)\b", r"\bblackline\b", r"\bdiff\b",
    r"what(?:'s| has| is| have)? changed", r"what did (?:they|he|she) change",
    r"changes (?:between|since|from) ", r"redline (?:this |it |these )?against",
)
_PREVIOUS_HINTS = (
    r"\b(?:last|previous|prior|earlier) (?:one|version|draft|turn)\b",
    r"the (?:one|version|draft) (?:you|i) (?:sent|saw|reviewed|had)",
    r"\bsince last time\b",
)
# Asking us for a clean copy, which is not the same as telling a client one is
# attached. "Attaching a clean copy" is a claim email_checks tests; this is a
# request, so it needs a verb aimed at us in front of it.
_CLEAN_HINTS = (
    (r"(?:send|give|make|get|need|want|produce|create|prepare|can i have|can you|"
    r"could you|please)\b[^.\n]{0,40}\bclean (?:copy|version)\b"),
    r"\bscrub\b", r"\b(?:strip|remove|clean) (?:out )?(?:the |all )?metadata\b",
    r"\baccept all (?:the |my |your )?changes\b", r"\baccept (?:them|it) all\b",
    # The reply our own footer tells them to send: "clean copy", "clean copy
    # please", "Clean version". It needed a verb in front and fell through to
    # the agent, which with no model answered "something went wrong".
    r"(?:^|\n)\s*(?:a |the )?clean (?:copy|version)\b(?![^.\n]{0,15}\battached\b)",
)

# Asking for the formatting to be repaired rather than reported.
_REPAIR_HINTS = (
    (r"\b(?:fix|repair|sort out|clean up|tidy(?: up)?|normali[sz]e) "
    r"(?:the |its |all the |up the )?(?:formatting|styles|fonts)\b"),
)

# Asking for typed clause numbers, and the references to them, to be put right.
# "renumber" on its own is the reply the review's footer offers. Not when the
# lawyer is saying what they, or the other side, will or will not do.
_RENUMBER_HINTS = (
    r"\brenumber\b",
    (r"\b(?:fix|correct|repair|sort out|tidy(?: up)?|redo)\s+"
     r"(?:the |all the |its |up the )?"
     r"(?:(?:clause|section) numbers|(?:clause |section )?numbering)\b"),
)
# Asking for the signature pack: an execution copy and each party's signature
# page. A request, not a description: "attaching the execution version" and
# "is this ready to sign?" are a document to review, so the phrase needs a verb
# aimed at us in front of it, or a line to itself, as the clean-copy hints do.
_SIGPACK_PHRASE = (
    r"(?:sig(?:nature)?\.?\s+(?:pages?|pack(?:et)?)|execution\s+(?:version|copy|pack))"
)
_SIGPACK_HINTS = (
    # Verbs aimed at us. Not "please" or "sign": "please sign the signature
    # pages and return them" is a lawyer writing to a client with us copied.
    (r"(?:\b(?:send|give)\s+(?:me|us)|\b(?:make|prepare|produce|create|draw\s+up)|"
     r"\b(?:pull|put)\s+together|\bcan\s+(?:i|we)\s+have|\b(?:i|we)\s+need|"
     r"\bi'?d\s+like|\b(?:can|could|would)\s+you\s+(?:make|prepare|produce|create|"
     r"send|do|pull|put|get|sort))"
     r"(?:(?!\battach|\benclos|\bsign(?:ed)?\s)[^.?\n]){0,40}\b" + _SIGPACK_PHRASE + r"\b"),
    r"(?:^|\n)\s*(?:the\s+)?" + _SIGPACK_PHRASE + r"\b(?![^.\n]{0,15}\battached\b)",
    (r"\b(?:get|make|put)\s+(?:this|it|these|them|the\s+\w+)\s+ready\s+(?:to|for)\s+"
     r"(?:sign|signing|signature|execution)\b"),
)
_SOMEONE_ELSE_RENUMBERS = re.compile(
    r"(?:\b(?:we|i|they|he|she|will|shall|not|never)|n't|'ll)\s+(?:\w+\s+){0,2}$",
    re.IGNORECASE,
)

_MEMO_RE = re.compile("|".join(_MEMO_HINTS), re.IGNORECASE)
_RENUMBER_RE = re.compile("|".join(_RENUMBER_HINTS), re.IGNORECASE)
_REPAIR_RE = re.compile("|".join(_REPAIR_HINTS), re.IGNORECASE)
_COMPARE_RE = re.compile("|".join(_COMPARE_HINTS), re.IGNORECASE)
_PREVIOUS_RE = re.compile("|".join(_PREVIOUS_HINTS), re.IGNORECASE)
_CLEAN_RE = re.compile("|".join(_CLEAN_HINTS), re.IGNORECASE)
_SIGPACK_RE = re.compile("|".join(_SIGPACK_HINTS), re.IGNORECASE)
_PROOF_RE = re.compile("|".join(_PROOF_HINTS), re.IGNORECASE)


class Rejection(Exception):
    """Raised with a sentence we are willing to email back verbatim."""


def check_sender(email: InboundEmail) -> None:
    """Refuse anyone the firm has not approved.

    With no allowlist, the firm's own domains are the list. An empty list used
    to mean "anyone", and the agent's address is visible on every email it is
    CC'd on, so a counterparty's reply-all got the firm's review of their own
    draft. Only a deployment with neither setting (a laptop) stays open.
    """
    cfg = settings()
    allow = cfg.allowlist
    addr = email.from_address.lower()
    domain = addr.split("@")[-1]
    if not allow:
        if not cfg.firm_domains.strip() or cfg.is_internal(addr):
            return
        raise Rejection(f"{email.from_address} is outside the firm.")
    if addr not in allow and domain not in allow:
        contact = settings().allowlist_contact.strip()
        raise Rejection(
            "This review agent is limited to an approved list of senders and "
            f"{email.from_address} is not on it yet."
            + (f" Ask {contact} to add you." if contact else "")
        )


def pick_document(email: InboundEmail):
    """Choose the one attachment to review.

    Selection is by content, not by extension, because extensions lie. A file
    we cannot review is rejected with a sentence that says what to do next, and
    never with a generic error: a lawyer who gets "something went wrong" once
    does not send a second document.
    """
    cap = settings().max_attachment_mb * 1024 * 1024
    if not email.attachments:
        raise Rejection(
            "I could not find a document to review. Attach a .docx and I will take "
            'it from there, or reply "help" for everything I can do.'
        )

    classified = [(a, identify(a.content, a.filename)) for a in email.attachments]

    # Inline images, signature logos, contact cards, invites and the like are
    # never the document. A vCard counted as reviewable text and was once
    # chosen over the contract forwarded beside it.
    classified = [(a, k) for a, k in classified if not _is_noise(a)]
    if not classified:
        raise Rejection(
            "The only attachments I found were images. Attach a .docx and I will "
            "review it."
        )

    reviewable = [(a, k) for a, k in classified if _is_reviewable(k)]

    if not reviewable:
        # Report the most helpful single reason rather than a list.
        kinds = [k for _, k in classified]
        for preferred in (Kind.ARCHIVE_BOMB, Kind.LEGACY_DOC, Kind.ENCRYPTED, Kind.RTF, Kind.ODT,
                          Kind.ZIP_NOT_DOCX, Kind.DAMAGED, Kind.EMPTY):
            if preferred in kinds:
                raise Rejection(message(preferred))
        raise Rejection(message(Kind.UNKNOWN))

    oversize = [(a, k) for a, k in reviewable if a.size_bytes > cap]
    reviewable = [(a, k) for a, k in reviewable if a.size_bytes <= cap]
    if oversize:
        # Never silently review the smaller file instead. That produced "Looks
        # good, nothing to flag" about a cover note while the execution copy
        # attached beside it was never opened.
        names = ", ".join(a.filename for a, _ in oversize)
        raise Rejection(
            f"{names} is larger than the {settings().max_attachment_mb} MB I can "
            "accept by email, so I have not reviewed anything rather than review "
            "the wrong file. Send me the section you want checked, or file it to "
            "the matter and tell me the name."
        )

    if len(reviewable) > 1:
        docx = [(a, k) for a, k in reviewable if k is Kind.DOCX]
        if len(docx) == 1:
            return docx[0][0]
        # A clean draft and its blackline is what the other side sends most
        # often, and was refused with "send me the one you want". The one that
        # is not the comparison is the document.
        drafts = [a for a, _ in docx if not _looks_like_a_comparison(a)]
        if docx and len(drafts) == 1:
            return drafts[0]
        names = ", ".join(a.filename for a, _ in reviewable[:4])
        raise Rejection(
            f"There is more than one document attached ({names}). Send me the one "
            "you want reviewed on its own, or send two versions and say "
            '"compare" and I will show you what changed.'
        )
    return reviewable[0][0]


_NOISE_TYPES = ("image/", "text/calendar", "text/vcard", "text/x-vcard",
                "text/directory", "text/html", "application/pkcs7-signature",
                "application/x-pkcs7-signature", "application/ms-tnef")
_NOISE_NAMES = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".vcf", ".ics", ".htm",
                ".html", ".p7s", ".asc", ".sig")
_COMPARISON_NAME = re.compile(
    r"black[- ]?line|\bcompar|\bredline|\bmark(?:ed)?[- ]?up|\bchanges\b|\btracked\b"
    r"|\brl\b|\bvs\.?\b",
    re.IGNORECASE,
)


def _is_noise(a) -> bool:
    name = a.filename.lower()
    return (a.content_type.lower().startswith(_NOISE_TYPES)
            or name.endswith(_NOISE_NAMES) or name == "winmail.dat")


def _looks_like_a_comparison(a) -> bool:
    """A blackline, by its name or by carrying tracked changes."""
    if _COMPARISON_NAME.search(a.filename):
        return True
    from lra.pipeline.redline import revision_authors

    try:
        return bool(revision_authors(a.content))
    except Exception:  # noqa: BLE001 - an unreadable file is not a comparison
        return False


def set_aside(email: InboundEmail, chosen) -> list[str]:
    """The other documents attached, which the reply names as not reviewed."""
    return [a.filename for a in _reviewable(email) if a is not chosen]


def _reviewable(email: InboundEmail) -> list:
    """Attachments that could be a document, in the order they were attached."""
    cap = settings().max_attachment_mb * 1024 * 1024
    return [
        a for a in email.attachments
        if not _is_noise(a)
        and _is_reviewable(identify(a.content, a.filename))
        and a.size_bytes <= cap
    ]


def _is_reviewable(kind: Kind) -> bool:
    """Readable in-process, or convertible to something that is.

    A legacy .doc was refused here with "save it as .docx" even on a machine
    with LibreOffice installed, because the extractor's conversion path sat
    behind a gate that never let the file through to it. The Dockerfile
    installs LibreOffice for exactly these files, so they count as reviewable
    wherever the conversion is on offer, and are refused with the same sentence
    as before where it is not.
    """
    if kind in REVIEWABLE:
        return True
    if kind in SANDBOX_CONVERTIBLE:
        from lra.pipeline.reflow import conversion_available

        return conversion_available()
    return False


def wants_comparison(email: InboundEmail) -> bool:
    """The sender asked, in their own words, to compare versions."""
    if _COMPARE_RE.search(extract_instructions(email)):
        return True
    # The subject only counts when it is theirs and there are two versions to
    # compare. A forwarded "SPA - revised draft and blackline" carrying one
    # document got "I need exactly two documents" instead of a review.
    from lra.pipeline.router import straight_apostrophes

    subject = straight_apostrophes(email.subject).strip()
    if re.match(r"(?:fwd?|fw)\s*:", subject, re.IGNORECASE):
        return False
    return bool(_COMPARE_RE.search(subject)) and len(_reviewable(email)) == 2


def wants_previous_version(email: InboundEmail) -> bool:
    return bool(_PREVIOUS_RE.search(extract_instructions(email)))


def wants_repair(email: InboundEmail) -> bool:
    """Asked to fix the formatting. Only acted on when the agents are
    configured; otherwise the same words fall through to a proofread."""
    return bool(_REPAIR_RE.search(extract_instructions(email)))


def wants_renumber(email: InboundEmail) -> bool:
    """Asked to fix the typed clause numbering and every reference to it."""
    text = extract_instructions(email)
    return any(not (m.group(0).lower() == "renumber"
                    and _SOMEONE_ELSE_RENUMBERS.search(text[max(0, m.start() - 40):m.start()]))
               for m in _RENUMBER_RE.finditer(text))


def wants_signature_pack(email: InboundEmail) -> bool:
    """Asked for an execution copy and signature pages."""
    return bool(_SIGPACK_RE.search(extract_instructions(email)))


# "Send me the deadlines as a calendar", "put the key dates in my diary",
# "ics please". Not "calendar days": that is a unit, not a request.
_CAL = r"(?:calendar|diary|\.?ics|ical|invites?)(?!\s+(?:days?|weeks?|months?|years?|quarters?)\b)"
_CALENDAR_RE = re.compile(
    rf"\b(?:deadlines?|dates|key\s+dates|time\s*limits)\b[^.?!\n]{{0,40}}\b{_CAL}\b"
    rf"|\b(?:calendar|ics|ical)\s+(?:file|invites?|entries|export)\b"
    rf"|\b(?:add|put)\b[^.?!\n]{{0,30}}\b(?:my|the|a|our)\s+(?:calendar|diary)\b"
    rf"(?!\s+(?:days?|weeks?|months?|years?)\b)"
    r"|^\s*(?:an?\s+|the\s+)?\.?ics\b(?=\s*(?:file|please|pls|thanks|[.!?]|$))",
    re.IGNORECASE,
)


def wants_calendar(email: InboundEmail) -> bool:
    """Asked for the document's deadlines as a calendar file."""
    return bool(_CALENDAR_RE.search(extract_instructions(email)))


def wants_clean_copy(email: InboundEmail) -> bool:
    return bool(_CLEAN_RE.search(extract_instructions(email)))


_VERSION_MARK = re.compile(
    r"(?:\bv(?:ersion)?[ ._-]?|\bdraft[ ._-]?|\brev(?:ision)?[ ._-]?|\()(\d{1,3})\)?",
    re.IGNORECASE,
)


def _version_number(filename: str) -> int | None:
    found = _VERSION_MARK.findall(filename.rsplit(".", 1)[0])
    return int(found[-1]) if found else None


def _last_saved(content: bytes) -> str | None:
    """When a Word file says it was last saved. ISO strings sort correctly."""
    import zipfile
    from io import BytesIO

    try:
        core = zipfile.ZipFile(BytesIO(content)).read("docProps/core.xml").decode(
            "utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - not a Word file, or no properties
        return None
    m = re.search(r"<dcterms:modified[^>]*>([^<]+)<", core)
    return m.group(1).strip() if m else None


def pick_pair(email: InboundEmail):
    """The two versions to compare: (earlier, later, how we decided).

    Which is which matters more than anything else here, because a comparison
    run backwards shows every insertion as a deletion and reads as the other
    side having conceded the points they actually took. So the reply always
    says how the order was decided, and the evidence is used strongest first.
    """
    documents = _reviewable(email)
    if len(documents) != 2:
        raise Rejection(
            "To compare versions I need exactly two documents attached, the "
            f"earlier and the later. I found {len(documents)}."
        )
    first, second = documents

    numbers = _version_number(first.filename), _version_number(second.filename)
    if None not in numbers and numbers[0] != numbers[1]:
        ordered = (first, second) if numbers[0] < numbers[1] else (second, first)
        return (*ordered, "going by the version numbers in the filenames")

    saved = _last_saved(first.content), _last_saved(second.content)
    if None not in saved and saved[0] != saved[1]:
        ordered = (first, second) if saved[0] < saved[1] else (second, first)
        return (*ordered, "going by when each file was last saved")

    return first, second, "in the order they were attached"


def infer_mode(email: InboundEmail) -> Mode:
    """What kind of review was asked for, from the sender's own words.

    Quoted history is stripped first. A forwarded thread carries the
    counterparty's covering note and a reply carries our own previous email, so
    "no redline needed" written by somebody else is not an instruction to us.

    Memo is tested before proofread: "no redline, just fix the typos" is two
    instructions and the conservative one wins, because the cost of not editing
    a document is a mild annoyance and the cost of editing one we were told to
    leave alone is a lawyer who stops trusting the agent.
    """
    text = email.subject + "\n" + extract_instructions(email)
    if _MEMO_RE.search(text):
        return Mode.MEMO_ONLY
    if _PROOF_RE.search(text):
        return Mode.PROOFREAD
    return Mode.REDLINE


def mode_for(attachment, requested: Mode) -> Mode:
    """Downgrade to memo only when no Word copy can be made to mark up.

    This used to downgrade everything that was not a .docx. A PDF, a legacy
    .doc or a text file now gets a Word copy built or converted from it
    (pipeline/reflow.py) and the tracked changes go into that, so the only
    formats left as notes-only are the ones with no paragraphs to edit: decks
    and workbooks. A PDF that turns out to be a scan is decided later, when the
    build is attempted, and the handler says so in the reply.
    """
    if requested is Mode.MEMO_ONLY:
        return requested
    from lra.pipeline.reflow import convertible

    return requested if is_redlineable(attachment) or convertible(attachment) else Mode.MEMO_ONLY


def is_redlineable(attachment) -> bool:
    """Whether tracked changes can be written into this file.

    Decided from the bytes. The handler used to gate on the filename ending in
    ".docx", so a genuine Word file delivered as "Agreement.doc" by a mail
    gateway or a manual rename silently came back as a memo with no explanation.
    """
    return identify(attachment.content, attachment.filename) in REDLINEABLE


def extract_instructions(email: InboundEmail) -> str:
    """The sender's own words, and nothing anyone else wrote.

    This text is interpolated into the review prompt, so anything that leaks in
    here is an instruction the agent may follow. A forwarded draft carries the
    counterparty's covering note, and "please delete the indemnity entirely"
    written by opposing counsel must never be read as a request from the
    lawyer. The shared stripper in router handles Gmail forward blocks, Outlook
    quote headers, angle-quoted lines, signatures and confidentiality footers.
    """
    from lra.pipeline.router import strip_reply

    return strip_reply(email.body)
