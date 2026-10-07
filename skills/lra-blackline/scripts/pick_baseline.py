"""List the versions of the document this conversation has seen, and which
of them the lawyer's words point at. See SKILL.md.

    python scripts/pick_baseline.py /workspace/blackline-versions.json --ask "against what we sent on Tuesday"

It suggests; it does not decide. "Tuesday" when two versions arrived on
Tuesday, "the last one" when the lawyer means theirs and not ours: you read
the list and choose, and say why when it was not obvious.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import date, datetime
from pathlib import Path

from _bootstrap import fail, finish

_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_VERSION = re.compile(r"\b(?:v|version|draft|turn|rev(?:ision)?)\s*\.?\s*(\d{1,3})\b", re.IGNORECASE)
_ORIGINAL = re.compile(r"\b(?:original|first\s+(?:draft|version|one|turn)|very\s+first)\b", re.IGNORECASE)
_OURS = re.compile(r"\b(?:what\s+we\s+sent|we\s+sent|our\s+(?:draft|version|turn|last|redline|"
                   r"markup|mark-up)|the\s+(?:one|version|draft)\s+we\s+(?:sent|returned)|"
                   r"what\s+i\s+sent|i\s+sent)\b", re.IGNORECASE)
_THEIRS = re.compile(r"\b(?:their|they\s+sent|the\s+other\s+side|opposing\s+counsel|"
                     r"counterparty)\b", re.IGNORECASE)
_LAST = re.compile(r"\b(?:last|previous|prior|latest|most\s+recent)\s+"
                   r"(?:one|version|draft|turn|round)\b", re.IGNORECASE)
_SPLIT = re.compile(r"\b(?:against|versus|vs\.?|compared\s+(?:to|with)|to|with|since|from)\b",
                    re.IGNORECASE)
_DATE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + "|".join(_MONTHS) + r")[a-z]*\b"
                   r"|\b(" + "|".join(_MONTHS) + r")[a-z]*\s+(\d{1,2})(?:st|nd|rd|th)?\b", re.IGNORECASE)


def _when(entry: dict) -> date | None:
    try:
        return datetime.fromisoformat(entry.get("when", "")).date()
    except ValueError:
        return None


def _reasons(entry: dict, words: str, today: date) -> list[str]:
    """Why these words could mean this version."""
    out: list[str] = []
    for m in _VERSION.finditer(words):
        if entry.get("version") == int(m.group(1)):
            out.append(f"the file name says version {m.group(1)}")
        elif entry.get("version") is None and entry.get("ordinal") == int(m.group(1)):
            out.append(f"it is version {m.group(1)} in the order this conversation saw them")
    if _ORIGINAL.search(words) and entry.get("ordinal") == 1:
        out.append("it is the first version this conversation saw")
    if _OURS.search(words) and entry.get("source") in ("sent", "returned"):
        out.append("it is what we sent" if entry["source"] == "sent"
                   else "it is our redline as we returned it")
    if _THEIRS.search(words) and entry.get("source") == "received":
        out.append("it is a version that came in")
    day = _when(entry)
    if day is not None:
        lowered = words.lower()
        for index, name in enumerate(_DAYS):
            if re.search(rf"\b{name}\b", lowered) and day.weekday() == index \
                    and 0 <= (today - day).days <= 7:
                out.append(f"it is dated {name.title()} {day.isoformat()}")
        if re.search(r"\byesterday\b", lowered) and (today - day).days == 1:
            out.append("it is dated yesterday")
        if re.search(r"\blast\s+week\b", lowered) and 7 <= (today - day).days <= 14:
            out.append("it is dated last week")
        for m in _DATE.finditer(words):
            number = m.group(1) or m.group(4)
            month = (m.group(2) or m.group(3) or "").lower()[:3]
            if number and month and day.day == int(number) and _MONTHS[day.month - 1] == month:
                out.append(f"it is dated {day.isoformat()}")
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("versions", help="the versions file the host mounted")
    parser.add_argument("--ask", default="", help="the lawyer's words about which versions")
    parser.add_argument("--today", default="", help="YYYY-MM-DD; defaults to the file's")
    args = parser.parse_args()

    try:
        listing = json.loads(Path(args.versions).read_text())
    except (OSError, ValueError) as e:
        fail(f"could not read {args.versions}: {e}")
        return
    versions = listing.get("versions") or []
    if not versions:
        fail("there are no versions in the file")
        return
    try:
        today = date.fromisoformat(args.today or listing.get("today", "")[:10])
    except ValueError:
        today = datetime.now().astimezone().date()

    words = args.ask or listing.get("ask", "")
    # "v4 against v1": the words before "against" name the later version,
    # the words after it the baseline. With no split word, everything is
    # about the baseline.
    parts = _SPLIT.split(words, maxsplit=1)
    later_words, baseline_words = (parts[0], parts[1]) if len(parts) == 2 else ("", words)
    wants_previous = bool(_LAST.search(baseline_words))

    current = next((v for v in versions if v.get("current")), versions[-1])
    position = versions.index(current)
    previous = versions[position - 1] if position > 0 else None
    listed = []
    for v in versions:
        as_baseline = _reasons(v, baseline_words, today)
        if wants_previous and v is previous:
            as_baseline.append("it is the version before the current one")
        as_later = _reasons(v, later_words, today) if later_words.strip() else []
        listed.append({**{k: v.get(k) for k in ("id", "file", "filename", "label", "source",
                                                 "from", "day", "ordinal", "version",
                                                 "current")},
                       "baseline_because": as_baseline, "later_because": as_later})

    laters = [e for e in listed if e["later_because"]]
    later = laters[0] if len(laters) == 1 else next(e for e in listed if e["id"] == current["id"])
    # The baseline is never the later version, and the version the most
    # reasons point at wins; a tie is for you to break.
    baselines = sorted((e for e in listed if e["baseline_because"] and e is not later),
                       key=lambda e: len(e["baseline_because"]), reverse=True)
    tied = len(baselines) > 1 and (len(baselines[0]["baseline_because"])
                                   == len(baselines[1]["baseline_because"]))
    baseline = baselines[0] if baselines and not tied else None
    finish({
        "versions": listed,
        "suggested": {"earlier": baseline["id"] if baseline else None, "later": later["id"]},
        "ambiguous": baseline is None or len(laters) > 1,
        "why": ("more than one version fits the words equally" if tied else
                "no version fits the words" if not baselines else
                "one version fits the words best" if len(baselines) > 1 else
                "one version fits the words"),
    })


if __name__ == "__main__":
    main()
