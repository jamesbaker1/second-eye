"""Compose the reply.

This is the product surface. A lawyer sees this email and nothing else, often
on a phone, in a preview pane, at the moment before they hit send. If it is not
instantly readable it does not matter how good the review was.

Four rules:

  1. The verdict is the first line. No greeting, no throat-clearing.
  2. Blockers come before everything else. Their consequence is stated.
  3. Signal is separated from noise. A style nit must never sit in the same
     list as an uncapped indemnity, because then neither gets read.
  4. Nothing is longer than it needs to be. The minor findings are counted,
     and only the first few are named. This used to say "collapsed", which
     relied on <details>; see MINOR_SHOWN for why that did not survive contact
     with Outlook.
  5. Anything that shapes how the rest should be read - which review ran, why
     a document is or is not attached - is said near the top, not appended in
     grey at the bottom.
"""

from __future__ import annotations

import html
import re
from urllib.parse import quote

from lra.models import (
    Attachment,
    Finding,
    InboundEmail,
    Mode,
    OutboundEmail,
    ReviewResult,
    Severity,
)
from lra.pipeline import deadlines
from lra.pipeline.identity import EntryMode

DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

MINOR = (Severity.STYLE, Severity.FORMATTING)

# How many minor findings are named before the rest are counted. Rule 4 of this
# module used to be enforced by <details>, which Outlook on Windows renders
# through the Word HTML engine: unknown elements are dropped and their children
# shown, so the collapse never happened in the client most firms actually run
# and fourteen style nits printed at full length beside one blocker.
MINOR_SHOWN = 3

# The outbound ceiling, which is NOT the inbound one. Postmark rejects a message
# over 10 MB and counts the base64-encoded attachment, roughly a third larger
# than the bytes; MAX_ATTACHMENT_MB defaults to 25. So a document we are willing
# to accept can be one we cannot send back, and attaching it anyway made the
# send fail, which the handler could only report as a review that did not run.
MAX_ATTACHMENT_BYTES = 7 * 1024 * 1024


class _Identities:
    """A set of objects compared by identity, holding them alive.

    Findings are pydantic models and so unhashable, which is why this code
    tracked them by `id()`. That idiom is only safe while something else keeps
    every object alive: CPython reuses a freed address, so an id still in the
    set can belong to a different object. The same pattern in ooxml.py, over
    lxml proxies that really do get collected, produced a failure that passed
    on one machine and failed in CI on the same commit.

    These objects are in fact held by locals in compose(), so the old code was
    correct. This makes it correct by construction instead of by coincidence.
    """

    __slots__ = ("_ids", "_kept")

    def __init__(self, items=()):
        self._ids: set[int] = set()
        self._kept: list = []
        for item in items:
            self.add(item)

    def add(self, item) -> None:
        self._ids.add(id(item))
        self._kept.append(item)

    def __contains__(self, item) -> bool:
        return id(item) in self._ids

    def __bool__(self) -> bool:
        return bool(self._ids)


def compose(
    inbound: InboundEmail,
    result: ReviewResult,
    redlined: bytes | None,
    filename: str,
    applied: list[Finding],
    skipped: list[Finding],
    entry: EntryMode = EntryMode.FORWARD,
    external: list[str] | None = None,
    notes: list[str] | None = None,
    commented: list[Finding] | None = None,
    explain_mode: bool = True,
    extra_attachments: list[Attachment] | None = None,
    since: tuple | None = None,
    numbers: list[int | None] | None = None,
    tap_address: str | None = None,
    dates: list | None = None,
    labels: list[str | None] | None = None,
    their_paper: bool = False,
) -> OutboundEmail:
    """The review reply.

    `labels` are the letters shown beside the findings `dismissable` returns,
    in its order, for "B is fine". Without them nothing is lettered.

    `their_paper` is a review of the other side's draft (their_paper.py): the
    verdict counts points to push back on rather than saying whether to send.

    `tap_address` is where the HTML body's one-tap links write to: the agent's
    address with this conversation's token in its plus tag (identity.
    tap_address). Without it no link is drawn and the email is as it was.
    """
    already_sent = entry is EntryMode.BCC_SENT
    blockers = [f for f in result.findings if f.severity is Severity.BLOCKER]

    applied_set = _Identities(applied)
    # Anything it can fix the moment someone answers a one-word question. This
    # is the difference between a review that creates work and one that takes
    # work away, so it gets its own section directly under the blockers.
    asks = [
        f for f in result.findings
        if f.question and f not in applied_set
    ]
    ask_set = _Identities(asks)
    judgment = [
        f for f in result.findings
        if f.severity is Severity.SUBSTANTIVE
        and f not in applied_set and f not in ask_set
    ]
    minor = [
        f for f in result.findings
        if f.severity in MINOR and f not in applied_set and f not in ask_set
    ]

    # Computed from the lists this email actually renders, not from every
    # finding. "read the substantive notes" used to print above a message with
    # no such section, because the one substantive finding had been written as
    # a tracked change and so was filtered out of it.
    verdict = (
        _sent_verdict(blockers, result.findings) if already_sent
        else _verdict(blockers, judgment, asks, minor, applied)
    )
    if result.cut_short and not blockers:
        # A blocker found in half a document is still a blocker, so that
        # verdict stands. Every other verdict is a statement about the whole
        # document, and a review that stopped early cannot make one.
        verdict = (
            "Already sent. Partial review: no errors in what I got to."
            if already_sent else "Partial review: nothing blocking in what I got to."
        )
    if their_paper and not already_sent:
        verdict = _their_verdict(blockers, result.findings, result.cut_short)

    attachments, size_note = _attachment(redlined, filename, result.mode)
    # Anything made alongside the redline: the annotated PDF, the comparison
    # with the version reviewed last time. Already sized by the caller.
    attachments = attachments + list(extra_attachments or [])

    # `skipped` is what the redliner did not write. A finding the reviewer had
    # marked auto_apply is one it meant to write, so finding it here means an
    # edit was attempted and abandoned. Said beside the finding, because
    # otherwise it reads as something nobody ever tried.
    unplaced = _Identities()
    if result.mode is not Mode.MEMO_ONLY:
        unplaced = _Identities(
            f for f in skipped
            if f.auto_apply and f.suggested_text and f not in applied_set
        )

    all_notes = _notes(result, filename, attachments, size_note, notes or [],
                       explain_mode)

    sections = _sections(
        verdict, result, blockers, applied, asks, judgment, minor,
        already_sent, external or [], all_notes, commented or [], unplaced,
        since=since, numbers=numbers, their_paper=their_paper,
        labels=_label_map(result, applied, labels),
    )
    # The document's dates and time limits, after the findings (deadlines.py).
    sections = deadlines.place(sections, dates)

    # Not on a document already sent, where there is nothing to answer or
    # accept, nor on notes only, where there is no copy for an answer to land in.
    tap = None
    if tap_address and not already_sent and result.mode is not Mode.MEMO_ONLY:
        tap = _tapper(tap_address, _subject(inbound.subject, verdict))

    return OutboundEmail(
        to=[inbound.from_address],
        subject=_subject(inbound.subject, verdict),
        text_body=_as_text(sections),
        html_body=_as_html(sections, tap),
        in_reply_to=inbound.message_id,
        thread_id=inbound.thread_id,
        attachments=attachments,
    )


