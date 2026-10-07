"""Dates and deadlines: every time limit in the document, in one table.

A lawyer reading a contract before it goes out wants to know, in one glance,
what falls due when: the long stop date, the notice window before renewal, the
cure period, the days to pay. That is exactly the reading a tired eye does
worst at 11pm, and it is mechanical, so it is done here rather than by the
model: the same document gives the same table every time.

Two outputs:

  rows(doc)       the table, for the reply. Informational, never a finding.
  conflicts(doc)  the few contradictions that are certain from the text alone,
                  as findings: a notice window longer than the term it must
                  precede, a long stop date before the agreement is dated, a
                  named period given two different lengths.

Conservative throughout (PRODUCT.md: false positives are existential). A row
is only made for a construction this module understands; a date is only
computed from an anchor the document itself dates, and never across business
days, whose holidays it cannot know. A conflict is raised only when it holds
on the most generous reading of both periods: three months is 84 to 93 days,
so "90 days' notice before the end of a 3-month term" is left alone.
"""

from __future__ import annotations

import calendar
import datetime
import html
import re
from dataclasses import dataclass

from lra.models import Finding, Severity
from lra.pipeline import checks
from lra.pipeline.extract import ExtractedDoc

TITLE = "Dates and deadlines"
# Rendered only when the table says more than one thing, and never longer than
# a phone screen: a table of forty dates is the document, not a summary of it.
MIN_ROWS = 2
SHOWN = 8


@dataclass
class Row:
    block: int
    where: str      # "clause 4.1", "Schedule 4, paragraph 3", or ""
    what: str       # plain English, at most eight words
    when: str       # the period or date as the document writes it
    date: str = ""  # computed from a date the document states, or ""
    kind: str = "period"
    # The row's day as YYYY-MM-DD, whether stated ("ends on 31 October 2027")
    # or computed: what the calendar file and the timeline sort by.
    iso: str = ""


@dataclass
class _Period:
    n: int | None       # None when the words and the numeral disagree
    unit: str           # day | business day | week | month | year
    text: str


# --------------------------------------------------------------------------
# Reading periods and dates
# --------------------------------------------------------------------------

_NUM_WORD = "|".join(sorted(list(checks._UNITS) + list(checks._TENS), key=len, reverse=True))
_WORDS = rf"(?:{_NUM_WORD})(?:[- ](?:{_NUM_WORD}|hundred|and)){{0,5}}"
_QTY = (
    rf"(?:(?P<words>\b{_WORDS})\s*\(\s*(?P<paren>\d{{1,4}})\s*\)"
    rf"|(?<![\d,.£$€])(?P<digits>\b\d{{1,4}})(?:\s*\(\s*(?P<words2>{_WORDS})\s*\))?"
    rf"|(?P<bare>\b{_WORDS}))"
)
_UNIT = (
    r"(?P<unit>(?:business|working|banking|calendar)\s+days?|days?|weeks?|months?|years?)"
    r"(?:['’]s?)?"
)
_PRE = (
    r"(?P<pre>(?:within|not\s+less\s+than|not\s+more\s+than|no\s+(?:less|more|later)\s+than|"
    r"not\s+later\s+than|at\s+least|for|during|of)\s+"
    r"(?:(?:a|an|the)\s+)?(?:further\s+|minimum\s+|maximum\s+)?(?:period\s+of\s+)?)?"
)
_DATE = rf"(?:{checks._LOOSE_DATE}|{checks._ANY_DATE})"
_DATE_RE = re.compile(_DATE)

# The thing a period runs from: stops at a clause break or a sentence end.
_REST = r"(?:[^,;:()\n.]|\.(?!\s|$)){1,90}"
_PERIOD = re.compile(rf"{_PRE}{_QTY}(?:\s+|-){_UNIT}(?![\w-])", re.IGNORECASE)

# What follows a period that says it is not a time limit.
_NOT_A_LIMIT = re.compile(r"^\s*(?:of\s+age|old\b|['’]?\s*experience|in\s+prison)",
                          re.IGNORECASE)

_REL = re.compile(
    r"^(?P<notice>(?:\s+(?:prior\s+)?(?:written\s+)?notice(?:\s+in\s+writing)?))?"
    r"\s+(?P<rel>after|from|of|following|before|prior\s+to|preceding|beginning\s+on|"
    r"commencing\s+on|starting\s+on)\s+"
    # A date is read whole, commas and all: "before October 31, 2027".
    rf"(?:(?:and\s+)?including\s+)?(?P<rest>(?:the\s+)?{_DATE}(?![\w/])|{_REST})",
    re.IGNORECASE,
)
_NOTICE_AFTER = re.compile(
    r"^\s+(?:prior\s+)?(?:written\s+)?notice(?:\s+in\s+writing)?\b", re.IGNORECASE)

_STOP = {"and", "or", "which", "that", "in", "to", "for", "unless", "provided",
         "together", "subject", "if", "at", "with", "under", "on", "by", "who",
         "is", "are", "shall", "will", "may", "must", "as", "but", "then", "has",
         "have", "was", "were", "being", "whichever", "save", "except",
         "requiring", "given", "served", "specifying"}

