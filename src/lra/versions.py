"""Requests about versions of a document rather than about one document.

Two of them: "what changed between these?" and "give me a clean copy". Both are
things a lawyer otherwise opens a desktop tool for, both reuse machinery that
already exists, and both end the same way as a review: one reply, a verdict in
the first line, and an attachment that has been proved before it is sent.
"""

from __future__ import annotations

import logging
from pathlib import Path

from lra import archive, reconcile, thread
from lra.models import Attachment, InboundEmail, OutboundEmail
from lra.pipeline import (
    clean,
    clean_pdf,
    compare,
    extract,
    identity,
    intake,
    reflow,
    renumber,
    repair,
    reply,
)
from lra.pipeline.filetype import Kind, identify

log = logging.getLogger(__name__)

DOCX_TYPE = reply.DOCX_TYPE
PROMPTS = Path(__file__).parent / "prompts"

# How many changes the email spells out. The markup carries all of them; the
# email is for deciding which to open it for.
_LISTED = 12
_LISTED_WITHOUT_MARKUP = 40
_QUOTE = 220


def house_style() -> str:
    """The firm's conventions, minus the template's own instructions.

    The shipped file is a placeholder explaining what to put in it. Sending
    that to the model as the firm's house style would be worse than sending
    nothing, so it only counts once somebody has replaced it.
    """
    try:
        text = (PROMPTS / "house_style.md").read_text()
    except OSError:
        return ""
    return "" if "Replace this file with them." in text else text.strip()


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------


def handle_comparison(email: InboundEmail, provider, pair: tuple | None = None,
                      previous: bool | None = None) -> str:
    """Compare two versions and reply. Returns a status word for the job log.
    `pair` is (earlier, later, how it was decided) and `previous` whether the
    baseline is the version seen before, when a triage plan decided them
    (handler._dispatch); otherwise both are worked out here."""
    user = identity.resolve_user(email)
    earlier, later, how = pair or _resolve_pair(email, user, previous)

    instructions = intake.extract_instructions(email)
    earlier_doc, later_doc = extract.extract(earlier), extract.extract(later)

    # A Word file for each side, so the comparison can be tracked changes even
    # when one side arrived as a PDF. This is Litera Compare's headline case
    # and the commonest one in practice: the draft went out as Word and came
    # back from the other side as a PDF. A side with no Word copy (a deck, a
    # workbook, a scan) leaves the comparison as a change list in the email.
    old_word, old_how = _word_copy(earlier, earlier_doc)
    new_word, new_how = _word_copy(later, later_doc)
    converted = [n for n in (old_how, new_how) if n]

    if old_word is not None and new_word is not None:
        result = compare.compare(old_word, new_word)
        if old_how and result.content:
            result.notes.insert(0, (
                "The marked-up copy is that Word copy of the earlier version, so it "
                "does not have the original's layout."
            ))
    else:
        result = compare.compare_text(earlier_doc, later_doc)

    _archive(email, user, {later.filename: later_doc.as_prompt(limit=200_000)})

    if not result.identical:
        compare.explain(result, later_doc, instructions, house_style())

    sent_id = provider.send(_comparison_reply(email, earlier, later, how, result, converted))
    _remember(email, user, later, new_word or later.content, sent_id)
    return "compared"


def _word_copy(att: Attachment, doc) -> tuple[bytes | None, str]:
    """The Word file for one side of a comparison, and a sentence saying how it
    was made when it was not the file that arrived. None when there is no
    honest way to make one, in which case the email carries the list."""
    if identify(att.content, att.filename) is Kind.DOCX:
        return att.content, ""
    if not reflow.convertible(att):
        return None, ""
    try:
        rebuilt = reflow.to_docx(att, doc)
    except reflow.CannotConvert as e:
        log.info("no Word copy of %s for the comparison: %s", att.filename, e)
        return None, ""
    except Exception:
        log.exception("could not build a Word copy of %s for the comparison", att.filename)
        return None, ""
    if rebuilt.source == "pdf":
        how = f"{att.filename} is a PDF, so I rebuilt it as a Word document from its text first"
    elif rebuilt.source == "text":
        how = f"{att.filename} is a text file, so I rebuilt it as a Word document first"
    else:
        how = f"{att.filename} was converted from .{rebuilt.source} to Word first"
    return rebuilt.content, how


def _remember(email: InboundEmail, user: str, later: Attachment, content: bytes,
              sent_id) -> None:
    """Leave the later version behind as the thread's document, so that a reply
    ("look harder at clause 4") is a review of it rather than a message about
    nothing. The document held is the Word copy when one was made, since that
    is what a review of it would redline. Never allowed to fail the comparison
    it follows."""
    try:
        key = thread.resolve(user, email.thread_id, email.message_id, email.in_reply_to,
                             email.references) or thread.key(email.thread_id,
                                                              email.message_id, user)
        name = later.filename if content is later.content else reflow.docx_name(later.filename)
        state = thread.start(key, user, name, content, origin="compare")
        thread.add_alias(state.id, email.message_id, email.thread_id,
                         sent_id if isinstance(sent_id, str) else None)
    except Exception:
        log.exception("could not record the comparison's thread")


