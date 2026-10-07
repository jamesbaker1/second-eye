"""The deadlines as a calendar file.

The reply's "Dates and deadlines" table says what falls due when. A lawyer's
next move is to put the dated ones in the diary, one by one, retyping a date
from an email into a calendar: exactly where a 30 June becomes a 30 July.
This writes them as one .ics file (RFC 5545) instead: one all-day event per
dated deadline, named for what it is and the clause it comes from, with a
reminder seven days before.

Only what the document dates. A period that runs from an event with no date
yet ("within 12 months after Completion") has no day to put in a calendar,
and a guessed one is worse than none: it is listed in the email instead, and
never invented here. The agreement's own date is not a deadline and is left
out, and so are reference dates such as the Accounts Date, which fix a
historic period rather than set anything to do.

Two ways in: attached to a review's reply when the table has at least two
dated deadlines (handler._compose), and on request, "send me the deadlines as
a calendar", for the attached document or the one this conversation holds
(handle(), routed like the signature pack).
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import re
from dataclasses import dataclass

from lra.models import Attachment, InboundEmail
from lra.pipeline.extract import ExtractedDoc

log = logging.getLogger(__name__)

ICS_TYPE = "text/calendar"
# Attached to a review only when it says more than one thing: a single date is
# quicker to type than to import.
MIN_DATED = 2
REMINDER_DAYS = 7
# Undated periods named in the email; the table above carries the rest.
UNDATED_LISTED = 6
PRODID = "-//Redline Desk//Deadlines//EN"


@dataclass
class Event:
    day: datetime.date
    what: str
    where: str
    when: str


@dataclass
class Deadlines:
    dated: list[Event]
    undated: list[dict]         # timeline rows with no date: where, what, when


def read(doc: ExtractedDoc) -> Deadlines:
    """The document's deadlines, split into those it dates and those it
    does not."""
    from lra.pipeline import timeline

    if getattr(doc, "transcribed", False):
        return Deadlines([], [])
    _, rows = timeline.events(doc)
    dated: list[Event] = []
    seen: set[tuple[str, datetime.date]] = set()
    undated: list[dict] = []
    for r in rows:
        if r["kind"] in ("agreement-date", "reference"):
            continue            # not deadlines: the signing, and historic dates
        if not r["date"]:
            if r["from"]:
                undated.append(r)
            continue
        day = datetime.date.fromisoformat(r["date"])
        if (r["what"], day) in seen:
            continue
        seen.add((r["what"], day))
        dated.append(Event(day, r["what"], r["where"], r["when"]))
    dated.sort(key=lambda e: e.day)
    return Deadlines(dated, undated)


# --------------------------------------------------------------------------
# RFC 5545
# --------------------------------------------------------------------------


def _text(value: str) -> str:
    """A TEXT value: backslash, semicolon, comma and newline escaped (3.3.11)."""
    return (value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n"))


def _fold(line: str) -> str:
    """Lines longer than 75 octets continue after CRLF and a space (3.1),
    never splitting a UTF-8 character."""
    parts, current = [], b""
    for char in line:
        encoded = char.encode("utf-8")
        if len(current) + len(encoded) > (75 if not parts else 74):
            parts.append(current.decode("utf-8"))
            current = b""
        current += encoded
    parts.append(current.decode("utf-8"))
    return "\r\n ".join(parts)


def _stem(filename: str) -> str:
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    return re.sub(r'[\\/:*?"<>|\r\n]+', " ", stem).strip() or "document"


def filename_for(document: str) -> str:
    return f"{_stem(document)} (deadlines).ics"


def calendar(events: list[Event], document: str,
             now: datetime.datetime | None = None) -> bytes:
    """The events as an iCalendar file."""
    now = now or datetime.datetime.now(datetime.UTC)
    stamp = now.astimezone(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    stem = _stem(document)
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", f"X-WR-CALNAME:{_text(stem + ' deadlines')}"]
    for e in events:
        where = e.where[:1].upper() + e.where[1:] if e.where else ""
        summary = f"{e.what} ({e.where})" if e.where else e.what
        # The same deadline in the same document is the same event, so a
        # second import updates it rather than adding a duplicate.
        uid = hashlib.sha256(f"{stem}|{e.where}|{e.what}|{e.day}".encode()).hexdigest()[:32]
        description = (f"{where + ': ' if where else ''}{e.when}. From {document}. "
                       "Check the date against the signed version.")
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}@redline-desk",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{e.day:%Y%m%d}",
            f"DTEND;VALUE=DATE:{e.day + datetime.timedelta(days=1):%Y%m%d}",
            f"SUMMARY:{_text(f'{summary} - {stem}')}",
            f"DESCRIPTION:{_text(description)}",
            "TRANSP:TRANSPARENT",
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{_text(f'In {REMINDER_DAYS} days: {summary} - {stem}')}",
            f"TRIGGER:-P{REMINDER_DAYS}D",
            "END:VALARM",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return ("\r\n".join(_fold(line) for line in lines) + "\r\n").encode("utf-8")


def attachment(deadlines: Deadlines, document: str, minimum: int = MIN_DATED,
               now: datetime.datetime | None = None) -> Attachment | None:
    if len(deadlines.dated) < max(1, minimum):
        return None
    content = calendar(deadlines.dated, document, now)
    return Attachment(filename=filename_for(document), content_type=ICS_TYPE,
                      size_bytes=len(content), content=content)


def _undated_line(r: dict) -> str:
    where = r["where"][:1].upper() + r["where"][1:] if r["where"] else ""
    return f"{where + ': ' if where else ''}{r['what']}, {r['when']}"


def note(deadlines: Deadlines, name: str) -> str:
    """One sentence for the reply: what the calendar file holds, and which
    deadlines are not in it and why."""
    n = len(deadlines.dated)
    said = (f"{name} puts the {n} dated deadline{'s' if n != 1 else ''} in your "
            f"calendar, each with a reminder {REMINDER_DAYS} days before.")
    if deadlines.undated:
        listed = [_undated_line(r) for r in deadlines.undated[:UNDATED_LISTED]]
        more = len(deadlines.undated) - len(listed)
        said += (" Not in it, because they run from an event with no date yet: "
                 + "; ".join(listed) + (f"; and {more} more" if more > 0 else "") + ".")
    return said


# --------------------------------------------------------------------------
# On request: "send me the deadlines as a calendar"
# --------------------------------------------------------------------------


def handle(email: InboundEmail, provider, state=None) -> str:
    """The attached document's deadlines, or this conversation's document's,
    as a calendar file. Returns a status word for the job log."""
    from lra.pipeline import clean, extract, intake
    from lra.versions import _send

    if email.attachments:
        att = intake.pick_document(email)
    elif state is not None:
        from lra import thread

        content, _, _ = thread.rebuild(state)
        try:
            content = clean.clean(content).content      # as if every change were accepted
        except Exception:
            log.info("could not accept the changes; reading the redline", exc_info=True)
        att = Attachment(filename=state.filename, content_type="", size_bytes=len(content),
                         content=content)
    else:
        raise intake.Rejection(
            "Attach the document and I will send its deadlines back as a calendar file.")
    doc = extract.extract(att)
    found = read(doc)
    made = attachment(found, att.filename, minimum=1)
    n = len(found.dated)
    if made is None:
        verdict = f"{att.filename} dates none of its deadlines, so there is nothing to put in a calendar."
    else:
        verdict = f"{n} dated deadline{'s' if n != 1 else ''} from {att.filename}, as a calendar file."
    sections: list = [("verdict", verdict)]
    if found.dated:
        sections.append(("findings", "In the calendar", [
            (f"{e.day.day} {e.day:%B %Y}: {e.what}" + (f" ({e.where})" if e.where else ""), "")
            for e in found.dated]))
    if found.undated:
        sections.append(("findings", "Not dated, so not in it", [
            (_undated_line(r), "") for r in found.undated]))
    if made is not None:
        sections.append(("summary", (
            f"Open {made.filename} to add them; each has a reminder {REMINDER_DAYS} days "
            "before. Check each date against the signed version.")))
    provider.send(_send(email, verdict, sections, [made] if made else []))
    return "deadlines calendar"