def _attachment(redlined: bytes | None, filename: str,
                mode: Mode) -> tuple[list[Attachment], str | None]:
    """The marked-up copy, unless it is too large to send.

    Returns the note to print when it could not go, because an attachment the
    lawyer was promised and did not get has to be explained: silence reads as
    the firm's mail gateway having stripped it.
    """
    if not redlined or mode is Mode.MEMO_ONLY:
        return [], None
    if len(redlined) > MAX_ATTACHMENT_BYTES:
        megabytes = len(redlined) / (1024 * 1024)
        return [], (
            f"The marked-up copy came to {megabytes:.0f} MB, which is more than I "
            "can attach to an email, so these are the findings on their own. Send "
            "me the part you want marked up and I will redline that."
        )
    return [
        Attachment(
            filename=_redline_name(filename),
            content_type=DOCX_TYPE,
            size_bytes=len(redlined),
            content=redlined,
        )
    ], None


def _notes(result, filename, attachments, size_note, redline_notes,
           explain_mode=True) -> list[str]:
    """Everything the lawyer needs in order to read the rest correctly.

    Which review ran, and why a document is or is not attached. Both used to be
    invisible: a proofread and a full review produced identically shaped
    emails, and a redline that could not be written simply arrived with no
    attachment and nothing said about it.
    """
    out = []
    if explain_mode:
        mode_note = _mode_note(result.mode, filename)
        if mode_note:
            out.append(mode_note)
    if result.cut_short:
        # First, before anything about attachments: it changes how every
        # finding below should be read. A partial review that looks complete
        # is the one thing this email must never be.
        out.append(
            "I ran out of time before the end, so this is not a full review. "
            "Names, dates, numbers and references were all checked; the legal "
            "points cover only what I got to."
        )
    if size_note:
        out.append(size_note)
    elif (result.mode in (Mode.REDLINE, Mode.PROOFREAD)
          and not attachments and result.findings):
        # A clean document is excluded deliberately: "nothing to flag" already
        # says why there is nothing attached, and repeating it is noise.
        #
        # Two different truths, and neither may be stated where the other
        # holds. If nothing here was ever a candidate for a tracked change then
        # the reason is the findings themselves. If something was, the edit
        # exists somewhere and only the copy is missing - including on the
        # handler's path where the review succeeded and the send failed - so
        # the note says that and claims nothing about safety.
        if any(f.auto_apply and f.suggested_text for f in result.findings):
            out.append(
                "I could not give you an edited copy this time, so these are "
                "yours to make."
            )
        else:
            out.append(
                "I did not attach an edited copy, because none of this was safe "
                "for me to change on my own."
            )
    return out + list(redline_notes)


def _mode_note(mode: Mode, filename: str) -> str | None:
    """One line naming the review that ran, whenever it was not the full one.

    docs/ux.md's rule is that if we guess what was wanted, we say what we
    guessed. A memo-only or proofread run is a downgraded review and the lawyer
    had no way to tell they had been given one.
    """
    # Only reached when the mode was actually requested or forced by the
    # format. A review that failed partway also ends up in MEMO_ONLY, purely to
    # suppress the attachment, and telling that lawyer "you asked me not to
    # edit the document" puts words in their mouth about a request they never
    # made. That path passes explain_mode=False; its summary already says the
    # review is incomplete.
    if mode is Mode.MEMO_ONLY:
        extension = _extension(filename)
        if extension in _NEVER_REDLINED:
            # Guessed from the name, because this function only ever sees the
            # name. The downgrade itself was decided on the bytes in
            # intake.mode_for; this sentence only explains it. Only decks and
            # workbooks are left here: a PDF, a .doc or a text file now gets
            # its changes in a Word copy, and when that copy could not be
            # made the handler says why in a note of its own.
            return (
                f"I cannot put tracked changes into a .{extension} file, so these "
                "are notes only."
            )
        return "Notes only, as you asked; the document is untouched."
    if mode is Mode.PROOFREAD:
        return "Proofread only, as you asked: wording and formatting, not substance."
    return None