_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
             "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
             "fifteenth": 15, "twentieth": 20, "thirtieth": 30}
_ORD = rf"(?:{'|'.join(_ORDINALS)}|\d{{1,2}}(?:st|nd|rd|th))"
_ANNIVERSARY = re.compile(
    rf"(?P<pre>(?:on|after|before|by|from)\s+)?the\s+(?P<ord>{_ORD})\s+anniversary\s+of\s+"
    rf"(?P<rest>{_REST})",
    re.IGNORECASE,
)
_ORDINAL_DAY = re.compile(
    rf"(?P<pre>on\s+)?the\s+(?P<ord>{_ORD})(?:\s+\(\d{{1,2}}(?:st|nd|rd|th)\))?\s+"
    r"(?P<unit>business\s+day|working\s+day|day)\s+(?P<rel>after|following|before)\s+"
    rf"(?P<rest>{_REST})",
    re.IGNORECASE,
)

# The date the document is made. Only the opening words count: "dated", "made
# on", "entered into as of". A date anywhere else is not the agreement's.
_AGREEMENT_DATE = re.compile(
    rf"\b(?:is\s+)?(?:dated|made\s+(?:on|as\s+of)|(?:made\s+and\s+)?entered\s+into\s+"
    rf"(?:on|as\s+of))\s+(?:the\s+)?(?P<date>{_DATE})",
    re.IGNORECASE,
)
_AGREEMENT_DATE_REACH = 8
_AGREEMENT_ALIASES = ("date of this agreement", "date of this deed", "date of this lease",
                      "date of this letter", "date of this instrument",
                      "date first written above", "date first above written",
                      "date hereof", "date of signing of this agreement")

_DEFINED_DATE = re.compile(
    rf"[“\"](?P<term>[A-Z][A-Za-z\- ]{{1,40}}?(?:Date|Day|Deadline)s?)[”\"]\s+"
    rf"(?:means|shall\s+mean|is)\s+(?:the\s+)?(?:date\s+)?(?P<date>{_DATE})"
)
# A date the document ties to something happening, by the words before it:
#   start  "from", "with effect from", "commencing on", "shall commence on"
#   end    "ends on", "expires on", "shall terminate on", "until"
#   by     "by", "no later than", "on or before", "before", "prior to"
#   on     "on", when the sentence says something shall or will happen then
# Longest first, so "on or before" is never read as "on".
_START_WORDS = (
    r"with\s+effect\s+(?:on\s+and\s+)?from(?:\s+and\s+including)?|from\s+and\s+including|"
    r"(?:commencing|starting|beginning|effective)\s+(?:on\s+and\s+from|on|from|as\s+of)|"
    r"(?:shall\s+|will\s+)?(?:commences?|starts?|begins?)\s+on|between|from"
)
_END_WORDS = (
    r"(?:shall\s+|will\s+)?(?:end|ends|expire|expires|terminate|terminates)\s+"
    r"(?:automatically\s+)?on|(?:ending|expiring|terminating)\s+on|"
    r"(?:up\s+)?to\s+and\s+including|until|till"
)
_BY_WORDS = (
    r"on\s+or\s+before|on\s+or\s+prior\s+to|(?:by\s+)?no\s+later\s+than|not\s+later\s+than|"
    r"by|before|prior\s+to"
)
_DATED = re.compile(
    rf"\b(?:(?P<start>{_START_WORDS})|(?P<end>{_END_WORDS})|(?P<by>{_BY_WORDS})|(?P<on>on))\s+"
    rf"(?:the\s+)?(?P<date>{_DATE})(?![\w/])",
    re.IGNORECASE,
)
# The words that dated a deadline before any of the others were read. Only
# these may date a deadline before the agreement is (the stale date the
# timeline flags): "from 1 January 2025" before signing is history, "no later
# than 31 March 2026" before signing is a mistake.
_STRONG = re.compile(
    r"^(?:on\s+or\s+(?:before|prior\s+to)|(?:by\s+)?no[t]?\s+later\s+than|until|"
    r"(?:expires?|ending|terminates?)\s+on)$", re.IGNORECASE)
# "on", "from", "before" and "prior to" are everyday words. Before a date they
# set a deadline only in a sentence that says something is to happen.
_WEAK = re.compile(r"^(?:on|from|before|prior\s+to)$", re.IGNORECASE)
_TO_HAPPEN = re.compile(
    r"\b(?:shall|will|must|may|is\s+due|are\s+due|falls?\s+due|payable|is\s+to|are\s+to|"
    r"to\s+be|takes?\s+place|commences?|expires?|ends?|continues?|remains?|exercisable)\b",
    re.IGNORECASE)
# The dates after the first: "on 1 January 2027, 1 April 2027 and 1 July
# 2027", or the end of a window, "between 1 June 2027 and 30 June 2027".
_MORE_DATES = re.compile(
    rf"\s*(?P<sep>,\s*(?:and\s+|or\s+)?|\s+(?:and|or)\s+|\s+(?:to|until|till)\s+"
    rf"(?:and\s+including\s+)?|\s*[-–]\s*)(?:on\s+)?(?:the\s+)?(?P<date>{_DATE})(?![\w/])",
    re.IGNORECASE)