def _resolve_pair(email: InboundEmail, user: str, previous: bool | None = None):
    documents = intake._reviewable(email)
    if previous is None:
        previous = intake.wants_previous_version(email)
    if len(documents) == 1 and previous:
        later = documents[0]
        found = archive.latest_version(user, later.filename)
        if found is None:
            # The archive is off by default, and "the one I sent you" is a
            # promise the help text makes regardless. The lawyer's own
            # conversations hold every document reviewed, so look there:
            # by name, then by how alike the text is.
            found = _from_threads(user, later)
        if found is None:
            raise intake.Rejection(
                f"I do not have an earlier version of {later.filename} to compare "
                "against. Attach both versions and I will show you what changed."
            )
        blob, sent_at = found
        earlier = Attachment(
            filename=f"{later.filename} (as of {sent_at[:10]})",
            content_type=later.content_type,
            size_bytes=len(blob),
            content=blob,
        )
        return earlier, later, f"against the copy I last saw on {sent_at[:10]}"
    return intake.pick_pair(email)


def _from_threads(user: str, later: Attachment) -> tuple[bytes, str] | None:
    """The document a recent conversation of this lawyer's was about, when it
    reads as an earlier version of `later`. Never raises: the archive answer,
    or the rejection, stands if this cannot help."""
    try:
        text = reconcile._text_of(later)
        state = reconcile.conversation_for(user, later, text, require_changes=False)
    except Exception:
        log.exception("could not look for an earlier version in the threads")
        return None
    if state is None or not state.original:
        return None
    return state.original, (state.updated_at or state.created_at or "")


def _archive(email: InboundEmail, user: str, attachment_text: dict[str, str]) -> None:
    try:
        archive.store(
            email, owner=user, direction="received",
            matter_id=identity.resolve_matter(email),
            external=identity.external_recipients(email),
            attachment_text=attachment_text,
        )
    except Exception:
        log.exception("archiving failed for %s", email.message_id)


_RISK_ORDER = {"high": 0, "medium": 1, "low": 2, "": 3}


def _comparison_reply(email: InboundEmail, earlier: Attachment, later: Attachment,
                      how: str, result: compare.Comparison,
                      converted: list[str] | None = None) -> OutboundEmail:
    orientation = (
        f"I treated {earlier.filename} as the earlier version and {later.filename} "
        f"as the later one, {how}. If that is backwards, send them again and say "
        "which is which."
    )
    if converted:
        # Said with the orientation, because it changes what the attachment is:
        # a comparison of the text, not of the pages the other side sent.
        orientation += " " + ". ".join(converted) + "."

    if result.identical:
        verdict = "No changes. The two versions say the same thing."
        sections = [("verdict", verdict), ("summary", orientation),
                    ("notes", "", [(n, "") for n in result.notes])]
        return _send(email, verdict, sections, [])

    ranked = sorted(result.changes, key=lambda c: _RISK_ORDER.get(c.risk, 3))
    matter = [c for c in ranked if c.risk in ("high", "medium")]
    assessed = any(c.risk or c.impact for c in result.changes)

    total = len(result.changes)
    plural = "s" if total != 1 else ""
    if matter:
        verdict = (f"{total} change{plural}. {len(matter)} need"
                   f"{'s' if len(matter) == 1 else ''} your attention.")
    elif assessed:
        verdict = f"{total} change{plural}. Nothing that shifts the deal."
    else:
        verdict = f"{total} change{plural} between the two versions."

    sections: list = [("verdict", verdict)]
    if result.summary:
        sections.append(("summary", result.summary))
    sections.append(("summary", orientation))

    if matter:
        sections.append(("findings", "Read these", [_line(c) for c in matter]))
    rest = [c for c in ranked if c not in matter]
    # With no marked-up copy the email is the only record, so it carries more.
    limit = _LISTED if result.content else _LISTED_WITHOUT_MARKUP
    shown = rest[: max(0, limit - len(matter))]
    if shown:
        heading = "Everything else" if matter else "What changed"
        sections.append(("findings", heading, [_line(c) for c in shown]))
    hidden = len(rest) - len(shown)

    notes = list(result.notes)
    attachments: list[Attachment] = []
    if result.content:
        name = _comparison_name(later.filename)
        attachments.append(Attachment(
            filename=name, content_type=DOCX_TYPE,
            size_bytes=len(result.content), content=result.content,
        ))
        claim = (
            f"{name} is the earlier version with every change marked. Accepting "
            "all of them gives you the later version word for word, and "
            "rejecting all of them gives you the earlier one; I checked both."
            if result.proven else
            f"{name} is the earlier version with the changes marked."
        )
        notes.insert(0, claim)
        unplaced = [c for c in result.changes if not c.landed]
        if unplaced:
            notes.insert(1, (
                f"{len(unplaced)} change{'s' if len(unplaced) != 1 else ''} could not be marked up safely and "
                "appear only in this email: "
                + "; ".join(f"{c.where} ({c.why_not})" for c in unplaced[:5]) + "."
            ))
    if hidden > 0:
        notes.append(f"{hidden} smaller change{'s are' if hidden != 1 else ' is'} not listed here"
                     + ((" but in the marked-up copy." ) if result.content else "."))
    sections.append(("notes", "", [(n, "") for n in notes]))
    return _send(email, verdict, sections, attachments)