# Formats with no paragraphs to write a tracked change into, and no Word copy
# worth building from them.
_NEVER_REDLINED = {"pptx", "ppt", "xlsx", "xls", "csv"}


def _extension(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


# --------------------------------------------------------------------------
# Structure. Built once, rendered twice.
# --------------------------------------------------------------------------


def dismissable(result: ReviewResult, applied: list[Finding]) -> list[Finding]:
    """The findings a review shows that are not tracked changes, in the order
    it shows them: blockers, "Your call", quick questions, the minor points it
    names. Each gets a letter, so "B is fine" can dismiss one."""
    applied_set = _Identities(applied)
    shown = [f for f in result.findings if f not in applied_set]
    blockers = [f for f in shown if f.severity is Severity.BLOCKER]
    asks = [f for f in shown if f.question]
    ask_set = _Identities(asks)
    judgment = [f for f in shown if f.severity is Severity.SUBSTANTIVE and f not in ask_set]
    blocker_set = _Identities(blockers)
    questions = [f for f in asks if f not in blocker_set]
    minor = [f for f in shown if f.severity in MINOR and f not in ask_set]
    return blockers + judgment + questions + minor[:MINOR_SHOWN]


def _label_map(result, applied, labels) -> dict[int, str]:
    if not labels:
        return {}
    shown = dismissable(result, applied)
    return {id(f): x for f, x in zip(shown, labels) if x}


def _sections(verdict, result, blockers, applied, asks, judgment, minor,
              already_sent, external, notes, commented, unplaced,
              since=None, numbers=None, labels=None,
              their_paper=False) -> list[tuple]:
    """The email, in the order a partner reads it on a phone.

    Verdict, then what must be fixed, then what needs their call, then what was
    done. Written like a junior associate's note: where, what, what to do, and
    nothing about how the review works. A blocker that carried a question used
    to be moved to a questions list below the tracked changes with its
    explanation dropped, so "2 blockers" sat over an email with no blockers in
    it; every blocker is now in the first list, with its question beside it.
    """
    out: list[tuple] = [("verdict", verdict)]
    labels = labels or {}

    def lettered(f, text: str) -> str:
        """ "B. Cl. 5: ..." for a finding with a letter."""
        x = labels.get(id(f))
        return f"{x}. {text}" if x else text

    summary = _useful_summary(result.summary)
    if summary and result.mode is Mode.QUESTION:
        # They asked something. The answer is the point of the email.
        out.append(("summary", summary))
        summary = ""

    if since:
        # Directly under the verdict. PRODUCT.md: the most useful review is
        # "what changed since the version you sent", so on a second look at a
        # document it is the first thing after the one-line answer.
        heading, line, items = since
        out.append(("since", heading, line, items))

    if already_sent and external:
        # Not a review, an incident report. The next action is a correction to
        # the client, so say where it went before anything else.
        out.append(("alert", "This already went to " + ", ".join(external) + "."))

    if notes:
        # Which review ran and what is or is not attached shapes how the rest
        # is read, so it sits above the findings, in body text.
        out.append(("notes", "", [(n, "") for n in notes]))

    if blockers:
        heading = ("Wrong in what you sent" if already_sent
                   else "Errors in their draft" if their_paper
                   else "Fix before sending")
        out.append(("list", heading, [
            (lettered(f, _where_title(f)), _blocker_detail(f, unplaced), _option_links(f),
             f.question or "")
            for f in blockers
        ]))

    if judgment:
        out.append(("list", "To push back on" if their_paper else "Your call", [
            (lettered(f, _where_title(f)), _detail(f, unplaced)) for f in judgment
        ]))

    blocker_set = _Identities(blockers)
    questions = [f for f in asks if f not in blocker_set]
    if questions:
        label = "Quick questions"
        if commented:
            # Say where they are. A question in an email makes the lawyer hunt
            # for the clause; a comment sits beside it.
            label += " (also commented in the document)"
        out.append((
            "asks",
            label,
            [(lettered(f, _located(f, f.question or f.title)), _options_for(f),
              _option_links(f))
             for f in questions],
        ))

    if applied:
        out.append(("list", "Tracked in the attached copy",
                    _applied_items(applied, numbers)))

    if minor:
        # Counted, and only the first few named. A capitalisation nit must
        # never take the same space as a liability cap.
        out.append((
            "minor",
            f"{len(minor)} minor point{'s' if len(minor) != 1 else ''}",
            [(lettered(f, _where_title(f)), "") for f in minor[:MINOR_SHOWN]],
            max(0, len(minor) - MINOR_SHOWN),
        ))

    if summary:
        # Last, and only what the findings do not already say: what could not
        # be checked, or the one piece of context behind them. It used to open
        # the email by restating the verdict in three sentences.
        out.append(("summary", summary))

    footer, phrases = _footer(result, blockers, asks, judgment, applied, numbers,
                              already_sent)
    footer = _with_dismissal(footer, [x for x in labels.values()])
    if (not already_sent and result.mode is not Mode.MEMO_ONLY
            and any(f.category == "numbering" for f in result.findings)):
        footer = (footer + " " + RENUMBER_OFFER).strip()
        phrases = [*phrases, "renumber"]
    if footer:
        out.append(("footer", footer, phrases))
    return out


# The numbering check fired: the fix for it, and for every reference to it, is
# one reply away (pipeline/renumber.py).
RENUMBER_OFFER = 'Reply "renumber" and I\'ll fix the numbering and every reference to it.'


def _with_dismissal(footer: str, letters: list[str]) -> str:
    """One clause, only when something is lettered. The example is never a
    one-tap link: pressing it would dismiss a point nobody chose."""
    if not letters:
        return footer
    first = min(letters)
    if not footer:
        return f'Reply "{first} is fine" to dismiss a point.'
    return footer.rstrip(".") + f', or "{first} is fine" to dismiss a point.'


def _where(finding) -> str:
    return (getattr(finding, "where", "") or "").strip()


def _located(finding, text: str) -> str:
    """ "Cl. 5: ..." when we know the clause and the text does not say it."""
    where = _where(finding)
    if not where or where.lower() in text.lower():
        return text
    return f"{where[0].upper()}{where[1:]}: {text}"


def _where_title(finding) -> str:
    return _located(finding, finding.title)


def _blocker_detail(finding, unplaced) -> str:
    """The question when one would settle it, the consequence when not."""
    if finding.question:
        options = _options_for(finding)
        return finding.question + (f" ({options})" if options else "")
    return _detail(finding, unplaced)


# The model's summary opening with the verdict the first line already gave.
_RESTATES_VERDICT = re.compile(
    r"^\s*(?:this (?:document )?is |it is |the document is )?"
    r"(?:not )?(?:safe|ready|fine|good) to (?:send|go)\b[^.]*\.\s*"
    r"|^\s*(?:not safe|looks good|nothing to flag)[^.]*\.\s*",
    re.IGNORECASE,
)


def _useful_summary(summary: str) -> str:
    text = _RESTATES_VERDICT.sub("", (summary or "").strip()).strip()
    return text


def _footer(result, blockers, asks, judgment, applied, numbers,
            already_sent) -> tuple[str, list[str]]:
    """The one next step, in words that work if typed back. Nothing when there
    is nothing to do: a clean document ends at its verdict.

    Also the quoted replies in it that are safe to send as they stand, which
    the HTML body turns into one-tap links. A bare "undo" is never one: on its
    own it reverses everything."""
    if already_sent:
        if blockers or judgment:
            return ("Reply and I will prepare a corrected copy you can send "
                    "as a follow-up."), []
        return "", []
    if result.mode is Mode.MEMO_ONLY:
        return ("Want any of these made? Reply and say which."
                if (blockers or judgment or asks) else ""), []
    parts: list[str] = []
    phrases: list[str] = []
    if asks:
        # In the order the questions are shown: blockers first.
        blocker_set = _Identities(blockers)
        shown = [f for f in asks if f in blocker_set] + [f for f in asks if f not in blocker_set]
        example = _answer_example(shown)
        ask_set = _Identities(asks)
        if (example and len(shown) <= 3 and all(f.options for f in shown)
                and all(f in ask_set for f in blockers)):
            # The whole job in one reply. The example answers every question,
            # and answering them settles every blocker, so answers plus "clean
            # copy" is a document ready to send. Offered as two replies, it
            # was two round trips from a lawyer with no time for one.
            done = f'Reply "{example}; clean copy" for a clean copy with your answers in'
            phrases.append(f"{example}; clean copy")
            if applied:
                first = next((n for n in numbers or [] if n), None)
                undo = f'"undo {first}"' if first else '"undo" and the number'
                return f"{done}. Put {undo} first to drop one of my changes.", phrases
            phrases.append(example)
            return f'{done}, or "{example}" to see them tracked first.', phrases
        parts.append(f'Reply with the answers, e.g. "{example}".' if example
                     else "Reply with the answers.")
        if example:
            phrases.append(example)
    if applied:
        first = next((n for n in numbers or [] if n), None)
        undo = f'"undo {first}" to reverse one' if first else '"undo" and which one'
        parts.append(f'Reply "clean copy" to accept my changes, or {undo}.')
        phrases.append("clean copy")
        if first:
            phrases.append(f"undo {first}")
    elif judgment and not asks:
        parts.append("Reply in plain English with anything you want changed.")
    return " ".join(parts), phrases


def _answer_example(asks) -> str:
    """A reply that would actually work, built from the first options offered."""
    picks = [f.options[0] for f in asks if f.options][:3]
    return "; ".join(picks)


# Above this many tracked changes, the mechanical ones are counted by kind
# rather than listed. Forty-one titles under "I made 41 tracked changes" is a
# list nobody reads, and the three substantive edits in it are lost.
GROUP_APPLIED_ABOVE = 8
# A numbered change, or a lettered finding: its own bullet.
_NUMBERED = re.compile(r"^(?:\d+|[A-Z])\. ")

_CATEGORY_PLURAL = {
    "defined-term": "defined-term corrections",
    "cross-reference": "cross-reference corrections",
    "numbering": "numbering corrections",
    "date": "date-format corrections",
    "amount": "amount corrections",
    "party-name": "party-name corrections",
    "placeholder": "placeholders filled in",
    "leftovers": "leftovers removed",
    "formatting": "formatting corrections",
    "style": "style corrections",
}


def _applied_items(applied: list[Finding],
                   numbers: list[int | None] | None = None) -> list[tuple[str, str]]:
    """The tracked changes, named individually while there are few and by
    kind once there are many. A substantive or blocking change is always
    named: those are the ones a lawyer must see before accepting.

    Each named change carries its number from the thread ledger, which is what
    "undo 2" refers to. Without one, undo had only word-matching to go on and
    reversed whichever change shared a word with the reply."""
    number_of = {id(f): n for f, n in zip(applied, numbers or []) if n}

    def named_as(f: Finding) -> str:
        n = number_of.get(id(f))
        what = _as_edit(f)
        return f"{n}. {what}" if n else what

    def item(f: Finding) -> tuple:
        # The one-tap undo beside a numbered change. Only by number: undo is
        # strict, and a number is the one thing it can never misread.
        n = number_of.get(id(f))
        return (named_as(f), "", [(f"undo {n}", f"undo {n}")] if n else [])

    if len(applied) <= GROUP_APPLIED_ABOVE:
        return [item(f) for f in applied]
    named = [f for f in applied if f.severity in (Severity.BLOCKER, Severity.SUBSTANTIVE)]
    grouped = [f for f in applied if f not in named]
    counts: dict[str, int] = {}
    for f in grouped:
        counts[f.category] = counts.get(f.category, 0) + 1
    items = [item(f) for f in named]
    for category, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        what = _CATEGORY_PLURAL.get(category, f"{category} corrections")
        items.append((f"{n} {what}" if n != 1 else f"1 {what.removesuffix('s')}", ""))
    return items


def _as_edit(finding: Finding) -> str:
    """What the tracked change does, in the words that changed.

    '"Reciever" is misspelled' describes a problem; the lawyer is reading a list
    of edits, and 'Cl. 2: "Reciever" → "Recipient"' is one they can accept
    without opening the document. Falls back to the title for anything too long
    to quote or with nothing to show on one side.
    """
    anchor, replacement = finding.anchor or "", finding.suggested_text
    if not anchor or not replacement or finding.category == "drafting":
        return _where_title(finding)
    before, after = _differing_part(anchor, replacement)
    if not before or not after or len(before) > 60 or len(after) > 60:
        return _where_title(finding)
    return _located(finding, f"“{before}” → “{after}”")


def _differing_part(before: str, after: str) -> tuple[str, str]:
    """Trim the common prefix and suffix, widened to whole words."""
    start = 0
    while start < min(len(before), len(after)) and before[start] == after[start]:
        start += 1
    end = 0
    while (end < min(len(before), len(after)) - start
           and before[len(before) - 1 - end] == after[len(after) - 1 - end]):
        end += 1
    while start > 0 and before[start - 1].isalnum():
        start -= 1
    while end > 0 and before[len(before) - end].isalnum():
        end -= 1
    return before[start: len(before) - end].strip(), after[start: len(after) - end].strip()


def _detail(finding, unplaced) -> str:
    """The explanation, plus the fact that a correction existed and did not land.

    A blocker the redliner could not anchor reads exactly like one we never
    intended to fix, which understates how close the correction was. Worded
    around what the lawyer has rather than what went wrong, because the same
    state covers an anchor we could not place and a redline we could not send.
    """
    if finding not in unplaced:
        return finding.explanation
    note = "I could not give you this one as a tracked change, so it is yours to make."
    return f"{finding.explanation} {note}" if finding.explanation else note


def _option_links(finding) -> list[tuple[str, str]]:
    """Each option offered, as (label, reply body) for a one-tap link."""
    if not finding.question:
        return []
    return [(opt, opt) for opt in finding.options or [] if opt.strip()]


def _options_for(finding) -> str:
    """The choices, unless the question already spells them out.

    "Should this be 30 or 13?  [30 or 13]" says the same thing twice on one
    line, which is how an email starts to read like a form.
    """
    if not finding.options:
        return ""
    question = (finding.question or "").lower()
    if all(opt.lower() in question for opt in finding.options):
        return ""
    return " or ".join(finding.options)


def _sentence(title: str) -> str:
    """A full stop, unless the title already ends in punctuation or a quote."""
    title = title.rstrip()
    return title if title[-1:] in ".?!\u201d\"')" else title + "."


def _as_text(sections) -> str:
    lines: list[str] = []
    for section in sections:
        kind = section[0]
        if kind in ("verdict", "alert", "summary", "footer"):
            lines += [section[1], ""]
        elif kind == "notes":
            lines += [t for t, _ in section[2]] + [""]
        elif kind == "asks":
            lines.append(section[1] + ":")
            for question, options, *_ in section[2]:
                mark = "" if _NUMBERED.match(question) else "- "
                lines.append(f"  {mark}{question}" + (f"   [{options}]" if options else ""))
            lines.append("")
        elif kind == "since":
            lines.append(section[1])
            lines.append(section[2])
            lines += [f"  - {item}" for item in section[3]]
            lines.append("")
        elif kind == "items":
            lines += [f"  - {i}" for i in section[1]] + [""]
        elif kind == "deadlines":
            lines += deadlines.as_text(section)
        elif kind == "minor":
            lines.append(section[1] + ":")
            for title, _ in section[2]:
                mark = "" if _NUMBERED.match(title) else "- "
                lines.append(f"  {mark}{_sentence(title)}")
            if section[3]:
                lines.append(f"  ... and {section[3]} more.")
            lines.append("")
        else:
            lines.append(section[1] + ":")
            for title, detail, *_ in section[2]:
                # A numbered change is its own bullet: "  2. Cl. 4: ...".
                mark = "" if _NUMBERED.match(title) else "- "
                line = f"  {mark}{_sentence(title)}"
                lines.append(f"{line} {detail}" if detail else line)
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# What it can do, in the lawyer's terms. Nothing is installed and nobody is
# trained, so this is the one place the replies that work can be discovered:
# on request ("help"), once on a lawyer's first review, and under the
# "attach a .docx" refusal.
# ---------------------------------------------------------------------------

def help_text(agent_address: str) -> str:
    return f"""What I can do, one email each:
  - Forward or attach a Word document and I review it: a verdict in the first line, the findings, and a copy with tracked changes.
  - Attach two versions and say "compare" and I compare them, with the changes that carry risk called out.
  - Say "clean copy" (or "accept all") for a version with every tracked change accepted and the metadata removed.
  - Say "sig pages" when it is ready to sign: an execution copy and a signature page per party, or exactly what is still in the way.
  - Do several in one reply, separated by semicolons: "30; 2; clean copy" answers both questions and sends the clean copy.
  - Reply in plain English to change anything: "undo 2" (the numbers are beside each change), "add a force majeure clause after 7", "call them the Purchaser throughout".
  - "Stop flagging X" and I will not raise X with you again. "Stop" and I leave the thread alone.
  - BCC {agent_address} on what you send and I check it after the fact, replying to you and nobody else. Say "setup" for the mail rule that does it every time.
  - "help" repeats this."""


def setup_text(agent_address: str) -> str:
    return f"""The habit is one address: copy {agent_address} on what you send, and nothing leaves unchecked.

The honest version of "make it automatic": Outlook's rule for sent mail (File > Manage Rules & Alerts > New Rule > "Apply rule on messages I send" > "Cc the message to people or public group") can only Cc, not Bcc, so the recipient would see my address. Gmail and Outlook on the web have no rule for sent mail at all. So the reliable way is by hand, when a document goes out. Tap the attached card to save me as a contact; then BCC autocompletes.

Two things hold whenever I am only copied:
  - I reply to you and never to anyone the message was addressed to.
  - A message with nothing attached gets no reply at all, so copying me on ordinary mail costs nothing.

A document already sent to a client comes back as "Already sent" with what I would have caught, not a redline. To catch it beforehand, forward it to me first."""


# ---------------------------------------------------------------------------
# The contact card. The whole habit is "BCC the agent", and BCC only happens
# if the address autocompletes. Telling a lawyer to create a contact by hand
# is a chore they defer; a card attached to the email is one tap.
# ---------------------------------------------------------------------------

VCARD_TYPE = "text/vcard"
CARD_NOTE = "Forward or BCC documents here for a review before they go out."


def _vcard_text(value: str) -> str:
    """A vCard 3.0 text value: backslash, comma, semicolon and newline escaped."""
    return (value.replace("\\", "\\\\").replace(",", "\\,")
            .replace(";", "\\;").replace("\r\n", "\\n").replace("\n", "\\n"))


def _vcard_fold(line: str) -> str:
    """Lines over 75 octets continue on the next line after a space (RFC 2425)."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts, current = [], b""
    for char in line:
        encoded = char.encode("utf-8")
        if len(current) + len(encoded) > (75 if not parts else 74):
            parts.append(current.decode("utf-8"))
            current = b""
        current += encoded
    parts.append(current.decode("utf-8"))
    return "\r\n ".join(parts)


def contact_card(name: str, address: str) -> Attachment:
    """The agent as a vCard 3.0, named after the agent."""
    name = name.strip() or address
    lines = [
        "BEGIN:VCARD",
        "VERSION:3.0",
        f"N:;{_vcard_text(name)};;;",
        f"FN:{_vcard_text(name)}",
        f"ORG:{_vcard_text(name)}",
        f"EMAIL;TYPE=INTERNET,PREF:{address}",
        f"NOTE:{_vcard_text(CARD_NOTE)}",
        "END:VCARD",
    ]
    content = ("\r\n".join(_vcard_fold(line) for line in lines) + "\r\n").encode("utf-8")
    # A display name is the operator's to choose; a slash in it is not a filename.
    stem = re.sub(r'[\\/:*?"<>|\r\n]+', " ", name).strip() or "contact"
    return Attachment(filename=f"{stem}.vcf", content_type=VCARD_TYPE,
                      size_bytes=len(content), content=content)


def attach_contact_card(out: OutboundEmail, name: str, address: str) -> None:
    """Add the card, unless it would push the email past the outbound ceiling.

    The card is a few hundred bytes and optional; the redline is neither. A
    reply already carrying a near-limit redline goes without it rather than
    risk the send failing and the retry stripping the redline too.
    """
    card = contact_card(name, address)
    carried = sum(a.size_bytes for a in out.attachments)
    if carried + card.size_bytes > MAX_ATTACHMENT_BYTES:
        return
    out.attachments = list(out.attachments) + [card]


def text_as_html(text: str) -> str:
    """The HTML twin of a reply composed as plain lines.

    The follow-up replies (an undo, an answer, an instruction carried out) are
    written line by line rather than as sections, and went out text-only. Round
    one arrived looking like a colleague wrote it; round two arrived in
    Outlook's plain-text face. Blank lines separate paragraphs, lines beginning
    with a dash are list items, indented continuation lines stay with their
    item, and the first line is the verdict, set like every other verdict.
    """
    sections: list[tuple] = []
    paragraph: list[str] = []
    items: list[str] = []

    def flush() -> None:
        nonlocal paragraph, items
        if items:
            sections.append(("items", list(items)))
            items = []
        if paragraph:
            kind = "verdict" if not sections else "summary"
            sections.append((kind, " ".join(paragraph)))
            paragraph = []

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            flush()
        elif stripped.startswith("- "):
            if paragraph:
                flush()
            items.append(stripped[2:].strip())
        elif items and line.startswith("    "):
            items[-1] += " " + stripped
        else:
            if items:
                flush()
            paragraph.append(stripped)
    flush()
    return _as_html(sections)


# ---------------------------------------------------------------------------
# One-tap replies. Every answer the review asks for is a word or a number the
# lawyer has to type on a phone. In the HTML body each one is also a mailto:
# link that opens a reply with that text already in it. Plain <a> links with
# inline styles: Outlook on Windows renders mail through Word, which draws no
# buttons, runs no script and ignores most CSS, but underlines a link.
# ---------------------------------------------------------------------------

_LINK_STYLE = "color:#1a55c4;text-decoration:underline"
_SMALL_LINK_STYLE = "color:#666;text-decoration:underline;font-size:13px"


def _tapper(address: str, subject: str):
    """A function from reply text to the mailto: URL that writes it."""
    prefix = f"mailto:{address}?subject={quote(subject, safe='')}&body="

    def href(body: str) -> str:
        return prefix + quote(body, safe="")

    return href


def _link(tap, label: str, body: str, style: str = _LINK_STYLE) -> str:
    return (f'<a href="{html.escape(tap(body), quote=True)}" style="{style}">'
            f"{html.escape(label)}</a>")


def _option_links_html(tap, links) -> str:
    return " or ".join(_link(tap, label, body) for label, body in links)


def _footer_html(text: str, phrases: list[str], tap) -> str:
    """The footer, with each quoted reply that is safe to send as it stands
    turned into a link that sends it."""
    out = html.escape(text)
    if not tap:
        return out
    for phrase in phrases:
        quoted = html.escape(f'"{phrase}"')
        out = out.replace(quoted, f"&quot;{_link(tap, phrase, phrase)}&quot;", 1)
    return out


def linked(text: str, phrases: list[str], tap_address: str | None,
           subject: str) -> str:
    """One line of text as HTML, its quoted replies from `phrases` made
    one-tap when there is an address to send them to."""
    tap = _tapper(tap_address, _subject(subject)) if tap_address else None
    return _footer_html(text, phrases, tap)


def _as_html(sections, tap=None) -> str:
    """A plain, readable HTML version. No images, no tracking, no branding.

    Deliberately unstyled beyond legibility: this arrives in the middle of a
    working thread and should look like a colleague wrote it, not like a
    marketing email.

    `tap`, when given, turns every reply the email asks for into a one-tap
    mailto: link (see _tapper); without it nothing is linked.
    """
    parts = [
        (
            '<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;'
            'font-size:15px;line-height:1.5;color:#111;max-width:42em">'
        )
    ]
    for section in sections:
        kind = section[0]
        if kind == "verdict":
            parts.append(
                f'<p style="font-size:17px;font-weight:600;margin:0 0 .8em">'
                f"{html.escape(section[1])}</p>"
            )
        elif kind == "alert":
            parts.append(
                '<p style="margin:0 0 1em;padding:.7em .9em;background:#fdf1f1;'
                f'border-left:3px solid #c0392b">{html.escape(section[1])}</p>'
            )
        elif kind == "footer":
            phrases = section[2] if len(section) > 2 else []
            parts.append('<p style="margin:0 0 1em;color:#555">'
                         f"{_footer_html(section[1], phrases, tap)}</p>")
        elif kind == "summary":
            parts.append(f'<p style="margin:0 0 1em">{html.escape(section[1])}</p>')
        elif kind == "notes":
            # Body size, body colour. These explain which review ran and what
            # is attached; at 14px grey they were styled as the least important
            # thing in the email, which is the opposite of what they are.
            for title, _ in section[2]:
                parts.append(
                    f'<p style="margin:0 0 .8em">{html.escape(title)}</p>'
                )
        elif kind == "asks":
            rows = []
            for q, o, *rest in section[2]:
                links = rest[0] if rest else []
                if tap and links:
                    # The options as links, even when the question already
                    # names them: here they are something to press.
                    o_html = _option_links_html(tap, links)
                else:
                    o_html = html.escape(o)
                rows.append(
                    f"<li>{html.escape(q)}"
                    + (f' <span style="color:#666">[{o_html}]</span>' if o_html else "")
                    + "</li>")
            items = "".join(rows)
            parts.append(
                f'<p style="margin:0 0 .3em;font-weight:600">{html.escape(section[1])}</p>'
                f'<ul style="margin:0 0 1em;padding-left:1.2em">{items}</ul>'
            )
        elif kind == "since":
            items = "".join(f"<li>{html.escape(i)}</li>" for i in section[3])
            parts.append(
                f'<p style="margin:0 0 .2em;font-weight:600">{html.escape(section[1])}</p>'
                f'<p style="margin:0 0 .3em">{html.escape(section[2])}</p>'
                + (f'<ul style="margin:0 0 1em;padding-left:1.2em">{items}</ul>'
                   if items else "")
            )
        elif kind == "items":
            # A list with no heading of its own: the sentence above it is the
            # heading. Used by the line-composed follow-up replies.
            parts.append(
                '<ul style="margin:0 0 1em;padding-left:1.2em">'
                + "".join(f"<li>{html.escape(i)}</li>" for i in section[1])
                + "</ul>"
            )
        elif kind == "deadlines":
            parts.append(deadlines.as_html(section))
        elif kind == "minor":
            # A plain grey list, not <details>. Outlook on Windows renders mail
            # through the Word HTML engine, which has no <details>: it drops the
            # element and shows the children, so the collapse never happened
            # where most law firms read their mail. The cap does the job that
            # collapsing was supposed to do, and it works everywhere.
            items = "".join(f"<li>{html.escape(t)}</li>" for t, _ in section[2])
            if section[3]:
                items += f"<li>and {section[3]} more</li>"
            parts.append(
                f'<p style="margin:0 0 .3em;color:#555">{html.escape(section[1])}</p>'
                f'<ul style="margin:0 0 1em;padding-left:1.2em;color:#555">'
                f"{items}</ul>"
            )
        else:
            rows = []
            for t, d, *rest in section[2]:
                links = rest[0] if rest else []
                detail = html.escape(d) if d else ""
                if tap and links and len(rest) > 1 and rest[1]:
                    # A blocker's question, with its options as links in
                    # place of the plain "(30 or 13)".
                    detail = (f"{html.escape(rest[1])} "
                              f"({_option_links_html(tap, links)})")
                elif tap and links:
                    # A numbered change's undo, small and grey beside it.
                    undo = " ".join(_link(tap, label, body, _SMALL_LINK_STYLE)
                                    for label, body in links)
                    detail = f"{detail} {undo}".strip()
                rows.append(f"<li><b>{html.escape(_sentence(t))}</b>"
                            + (f" {detail}" if detail else "") + "</li>")
            items = "".join(rows)
            parts.append(
                f'<p style="margin:0 0 .3em;font-weight:600">{html.escape(section[1])}</p>'
                f'<ul style="margin:0 0 1em;padding-left:1.2em">{items}</ul>'
            )
    parts.append("</div>")
    return "".join(parts)


# --------------------------------------------------------------------------


def rejection(inbound: InboundEmail, reason: str) -> OutboundEmail:
    """A refusal, in the sentence that says what to do next and nothing else.

    It used to end "Reply to this email and I will pick it up from there" under
    every reason, including a file too large to send, where a reply without a
    new file achieves nothing.
    """
    body = reason.rstrip() + "\n"
    return OutboundEmail(
        to=[inbound.from_address],
        subject=_subject(inbound.subject, "Could not review"),
        text_body=body,
        html_body=_as_html([("summary", reason)]),
        in_reply_to=inbound.message_id,
        thread_id=inbound.thread_id,
    )


def _verdict(blockers: list[Finding], judgment: list[Finding],
             asks: list[Finding], minor: list[Finding],
             applied: list[Finding]) -> str:
    """One line a partner can act on from the preview pane.

    Every arm names a section of this email that exists. "blockers" was a
    code-review word; the lawyer's question is whether they can hit send.
    """
    if blockers:
        n = len(blockers)
        return f"Don't send yet: {n} thing{'s' if n != 1 else ''} to fix first."
    if judgment:
        n = len(judgment)
        return f"Nearly ready: {n} point{'s' if n != 1 else ''} for your call."
    if asks:
        n = len(asks)
        return f"Nearly ready: {n} quick question{'s' if n != 1 else ''}."
    if applied:
        n = len(applied)
        return f"Ready to send once you accept my {n} tracked change{'s' if n != 1 else ''}."
    if minor:
        return "Ready to send. A few optional tidy-ups below."
    # Earned, so said: a draft with nothing in it at all is the associate's
    # work done well, and the one place three more words cost nothing.
    return "Ready to send. Nothing to flag. Clean draft."


def _their_verdict(blockers: list[Finding], all_findings: list[Finding],
                   partial: bool = False) -> str:
    """The other side's draft: not whether to send it, but how much in it to
    answer. The count is the issues list's rows (their_paper.points)."""
    n = sum(1 for f in all_findings if f.severity is Severity.SUBSTANTIVE)
    b = len(blockers)
    head = "Their draft (partial review)" if partial else "Their draft"
    points = f"{n} point{'s' if n != 1 else ''} to push back on"
    errors = f"{b} error{'s' if b != 1 else ''} in it"
    if n and b:
        return f"{head}: {points}, and {errors}."
    if n:
        return f"{head}: {points}."
    if b:
        return f"{head}: {errors}, nothing to push back on."
    return f"{head}: nothing to push back on."


def _sent_verdict(blockers: list[Finding], all_findings: list[Finding]) -> str:
    """Counts auto-applied findings too: a correction written into our copy is
    still wrong in the version the client already has."""
    if blockers:
        n = len(blockers)
        return f"Already sent, with {n} error{'s' if n != 1 else ''} in it."
    substantive = [f for f in all_findings if f.severity is Severity.SUBSTANTIVE]
    if substantive:
        n = len(substantive)
        return (f"Already sent. No errors, but {n} point{'s' if n != 1 else ''} "
                "worth knowing.")
    return "Already sent. Nothing in it needs correcting."


# A verdict previously appended by this agent, so it can be stripped rather
# than stacked. Three rounds produced "Re: SPA - Not ready to send. 2 blockers -
# Looks good. Minor cleanups only - Looks good. Nothing to flag", which also
# breaks conversation grouping in every mail client.
_PRIOR_VERDICT = re.compile(
    r"\s+-\s+(?:Not ready to send[^-]*|Send with care[^-]*|Looks good[^-]*|"
    r"Already sent[^-]*|Could not review|\d+ changes?[^-]*|No changes[^-]*|"
    r"Clean copy attached|Already clean[^-]*|Formatting repaired)$",
    re.IGNORECASE,
)


def _subject(original: str, verdict: str = "") -> str:
    """The lawyer's own subject, with "Re:" and nothing else.

    The verdict used to be appended: "Re: Falcon NDA - Not ready to send. 2
    blockers". It read well in a list and it broke the thing a reply is for.
    Gmail files a message under the email it answers only when the References
    header matches AND the subject does, prefixes aside, so every reply we sent
    started a conversation of its own, and the first production email arrived
    beside the message it was answering rather than under it. Outlook groups by
    subject as well.

    Nothing is lost by taking it out. The verdict is the first line of every
    reply, which is what a phone shows under the subject, and DECISIONS asks
    for exactly that: a verdict in the first line.

    `verdict` is still accepted so that call sites say what they are replying
    with; it is not used. Verdicts appended under the old scheme are still
    stripped, so a thread that began before this change heals on its next
    reply instead of carrying "- Looks good" for ever.
    """
    base = original or "your document"
    while True:
        stripped = _PRIOR_VERDICT.sub("", base).strip()
        if stripped == base:
            break
        base = stripped
    if not base.lower().startswith("re:"):
        base = f"Re: {base}"
    return base


def _redline_name(filename: str) -> str:
    """Always .docx: the redline is a Word file whatever arrived. A PDF's
    marked-up copy used to be offered as "brief (redline).pdf", which is a
    Word document with the wrong extension, and one nothing would open."""
    stem, _, _ = filename.rpartition(".")
    return f"{stem or filename} (redline).docx"


def annotated_name(filename: str) -> str:
    stem, _, _ = filename.rpartition(".")
    return f"{stem or filename} (annotated).pdf"