# A period just before the date: "not less than 3 months before 31 October
# 2027" is read as the period it is, with the date computed from it.
_PERIOD_BEFORE = re.compile(
    rf"{_QTY}(?:\s+|-){_UNIT}(?:\s+(?:prior\s+)?(?:written\s+)?notice(?:\s+in\s+writing)?)?"
    r"\s*$", re.IGNORECASE)
# A date by which something must have happened, or the deal may be ended:
# "If Completion has not occurred by 31 March 2027, either party may terminate".
_LAPSE = re.compile(
    r"\b(?:not|fails?\s+to|failure\s+to)\b[^.;]{0,80}?\b(?:occurred|take\s+place|taken\s+place|"
    r"been\s+(?:satisfied|fulfilled|waived)|satisfied|fulfilled|complete|completed|happened)\b",
    re.IGNORECASE)

_NAMED_HERE = re.compile(
    r"\s*\(\s*(?:the\s+|such\s+date\s+being\s+the\s+)?[“\"]"
    r"(?P<term>[A-Z][A-Za-z\- ]{1,40}?(?:Date|Day|Deadline))[”\"]\s*\)")

# Dates that fix a historic period rather than set a deadline: the date the
# accounts are drawn up to, the locked box. Before signing by design, and never
# something to diary.
_REFERENCE_TERM = re.compile(
    r"\b(?:(?:last|latest|management|annual|audited)\s+)?(?:accounts?|accounting|"
    r"balance\s+sheet|locked\s+box|reference|valuation)\s+(?:reference\s+)?date$",
    re.IGNORECASE)
# Defined dates that are deadlines or events the deal must reach: one of these
# dated before signing is a stale date to flag, not a reference to leave out.
_DEADLINE_TERM = re.compile(
    r"complet|clos|target|expir|terminat|payment|due\b|deadline|long|outside|"
    r"drop|end\b|renewal|review|commencement|start|exercise|delivery|option|conditions",
    re.IGNORECASE)

_LONG_STOP = re.compile(r"^(?:long[\s-]?stop|outside|drop[\s-]dead)\s+date$", re.IGNORECASE)


def _unit(raw: str) -> str:
    raw = re.sub(r"\s+", " ", raw.lower()).rstrip("s")
    if raw.endswith(" day") and not raw.startswith("calendar"):
        return "business day"
    return "day" if raw.startswith("calendar") else raw


def _quantity(m: re.Match) -> int | None:
    if m.group("paren"):
        n, words = int(m.group("paren")), m.group("words")
    elif m.group("digits"):
        n, words = int(m.group("digits")), m.group("words2")
    else:
        return checks._parse_number_words(m.group("bare"))
    if words is not None:
        spelled = checks._parse_number_words(words)
        if spelled is None or spelled != n:
            return None     # "thirty (13) days": the amounts check says so
    return n


def _ordinal(raw: str) -> int | None:
    raw = raw.lower()
    if raw in _ORDINALS:
        return _ORDINALS[raw]
    digits = re.match(r"\d+", raw)
    return int(digits.group(0)) if digits else None


def _parse(text: str, order: str | None) -> datetime.date | None:
    parts = checks._date_value(text, order)
    if parts:
        try:
            return datetime.date(*parts)
        except ValueError:
            return None
    return checks._parse_loose_date(text)


def _shift(d: datetime.date, n: int, unit: str) -> datetime.date:
    if unit == "day":
        return d + datetime.timedelta(days=n)
    if unit == "week":
        return d + datetime.timedelta(days=7 * n)
    months = n * 12 if unit == "year" else n
    year, month = divmod(d.month - 1 + months, 12)
    year += d.year
    return datetime.date(year, month + 1,
                         min(d.day, calendar.monthrange(year, month + 1)[1]))


def _render(d: datetime.date, american: bool) -> str:
    name = checks._MONTH_NAMES[d.month]
    return f"{name} {d.day}, {d.year}" if american else f"{d.day} {name} {d.year}"


def _anchor_words(rest: str) -> str:
    """The thing a period runs from, cut where the sentence moves on."""
    words = rest.split()
    kept: list[str] = []
    for i, w in enumerate(words[:7]):
        if i and w.lower().strip(".") in _STOP:
            break
        kept.append(w)
    return " ".join(kept).rstrip(".")


# Bounds in calendar days, for comparing periods written in different units.
_BOUNDS = {"day": (1, 1), "business day": (1, 1.6), "week": (7, 7),
           "month": (28, 31), "year": (365, 366)}


def _span(p: _Period) -> tuple[float, float] | None:
    if p.n is None:
        return None
    lo, hi = _BOUNDS[p.unit]
    extra = 4 if p.unit == "business day" else 0   # a holiday or two
    return p.n * lo, p.n * hi + extra


# --------------------------------------------------------------------------
# What a period is for
# --------------------------------------------------------------------------