def _line(change: compare.Change) -> tuple[str, str]:
    label = {"replaced": "Changed", "inserted": "Added",
             "deleted": "Deleted", "moved": "Moved"}[change.kind]
    title = f"{label}, {change.where}"
    if change.risk:
        title = f"Key: {title}" if change.risk == "high" else title

    detail: list[str] = []
    if change.impact:
        detail.append(change.impact)
    # The words themselves, always. The impact is the model's account of the
    # change; the quotation is the change.
    if change.kind == "replaced":
        before, after = _differing(change.before, change.after)
        detail.append(f"Was “{_clip(before)}”, now “{_clip(after)}”")
    elif change.kind == "inserted":
        detail.append(f"New text: “{_clip(change.after)}”")
    elif change.kind == "deleted":
        detail.append(f"Removed: “{_clip(change.before)}”")
    elif compare._normalize(change.before) != compare._normalize(change.after):
        before, after = _differing(change.before, change.after)
        detail.append(f"Reworded on the way: was “{_clip(before)}”, now “{_clip(after)}”")
    if change.response:
        detail.append(f"Suggested response: {change.response}")
    return title, " ".join(d if d.endswith((".", "”", "?", "!")) else d + "." for d in detail)


def _differing(before: str, after: str) -> tuple[str, str]:
    from lra.handler import _differing_span

    a, b = _differing_span(" ".join(before.split()), " ".join(after.split()))
    return (a or before), (b or after)


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _QUOTE else text[: _QUOTE - 1].rstrip() + "…"


def _comparison_name(filename: str) -> str:
    stem, _, _ = filename.rpartition(".")
    return f"{stem or filename} (comparison).docx"


# --------------------------------------------------------------------------
# Clean copy
# --------------------------------------------------------------------------


def handle_clean_copy(email: InboundEmail, provider, filename: str,
                      content: bytes, note: str = "",
                      still_open: list[str] | None = None) -> str:
    """A clean copy, sent. `still_open` is what the review asked and nobody
    has answered: a clean copy reads as ready to send, and one that still says
    "thirty (13)" because the question about it was never answered must say so
    in its first line, not leave the lawyer to remember."""
    kind = identify(content, filename)
    if kind is Kind.PDF:
        try:
            result = clean_pdf.clean(content)
        except clean.CannotClean as e:
            raise intake.Rejection(str(e)) from e
        provider.send(_clean_reply(email, filename, result, note, still_open or [],
                                   pdf=True))
        return "cleaned"
    if kind is not Kind.DOCX:
        raise intake.Rejection(
            "I can only make a clean copy of a Word document or a PDF. Send me "
            "the .docx and I will strip it."
        )
    try:
        result = clean.clean(content)
    except clean.CannotClean as e:
        raise intake.Rejection(str(e)) from e

    provider.send(_clean_reply(email, filename, result, note, still_open or []))
    return "cleaned"


def _clean_reply(email: InboundEmail, filename: str,
                 result: clean.CleanCopy, note: str = "",
                 still_open: list[str] | None = None, pdf: bool = False) -> OutboundEmail:
    if not result.changed:
        verdict = "Already clean. Nothing to remove."
        carried = ("comments, attachments, scripts" if pdf
                   else "tracked changes, comments, hidden text")
        sections = [("verdict", verdict), ("summary", (
            f"{filename} carries no {carried} or identifying metadata, so I have "
            "not attached a copy."))]
        return _send(email, verdict, sections, [])

    verdict = "Clean copy attached."
    if still_open:
        n = len(still_open)
        verdict = f"Clean copy attached, but {n} question{'s are' if n != 1 else ' is'} still open."
    name = _clean_name(filename)
    sections = [
        ("verdict", verdict),
        ("findings", "Removed", [(item, "") for item in result.removed]),
        ("summary", "Nothing else changed; I checked the wording against yours."),
    ]
    if still_open:
        sections.insert(1, ("findings", "Still open, so not in this copy",
                            [(q, "") for q in still_open]))
    if note:
        # Directly under the verdict: it says which document this is.
        sections.insert(1, ("summary", note))
    attachment = Attachment(filename=name,
                            content_type="application/pdf" if pdf else DOCX_TYPE,
                            size_bytes=len(result.content), content=result.content)
    return _send(email, verdict, sections, [attachment])


