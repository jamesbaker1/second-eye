"""The document's time limits as a timeline, and the orderings worth a look.

deadlines.py reads every date and time limit into the reply's table. This
puts the same reading in order: what is dated, what runs from which event and
for how long, and which of them sit in an order a lawyer would want to see
before signing:

  claims-before-release   the window for claims closes before the retention
                          or escrow it is secured on is released
  notice-longer-than-term a notice period longer than the term it must be
                          given within
  long-stop-before-signing a long stop date on or before the date of the
                          agreement
  date-before-signing     a deadline dated before the agreement is
  long-stop-before-date   a long stop date before another date the deal
                          needs to reach first (a target completion date)
  cure-longer-than-notice a breach can still be cured after the notice that
                          terminates for it has run out

These are flags, not findings. Whether a claims window shorter than the
retention is a mistake or the deal is for the model to judge, with the clause
in front of it; this module only does the arithmetic of dates, conservatively,
the way deadlines.py does it (a month is 28 to 31 days, a business day 1 to
1.6 calendar days, and a flag is raised only when it holds on every reading).
The review agent runs it through the lra-document-tools skill
(scripts/timeline.py).
"""

from __future__ import annotations

import datetime
import re

from secondeye.pipeline import deadlines
from secondeye.pipeline.extract import ExtractedDoc

CATEGORY = "timing"


def _norm_anchor(text: str) -> str:
    text = re.sub(r"\s+", " ", text.lower()).strip(" .,;")
    return re.sub(r"^(?:the|such|that)\s+", "", text)


def _iso(reader: deadlines._Reader, shown: str) -> str:
    if not shown:
        return ""
    d = deadlines._parse(shown, reader.order)
    return d.isoformat() if d else ""


def _period_of(when: str) -> tuple[deadlines._Period, str, str] | None:
    """(period, relation, event it runs from) for a row's words, if it is a
    period running from or to an event."""
    m = deadlines._PERIOD.search(when)
    if m:
        rel = deadlines._REL.match(when[m.end():])
        period = deadlines._Period(deadlines._quantity(m), deadlines._unit(m.group("unit")),
                                   m.group(0))
        if rel:
            return period, rel.group("rel").lower().split()[0], \
                deadlines._anchor_words(rel.group("rest"))
        return period, "", ""
    a = deadlines._ANNIVERSARY.search(when)
    if a:
        n = deadlines._ordinal(a.group("ord"))
        if n:
            return (deadlines._Period(n, "year", a.group(0)), "after",
                    deadlines._anchor_words(a.group("rest")))
    return None


def _dated(reader: deadlines._Reader, row: deadlines.Row) -> str:
    """The row's own date: computed, or the date its words give."""
    if row.iso:
        return row.iso
    if row.date:
        return _iso(reader, row.date)
    m = deadlines._DATE_RE.search(row.when)
    return _iso(reader, m.group(0)) if m else ""


def events(doc: ExtractedDoc) -> tuple[deadlines._Reader, list[dict]]:
    reader = deadlines._Reader(doc)
    out = []
    for row in reader.rows:
        parsed = _period_of(row.when)
        period, rel, anchor = parsed if parsed else (None, "", "")
        span = deadlines._span(period) if period else None
        out.append({
            "where": row.where,
            "what": row.what,
            "when": row.when,
            "kind": row.kind,
            "date": _dated(reader, row),
            "runs": ("before" if rel in ("before", "prior", "preceding") else
                     "after" if rel else ""),
            "from": anchor,
            "days": [span[0], span[1]] if span else None,
        })
    return reader, out


def build(doc: ExtractedDoc) -> dict:
    """The timeline and its flags, as plain data for the skill's script."""
    if getattr(doc, "transcribed", False):
        return {"agreement_date": "", "dated": [], "relative": [], "flags": []}
    reader, rows = events(doc)
    dated = sorted((r for r in rows if r["date"]), key=lambda r: r["date"])
    relative = [r for r in rows if not r["date"]]
    # Grouped by the event each period runs from, earliest first.
    relative.sort(key=lambda r: (_norm_anchor(r["from"]) or "~",
                                 r["days"][0] if r["days"] else 0))
    return {
        "agreement_date": reader.agreement_date[0].isoformat() if reader.agreement_date else "",
        "anchors": {name: d.isoformat() for name, d in reader.anchors.items()
                    if name not in deadlines._AGREEMENT_ALIASES},
        "dated": dated,
        "relative": relative,
        "flags": flags(reader, rows),
    }