def _sentence(text: str, start: int, end: int) -> str:
    """The sentence around a match: from the last full stop, semicolon or colon
    followed by a space, to the next."""
    left = max(text.rfind(s, 0, start) for s in (". ", "; ", ": "))
    right = [i for i in (text.find(s, end) for s in (". ", "; ", ": ")) if i != -1]
    return text[left + 1 if left != -1 else 0: min(right) if right else len(text)]


def _near(text: str, start: int, end: int) -> str:
    """The sentence up to the match, and a few words after it."""
    sentence = _sentence(text, start, end)
    head = text.find(sentence)
    return sentence[:max(0, end - head) + 40]


# Whole words only. "solicit" inside "Seller's Solicitors" made where
# Completion takes place a restrictive covenant, and "compete" is inside
# "competent authority".
_RESTRICTIVE = re.compile(
    r"\b(?:non-?)?solicit(?:s|ed|ing|ation)?\b|\bentice\b|"
    r"\b(?:non-?)?compet(?:e|es|ing|ition)\b|\brestrict(?:s|ed|ion|ions|ive)?\b",
    re.IGNORECASE)
# Completion as the event a period runs from ("24 months after Completion")
# says when the period starts, not that the period is a step in completing.
_RUNS_FROM_COMPLETION = re.compile(
    r"\b(?:after|following|from|of|before|prior\s+to|preceding)\s+(?:the\s+)?"
    r"(?:date\s+of\s+)?(?:completion|closing)\b(?!\s+(?:accounts|statement|date))",
    re.IGNORECASE)


def _classify(sentence: str, before: str, notice: bool, pre: str = "",
              local: str = "") -> tuple[str, str]:
    """(kind, plain-English what) for a period, from the words around it.

    The words nearest the period decide first: "paid in cash within five
    Business Days ..., together with a retention" is a payment, not the
    retention the sentence goes on to mention. Only when those say nothing is
    the whole sentence read.
    """
    if local and local != sentence:
        kind, what = _classify(local, before, notice, pre)
        if kind != "period":
            return kind, what
    s = sentence.lower()
    b = before.lower()[-40:]
    if notice:
        if "renew" in s:
            return "notice", "Notice to stop renewal"
        if re.search(r"terminat|\bend\b", s):
            return "notice", "Notice to terminate"
        return "notice", "Notice period"
    if "renew" in s and (re.search(r"successive|further|additional", s)
                         or re.search(r"renew\w*(?:\s+for)?\s*$", b)):
        return "renewal", "Renewal period"
    if re.search(r"uncured|remed|\bcure", s):
        return "cure", "Time to remedy a breach"
    if re.search(r"initial\s+term", b):
        return "term", "Initial term"
    if re.search(r"\bterm\b[^.]{0,20}$|(?:continues?|continue)\s+(?:in\s+force\s+)?(?:for\s*)?$|"
                 r"remains?\s+in\s+(?:full\s+)?(?:force|effect)(?:\s+and\s+effect)?\s+(?:for\s*)?$|"
                 r"(?:agreement|lease|deed)\s+(?:is\s+)?(?:shall\s+)?(?:continues?|lasts?)\b", b):
        return "term", "Term"
    if "liabilit" in s and re.search(r"before|preceding|prior", s):
        return "lookback", "Liability cap look-back"
    if "deemed" in s and re.search(r"receiv|served|given|delivered", s):
        return "receipt", "Notice deemed received"
    if "surviv" in s:
        return "survival", "Survival after expiry"
    if _RESTRICTIVE.search(s):
        return "restrictive", "Restrictive covenant period"
    if "claim" in s:
        return "claims", "Time limit for claims"
    if re.search(r"retention|escrow", s):
        return "retention", "Retention period"
    if "rent review" in s or "reviewed" in s:
        return "review", "Review date"
    if re.search(r"payable|\bpay\b|\bpaid\b|invoice", s):
        return "payment", "Payment due"
    if "confidential" in s:
        return "confidentiality", "Confidentiality period"
    if re.search(r"complet|closing", _RUNS_FROM_COMPLETION.sub(" ", s)):
        return "completion", "Completion step"
    limit = re.search(r"within|no later than|not later than", b + " " + pre.lower())
    return "period", "Time limit" if limit else "Period"