def _clean_name(filename: str) -> str:
    stem, _, ext = filename.rpartition(".")
    stem = (stem or filename).replace(" (redline)", "")
    return f"{stem} (clean).{ext or 'docx'}"


# --------------------------------------------------------------------------
# Formatting repair
# --------------------------------------------------------------------------


def handle_repair(email: InboundEmail, provider, filename: str, content: bytes) -> str:
    if identify(content, filename) is not Kind.DOCX:
        raise intake.Rejection(
            "I can only repair the formatting of a Word document. Send me the "
            ".docx and I will take it from there."
        )
    try:
        result = repair.repair(content, filename)
    except repair.RepairRejected as e:
        raise intake.Rejection(str(e)) from e

    verdict = "Formatting repaired. Not a word changed."
    sections = [
        ("verdict", verdict),
        ("summary", (f"{result.filename} is attached. I compared it with yours before "
                    "sending: the wording is identical in every part of the "
                    "document, no tracked change or comment was added or removed, "
                    "and it introduces nothing the mechanical checks object to.")),
    ]
    if result.changes:
        sections.append(("findings", "What was changed",
                         [(line.rstrip("."), "") for line in result.changes]))
    sections.append(("notes", "", [(
        ("Automatic clause numbers are formatting, not text, so they are the one "
        "thing the comparison above cannot see. Glance at the numbering before "
        "you rely on this copy."), "")]))
    attachment = Attachment(filename=result.filename, content_type=DOCX_TYPE,
                            size_bytes=len(result.content), content=result.content)
    provider.send(_send(email, verdict, sections, [attachment]))
    return "repaired"


# --------------------------------------------------------------------------
# Renumbering
# --------------------------------------------------------------------------

_REFERENCES_LISTED = 15

# Said when "renumber" arrives as a reply on a review: the redline is rebuilt
# from the original every time, and edits are never stacked on edits.
RENUMBERED_ORIGINAL = (
    "This is the copy you sent me with only the numbering changed; my review's "
    "changes are in the redline I sent before."
)


def handle_renumber(email: InboundEmail, provider, filename: str, content: bytes,
                    note: str = "") -> str:
    """Typed clause numbers and every reference to them, fixed as tracked
    changes and proved, or a sentence saying why not."""
    if identify(content, filename) is not Kind.DOCX:
        raise intake.Rejection(
            "I can only renumber a Word document. Send me the .docx and I will "
            "fix the numbering and every reference to it."
        )
    try:
        result = renumber.renumber(content, filename)
    except renumber.CannotRenumber as e:
        raise intake.Rejection(str(e)) from e

    verdict = result.headline
    sections: list = [("verdict", verdict)]
    if note:
        sections.append(("summary", note))
    if result.was_wrong:
        sections.append(("summary", "What was wrong: "
                         + "; ".join(t.rstrip(".") for t in result.was_wrong) + "."))
    if result.references:
        shown = result.references[:_REFERENCES_LISTED]
        sections.append(("findings", "References updated", [
            (f"{r.where}: {r.before} → {r.after}", "") for r in shown]))
    sections.append(("summary", (
        f"{result.filename} is attached. I read it back before sending: the "
        f"{result.word} numbers now run in order, every reference points at the "
        f"{result.word} it pointed at before, and nothing else in the document "
        "changed. Automatic numbering and schedule paragraphs are untouched.")))
    hidden = len(result.references) - _REFERENCES_LISTED
    if hidden > 0:
        more = (f"{hidden} more reference change{'s are' if hidden != 1 else ' is'} "
                "in the document.")
        sections.append(("notes", "", [(more, "")]))
    attachment = Attachment(filename=result.filename, content_type=DOCX_TYPE,
                            size_bytes=len(result.content), content=result.content)
    provider.send(_send(email, verdict, sections, [attachment]))
    return "renumbered"


def _send(email: InboundEmail, verdict: str, sections: list,
          attachments: list[Attachment]) -> OutboundEmail:
    sections = [s for s in sections if s[0] != "notes" or s[2]]
    return OutboundEmail(
        to=[email.from_address],
        subject=reply._subject(email.subject, verdict.split(". ")[0]),
        text_body=reply._as_text(sections),
        html_body=reply._as_html(sections),
        in_reply_to=email.message_id,
        thread_id=email.thread_id,
        attachments=attachments,
    )