# --------------------------------------------------------------------------
# Flags
# --------------------------------------------------------------------------


def _flag(kind: str, title: str, detail: str, *rows: dict, anchor: str = "") -> dict:
    return {"kind": kind, "title": title, "detail": detail,
            "where": [r["where"] for r in rows if r.get("where")],
            "anchor": anchor or (rows[0]["when"] if rows else "")}


def flags(reader: deadlines._Reader, rows: list[dict]) -> list[dict]:
    out: list[dict] = []
    out += _claims_before_release(rows)
    out += _cure_longer_than_notice(rows)
    out += _dated_order(reader, rows)
    for f in deadlines._notice_longer_than_term(reader):
        out.append({"kind": "notice-longer-than-term", "title": f.title,
                    "detail": f.explanation, "where": [], "anchor": f.anchor})
    for f in deadlines._long_stop_before_signing(reader):
        out.append({"kind": "long-stop-before-signing", "title": f.title,
                    "detail": f.explanation, "where": [], "anchor": f.anchor})
    return out


def _days(n: float) -> str:
    return f"{n:g} days"


def _claims_before_release(rows: list[dict]) -> list[dict]:
    out = []
    claims = [r for r in rows if r["kind"] == "claims" and r["runs"] == "after" and r["days"]]
    held = [r for r in rows if r["kind"] == "retention" and r["runs"] == "after" and r["days"]]
    for c in claims:
        for h in held:
            if _norm_anchor(c["from"]) != _norm_anchor(h["from"]) or not c["from"]:
                continue
            if c["days"][1] < h["days"][0]:
                out.append(_flag(
                    "claims-before-release",
                    f"Claims close ({c['when']}) before the retention is released "
                    f"({h['when']})",
                    f"The last day to bring a claim is at most {_days(c['days'][1])} after "
                    f"{c['from']}; the retention runs at least {_days(h['days'][0])}. "
                    "For the rest of the retention period there is nothing it can be "
                    "claimed against for.",
                    c, h))
    return out


def _cure_longer_than_notice(rows: list[dict]) -> list[dict]:
    out = []
    cures = [r for r in rows if r["kind"] == "cure" and r["days"]]
    notices = [r for r in rows if r["kind"] == "notice" and r["what"] == "Notice to terminate"
               and r["days"]]
    for c in cures:
        for n in notices:
            # The same provision: termination for the breach being cured. A
            # notice to terminate for convenience elsewhere is another right.
            if c["where"] and c["where"] == n["where"] and c["days"][0] > n["days"][1]:
                out.append(_flag(
                    "cure-longer-than-notice",
                    f"The cure period ({c['when']}) is longer than the notice to "
                    f"terminate ({n['when']})",
                    "Termination on notice takes effect before a breach has to be "
                    "cured, so the cure period may give the defaulting party nothing, "
                    "or the two clauses may be read against each other.",
                    c, n))
    return out


def _dated_order(reader: deadlines._Reader, rows: list[dict]) -> list[dict]:
    out = []
    signed = reader.agreement_date[0] if reader.agreement_date else None
    long_stops = [(t, d) for t, d, _, _ in reader.defined_dates if deadlines._LONG_STOP.match(t)]
    for term, d, shown, _ in reader.defined_dates:
        if deadlines._LONG_STOP.match(term):
            continue
        for stop, stop_date in long_stops:
            if stop_date < d:
                out.append({
                    "kind": "long-stop-before-date",
                    "title": f"The {stop} ({_show(reader, stop_date)}) is before the "
                             f"{term} ({_show(reader, d)})",
                    "detail": f"Either party may walk away on the {stop}, before the "
                              f"{term} is reached.",
                    "where": [], "anchor": shown,
                })
    if signed is not None:
        for r in rows:
            if r["kind"] in ("agreement-date", "long-stop", "reference") or not r["date"] \
                    or r["from"]:
                continue
            d = datetime.date.fromisoformat(r["date"])
            if d < signed:
                out.append(_flag(
                    "date-before-signing",
                    f"{r['what']} ({r['when']}) is before the date of this agreement",
                    f"The agreement is dated {_show(reader, signed)}; a deadline "
                    "before it has passed before anyone signs. Usually a date carried "
                    "over from an earlier draft.",
                    r))
    return out


def _show(reader: deadlines._Reader, d: datetime.date) -> str:
    return deadlines._render(d, reader.american)