def _dated_what(group: str, sentence: str, before: str, near: str) -> tuple[str, str]:
    """(kind, plain-English what) for a date the document states, from the
    words that introduce it (start, end, by, on) and the sentence around it."""
    s, local = sentence.lower(), near.lower()
    lead = before[-200:].lower()
    lead = lead[max(lead.rfind(". "), lead.rfind("; ")) + 1:]
    if group in ("by", "on") and _LAPSE.search(lead) and re.search(
            r"terminat|rescind|lapse|cease", s):
        return "long-stop", "Long stop date"
    if re.search(r"\boption\b|\bexercis", local):
        return "option", {"start": "Option window opens", "end": "Option window closes",
                          "on": "Option exercise date"}.get(group, "Option exercise deadline")
    if group == "start":
        if re.search(r"payable|\bpay\b|\bpaid\b|\brent\b|fees?\b", local):
            return "start", "Payments start"
        return "start", "Start date"
    if group == "end":
        if re.search(r"renewal\s+(?:term|period)", local):
            return "term", "Renewal term ends"
        if re.search(r"\b(?:agreement|lease|licen[cs]e|contract|deed|term|appointment)\b", local):
            return "term", "Term ends"
        if re.search(r"\b(?:valid|open|available)\b", local):
            return "end", "Valid until"
        return "end", "Ends"
    # Notice given by the date, not "renews on 1 November unless notice ...".
    if re.search(r"\bnotice\b", lead):
        if "renew" in s:
            return "notice", "Notice to stop renewal"
        if re.search(r"terminat|\bbreak\b", s):
            return "notice", "Notice to terminate"
        return "notice", "Notice deadline"
    if re.search(r"\breview", local):
        return "review", "Rent review date" if re.search(r"\brent\b", s) else "Review date"
    if re.search(r"\brenew", local):
        return "renewal", "Renewal date"
    if group == "on" and re.search(r"\bbreak\b", s):
        return "break", "Break date"
    if group == "on" and re.search(r"terminat", local):
        return "term", "Termination date"
    kind, what = _classify(sentence, before, False, local=near)
    if kind == "completion":
        return kind, "Completion date"
    if what in ("Period", "Time limit"):
        return kind, "Due date" if group == "on" else "Deadline"
    return kind, what


def _window(what: str) -> tuple[str, str]:
    """The two ends of "between 1 June 2027 and 30 June 2027"."""
    if what.startswith("Option"):
        return "Option window opens", "Option window closes"
    return "Period starts", "Period ends"


# --------------------------------------------------------------------------
# The table
# --------------------------------------------------------------------------


class _Reader:
    """One pass over the document: rows, and the facts conflicts() needs."""

    def __init__(self, doc: ExtractedDoc):
        self.doc = doc
        self.order = checks._slash_order(doc)
        self.american = self.order == "mdy"
        self.labels = checks._locations(doc)
        self.rows: list[Row] = []
        self.anchors: dict[str, datetime.date] = {}
        self.agreement_date: tuple[datetime.date, str, int] | None = None
        self.defined_dates: list[tuple[str, datetime.date, str, int]] = []
        self.notices: list[tuple[_Period, str, int]] = []   # period, sentence, block
        self.terms: dict[str, list[tuple[_Period, int]]] = {}
        self._read()

    # -- dates the document states ------------------------------------------

    def _dates(self) -> None:
        taken: set[tuple[int, int]] = set()     # (block, offset) of dates already read
        for block in self.doc.blocks[:_AGREEMENT_DATE_REACH]:
            m = _AGREEMENT_DATE.search(block.text)
            if m:
                d = _parse(m.group("date"), self.order)
                if d:
                    self.agreement_date = (d, m.group("date"), block.index)
                    self.american = self.american or bool(re.match(r"[A-Z]", m.group("date")))
                    for alias in _AGREEMENT_ALIASES:
                        self.anchors[alias] = d
                    self._add(block.index, "Date of this agreement", m.group("date"),
                              kind="agreement-date", iso=d)
                    taken.add((block.index, m.start("date")))
                break
        for block in self.doc.blocks:
            for m in _DEFINED_DATE.finditer(block.text):
                d = _parse(m.group("date"), self.order)
                if not d:
                    continue
                term = m.group("term").strip()
                if term.endswith(("Dates", "Days", "Deadlines")):
                    term = term[:-1]        # “Review Dates” means ... and ...
                self.defined_dates.append((term, d, m.group(0), block.index))
                self.anchors.setdefault(term.lower(), d)
                kind = ("long-stop" if _LONG_STOP.match(term)
                        else "reference" if self._reference(term, d) else "date")
                self._add(block.index, term, m.group("date"), kind=kind, iso=d)
                taken.add((block.index, m.start("date")))
                for more, d2 in self._more_dates(block.text, m.end("date")):
                    self._add(block.index, term, more.group("date"), kind=kind, iso=d2)
                    taken.add((block.index, more.start("date")))
            for m in _DATED.finditer(block.text):
                if (block.index, m.start("date")) not in taken:
                    self._dated(block.index, block.text, m, taken)

    def _more_dates(self, text: str, end: int):
        """The dates listed after one: (match, date) for each that parses."""
        while True:
            more = _MORE_DATES.match(text, end)
            d = _parse(more.group("date"), self.order) if more else None
            if not d:
                return
            yield more, d
            end = more.end("date")

    def _dated(self, block: int, text: str, m: re.Match, taken: set) -> None:
        """A date the words before it make a deadline, a start or an end."""
        if checks._in_quotes(text, m.start()):
            return
        d = _parse(m.group("date"), self.order)
        if d is None:
            return
        group = next(g for g in ("start", "end", "by", "on") if m.group(g))
        words = re.sub(r"\s+", " ", m.group(group))
        if _PERIOD_BEFORE.search(text[max(0, m.start() - 80):m.start()]):
            return                  # "3 months before 31 October 2027": a period
        near = _near(text, m.start(), m.end())
        if _WEAK.match(words) and not _TO_HAPPEN.search(near):
            return                  # "On 1 March 2024 the Seller acquired ..."
        signed = self.agreement_date[0] if self.agreement_date else None
        if signed and d < signed and not _STRONG.match(words):
            return                  # history: the accounts "from 1 January 2025"
        more = list(self._more_dates(text, m.end("date")))
        window = group == "start" and more and not more[0][0].group("sep").strip().startswith(",")
        if words.lower() == "between" and not window:
            return                  # "between" one date and nothing
        taken.add((block, m.start("date")))
        named = _NAMED_HERE.match(text, m.end())
        if named:
            # "on or before 31 March 2027 (the “Long Stop Date”)": the clause
            # defines the date, so it is that date, whatever else the sentence
            # says ("no party shall have any claim").
            term = named.group("term").strip()
            self.defined_dates.append((term, d, m.group(0), block))
            self.anchors.setdefault(term.lower(), d)
            self._add(block, term, m.group(0), iso=d,
                      kind="long-stop" if _LONG_STOP.match(term) else "date")
            return
        sentence = _sentence(text, m.start(), m.end())
        kind, what = _dated_what(group, sentence, text[:m.start()], near)
        if kind == "long-stop":
            self.defined_dates.append(("Long stop date", d, m.group(0), block))
        if window:
            opens, closes = _window(what)
            end, d2 = more[0]
            self._add(block, opens, m.group(0), kind=kind, iso=d)
            self._add(block, closes, end.group(0), kind=kind, iso=d2)
            taken.add((block, end.start("date")))
            return
        self._add(block, what, m.group(0), kind=kind, iso=d)
        for extra, d2 in more:
            if extra.group("sep").strip().lower() in ("to", "until", "till", "-", "–"):
                break               # "on 1 June 2027 to 30 June 2027": not a list
            self._add(block, what, f"{words} {extra.group('date')}", kind=kind, iso=d2)
            taken.add((block, extra.start("date")))

    def _reference(self, term: str, d: datetime.date) -> bool:
        """A defined date that fixes a historic period, not a deadline: named
        as one (the Accounts Date), or dated before the agreement and named as
        nothing the deal has to reach."""
        if _REFERENCE_TERM.search(term):
            return True
        signed = self.agreement_date[0] if self.agreement_date else None
        return signed is not None and d < signed and not _DEADLINE_TERM.search(term)

    # -- periods -------------------------------------------------------------

    def _periods(self) -> None:
        for block in self.doc.blocks:
            text = block.text
            for m in _PERIOD.finditer(text):
                after = text[m.end():]
                if _NOT_A_LIMIT.match(after):
                    continue
                n = _quantity(m)
                period = _Period(n, _unit(m.group("unit")),
                                 text[m.end("pre") if m.group("pre") else m.start():m.end()])
                notice_words = _NOTICE_AFTER.match(after)
                notice = bool(notice_words)
                sentence = _sentence(text, m.start(), m.end())
                pre = m.group("pre") or ""
                if not pre and not notice and not _REL.match(after) and \
                        not re.search(r"(?:term|period)\s+(?:of|means|is)\s*$|"
                                      r"continues?\s+(?:in\s+force\s+)?for\s*$|"
                                      r"(?:successive|renew\w*(?:\s+for)?)\s*$",
                                      text[:m.start()], re.IGNORECASE):
                    # A bare "12 months" with nothing tying it to an event is
                    # a quantity, not a time limit.
                    continue
                kind, what = _classify(sentence, text[:m.start()], notice, pre,
                                       _near(text, m.start(), m.end()))
                when_end = m.end() + (notice_words.end() if notice_words else 0)
                noun = re.match(r"\s+(?:renewal\s+)?(?:periods?|terms?)\b", after)
                if noun and "-" in m.group(0):
                    when_end = m.end() + noun.end()     # "12-month periods"
                day = None
                rel = _REL.match(after)
                if rel:
                    anchor = _anchor_words(rel.group("rest"))
                    when_end = m.end() + rel.start("rest") + len(anchor)
                    day = self._compute(period, rel.group("rel").lower(), anchor, kind)
                    if kind == "term" and rel.group("rel").lower().split()[0] in (
                            "beginning", "commencing", "starting"):
                        what = "Initial term ends" if what == "Initial term" else "Term ends"
                    elif kind == "term" and day:
                        what = f"{what} ends"
                date = _render(day, self.american) if day else ""
                if kind == "notice":
                    self.notices.append((period, sentence, block.index))
                if kind in ("term", "renewal"):
                    self._record_term(text, m, period, kind, what)
                self._add(block.index, what, text[m.start():when_end], date, kind, day)
            for m in _ANNIVERSARY.finditer(text):
                n = _ordinal(m.group("ord"))
                anchor = _anchor_words(m.group("rest"))
                sentence = _sentence(text, m.start(), m.end())
                kind, what = _classify(sentence, text[:m.start()], False,
                                       local=_near(text, m.start(), m.end()))
                if what in ("Period", "Time limit"):
                    what = "Anniversary"
                day = self._compute(_Period(n, "year", ""), "after", anchor, kind) if n else None
                end = m.start("rest") + len(anchor)
                self._add(block.index, what, text[m.start():end],
                          _render(day, self.american) if day else "", kind, day)
            for m in _ORDINAL_DAY.finditer(text):
                anchor = _anchor_words(m.group("rest"))
                sentence = _sentence(text, m.start(), m.end())
                kind, what = _classify(sentence, text[:m.start()], False,
                                       local=_near(text, m.start(), m.end()))
                if what == "Period":
                    what = "Time limit"
                end = m.start("rest") + len(anchor)
                self._add(block.index, what, text[m.start():end], "", kind)

    def _record_term(self, text: str, m: re.Match, period: _Period, kind: str,
                     what: str) -> None:
        """Remember how long the term and renewal periods are, by name."""
        before = text[max(0, m.start() - 60):m.start()].lower()
        after = text[m.end():m.end() + 80]
        named = re.match(r"\s*(?:\(|,)\s*(?:each\s+(?:a|an)\s+|the\s+)?[“\"]([A-Z][A-Za-z\- ]{1,40})[”\"]",
                         after)
        if named:
            name = named.group(1).lower()
        elif kind == "renewal":
            name = "renewal period"
        elif "initial" in what.lower() or "initial term" in before:
            name = "initial term"
        else:
            name = "term"
        self.terms.setdefault(name, []).append((period, m.start()))

    def _compute(self, period: _Period, rel: str, anchor: str,
                 kind: str) -> datetime.date | None:
        if period.n is None or period.unit == "business day":
            return None
        key = anchor.lower()
        key = key.removeprefix("the ")
        start = None
        for alias, d in self.anchors.items():
            if key == alias or key.startswith(alias + " "):
                start = d
                break
        if start is None:
            written = re.sub(r"^the\s+", "", anchor.strip(), flags=re.IGNORECASE)
            dated = _DATE_RE.fullmatch(written)
            start = _parse(written, self.order) if dated else None
        if start is None:
            return None
        first = rel.split()[0]
        if first in ("before", "prior", "preceding"):
            return _shift(start, -period.n, period.unit)
        end = _shift(start, period.n, period.unit)
        if first in ("beginning", "commencing", "starting"):
            # Inclusive of the first day, so it ends the day before the
            # anniversary; and the start of the term is now a known date.
            for alias in ("start of the term", "commencement of the term",
                          "beginning of the term", "term commencement date"):
                self.anchors.setdefault(alias, start)
            end = end - datetime.timedelta(days=1)
            for alias in ("end of the term", "expiry of the term", "expiration of the term"):
                self.anchors.setdefault(alias, end + datetime.timedelta(days=1))
        return end

    def _add(self, block: int, what: str, when: str, date: str = "",
             kind: str = "period", iso: datetime.date | None = None) -> None:
        when = re.sub(r"\s+", " ", when).strip(" ,;")
        # "an initial term of twenty-four (24) months" is shown as the period.
        when = re.sub(r"^of\s+(?:(?:a|an|the)\s+)?", "", when)
        if len(when) > 90:
            when = when[:88].rsplit(" ", 1)[0] + "…"
        what = " ".join(what.split()[:8])
        if any(r.block == block and (r.when == when or when in r.when) for r in self.rows):
            return
        self.rows.append(Row(block, self.labels.get(block, ""), what, when, date, kind,
                             iso.isoformat() if iso else ""))

    def _read(self) -> None:
        self._dates()
        self._periods()
        position = {b.index: i for i, b in enumerate(self.doc.blocks)}
        self.rows.sort(key=lambda r: position.get(r.block, 0))


def rows(doc: ExtractedDoc) -> list[Row]:
    """Every date and time limit the document states, in document order."""
    if getattr(doc, "transcribed", False):
        return []
    return _Reader(doc).rows


# --------------------------------------------------------------------------
# Conflicts
# --------------------------------------------------------------------------

_BEFORE_END = re.compile(
    r"(?:before|prior\s+to|preceding)\s+(?:the\s+)?(?:end|expiry|expiration)\s+of\s+"
    r"(?:the\s+|any\s+|each\s+|a\s+)?(?P<what>(?:then[- ]current\s+)?[A-Za-z\- ]{2,30}?"
    r"(?:Term|term|Period|period))\b"
)


def conflicts(doc: ExtractedDoc) -> list[Finding]:
    """Contradictions in the document's own time limits that hold on any
    reading of the periods involved."""
    if getattr(doc, "transcribed", False):
        return []
    reader = _Reader(doc)
    return (_notice_longer_than_term(reader) + _long_stop_before_signing(reader)
            + _period_stated_twice(reader))


def _length(reader: _Reader, name: str) -> _Period | None:
    """The one length the document gives a term, or None if it gives more."""
    stated = reader.terms.get(name.lower()) or []
    lengths = {(p.n, p.unit) for p, _ in stated}
    return stated[0][0] if len(lengths) == 1 and stated[0][0].n else None


def _notice_longer_than_term(reader: _Reader) -> list[Finding]:
    out = []
    for notice, sentence, _ in reader.notices:
        span = _span(notice)
        if span is None:
            continue
        for m in _BEFORE_END.finditer(sentence):
            name = re.sub(r"^then[- ]current\s+", "", m.group("what").strip()).lower()
            term = _length(reader, name)
            if term is None and name in ("term", "renewal term"):
                term = _length(reader, "initial term") if name == "term" else \
                    _length(reader, "renewal period")
            term_span = _span(term) if term else None
            if term_span is None or span[0] <= term_span[1]:
                continue
            shown = re.sub(r"^then[- ]current\s+", "", m.group("what").strip())
            said = notice.text.strip()
            out.append(Finding(
                severity=Severity.SUBSTANTIVE,
                category="deadline",
                title=(f"{said[0].upper()}{said[1:]} notice is longer than the {shown} "
                       f"it must come before ({term.text.strip()})"),
                explanation=(
                    f"Notice has to be given {said} before the end of the {shown}, "
                    f"but the {shown} only lasts {term.text.strip()}, so the notice "
                    "can never be given in time. Shorten the notice or lengthen the "
                    "period."
                ),
                anchor=said,
                confidence=0.9,
            ))
            break
    return checks._dedupe(out)


def _long_stop_before_signing(reader: _Reader) -> list[Finding]:
    if reader.agreement_date is None:
        return []
    signed, signed_text, _ = reader.agreement_date
    out = []
    for term, d, shown, _ in reader.defined_dates:
        if not _LONG_STOP.match(term) or d >= signed:
            continue
        out.append(Finding(
            severity=Severity.BLOCKER,
            category="deadline",
            title=f"The {term} is before the date of this agreement",
            explanation=(
                f"The {term} is {_render(d, reader.american)}, but the agreement is "
                f"dated {signed_text}, so it has passed before the agreement is signed "
                "and either party could walk away at once. Usually a date carried "
                "over from an earlier draft."
            ),
            anchor=shown,
            confidence=0.9,
        ))
    return out


def _period_stated_twice(reader: _Reader) -> list[Finding]:
    """A named period given two different lengths: "an Initial Term of 12
    months" in one clause and "the Initial Term of 24 months" in another."""
    out = []
    for name, stated in reader.terms.items():
        if len(stated) < 2 or name in ("term",) and "initial term" in reader.terms:
            continue
        first = stated[0][0]
        for later, _ in stated[1:]:
            if first.n is None or later.n is None:
                continue
            a, b = _span(first), _span(later)
            if a is None or b is None or not (a[0] > b[1] or b[0] > a[1]):
                continue
            label = name.title() if name != "term" else "term"
            out.append(Finding(
                severity=Severity.SUBSTANTIVE,
                category="deadline",
                title=f"The {label} is {first.text.strip()} in one place and "
                      f"{later.text.strip()} in another",
                explanation=(
                    f"The document gives the {label} two different lengths, so the "
                    "dates that run from it are uncertain. Say which is meant."
                ),
                anchor=later.text.strip(),
                confidence=0.85,
            ))
            break
    return out


# --------------------------------------------------------------------------
# The reply section
# --------------------------------------------------------------------------


def section(table: list[Row] | None) -> tuple | None:
    """("deadlines", title, rows shown, how many more), or None when the
    table would say less than MIN_ROWS things."""
    if not table or len(table) < MIN_ROWS:
        return None
    return ("deadlines", TITLE, table[:SHOWN], max(0, len(table) - SHOWN))


def place(sections: list[tuple], table: list[Row] | None) -> list[tuple]:
    """After the findings: before the closing summary and footer."""
    made = section(table)
    if made is None:
        return sections
    at = len(sections)
    while at > 1 and sections[at - 1][0] in ("summary", "footer"):
        at -= 1
    return sections[:at] + [made] + sections[at:]


def _cap(where: str) -> str:
    return where[0].upper() + where[1:] if where else ""


def as_text(sec: tuple) -> list[str]:
    _, title, shown, more = sec
    lines = [title + ":"]
    for r in shown:
        head = f"{_cap(r.where)}: {r.what}" if r.where else r.what
        lines.append(f"  - {head} — {r.when}" + (f" ({r.date})" if r.date else ""))
    if more:
        lines.append(f"  ... and {more} more.")
    lines.append("")
    return lines


_CELL = "padding:3px 14px 3px 0;vertical-align:top;text-align:left"


def as_html(sec: tuple) -> str:
    """A plain table. Inline styles and attributes only: Outlook on Windows
    renders through Word, which ignores <style> blocks and most CSS."""
    _, title, shown, more = sec
    head = "".join(
        f'<th align="left" style="{_CELL};font-weight:600;border-bottom:1px solid #ccc">'
        f"{h}</th>" for h in ("Clause", "What", "When", "Date"))
    body = "".join(
        "<tr>" + "".join(
            f'<td style="{_CELL}">{html.escape(cell)}</td>'
            for cell in (_cap(r.where), r.what, r.when, r.date)
        ) + "</tr>"
        for r in shown
    )
    tail = (f'<p style="margin:0 0 1em;color:#555">and {more} more</p>' if more else "")
    return (
        f'<p style="margin:0 0 .3em;font-weight:600">{html.escape(title)}</p>'
        '<table cellpadding="0" cellspacing="0" border="0" '
        'style="border-collapse:collapse;margin:0 0 1em;font-size:14px">'
        f"<tr>{head}</tr>{body}</table>" + tail
    )
