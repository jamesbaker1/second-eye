"""What a recorded negotiation.json must be, held against the evidence it was written from.

Used on both sides of the sandbox: by validate_negotiation.py, before the file
is recorded, and by the host (secondeye.negotiation loads this file), before a word
of it reaches the lawyer, because the file came out of a sandbox that runs
model-written code. So both refuse the same things in the same words.

The model decides every status and every verdict. What is checked here is only
what can be checked without judgment: the shape, that every point we raised is
answered exactly once, that every change in their version is accounted for,
that each change and point cited exists, and that every claim quoted is in
their email word for word. Standard library only: the container may have no
JSON Schema package.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any

SCHEMA_FILE = Path(__file__).resolve().parent.parent / "negotiation.schema.json"

# A point's status that says their version moved at that clause, by where the
# point came from. An issues-list point or an email ask was not in the version
# we sent, so accepting it is a change; a tracked change of ours was, so
# rejecting it is one.
_NEEDS_A_CHANGE = {
    "issue": ("accepted", "partial", "changed"),
    "email": ("accepted", "partial", "changed"),
    "change": ("rejected", "partial", "changed"),
}

_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,),
    "array": (list,), "object": (dict,),
}


def schema() -> dict:
    return json.loads(SCHEMA_FILE.read_text())


def _is(value: Any, kind: str) -> bool:
    if kind in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, _TYPES.get(kind, (object,)))


def _problems(value: Any, spec: dict, where: str) -> list[str]:
    kind = spec.get("type")
    if kind and not _is(value, kind):
        return [f"{where} must be a {kind}, not {type(value).__name__}"]
    out: list[str] = []
    if "enum" in spec and value not in spec["enum"]:
        out.append(f"{where} is {value!r}; it must be one of {', '.join(spec['enum'])}")
    if kind == "string" and len(value.strip()) < spec.get("minLength", 0):
        out.append(f"{where} is empty")
    if kind == "array":
        if len(value) < spec.get("minItems", 0):
            out.append(f"{where} needs at least {spec['minItems']} item(s)")
        for i, item in enumerate(value):
            out += _problems(item, spec.get("items", {}), f"{where}[{i}]")
    if kind == "object":
        props = spec.get("properties", {})
        for name in spec.get("required", []):
            if value.get(name) is None:
                out.append(f"{where}.{name} is missing")
        for name, item in value.items():
            if name not in props:
                if spec.get("additionalProperties") is False:
                    out.append(f"{where}.{name} is not a field")
                continue
            if item is not None:
                out += _problems(item, props[name], f"{where}.{name}")
    return out


_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
                         " ": " "})


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").translate(_QUOTES)
    return re.sub(r"\s+", " ", text).strip().lower()


def _quoted_in(quote: str, text: str) -> bool:
    q = norm(quote).strip(" \"'")
    return bool(q) and q in norm(text)


def _without_nulls(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _without_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_without_nulls(v) for v in value]
    return value


def check(draft: Any, evidence: dict) -> tuple[dict | None, list[str]]:
    """(the outcome to record, []) or (None, every problem, one sentence each)."""
    if not isinstance(draft, dict):
        return None, [f"the draft must be a JSON object, not {type(draft).__name__}"]
    draft = _without_nulls(draft)
    problems = _problems(draft, schema(), "negotiation")
    if problems:
        return None, problems

    points = {p["id"]: p for p in evidence.get("points") or []}
    count = len(evidence.get("changes") or [])
    cover = (evidence.get("cover") or {}).get("text") or ""

    def bad_changes(indices: list[int], where: str) -> None:
        for i in indices:
            if not 0 <= i < count:
                problems.append(f"{where} cites change {i}, and the evidence has changes "
                                f"0 to {count - 1}" if count else
                                f"{where} cites change {i}, and there are no changes")

    answered: dict[str, list[str]] = {}
    cited: set[int] = set()
    for n, p in enumerate(draft["points"]):
        where = f"points[{n}] ({p['id']})"
        parent, _, part = p["id"].partition(".")
        raised = points.get(parent)
        if raised is None:
            problems.append(f"{where}: there is no point {parent} in the evidence")
            continue
        if part and (raised.get("source") != "email" or not part.isdigit()):
            problems.append(f"{where}: only a point from our covering email can be split "
                            f"into asks (P<n>.1, P<n>.2)")
        if part and not (p.get("ask") or "").strip():
            problems.append(f"{where}: an ask split from {parent} needs `ask`")
        answered.setdefault(parent, []).append(p["id"])
        bad_changes(p["changes"], where)
        cited.update(p["changes"])
        if p["status"] in _NEEDS_A_CHANGE.get(raised.get("source", "issue"), ()) \
                and not p["changes"]:
            problems.append(f"{where} is {p['status']}: cite the change that shows it")
        if p["mentioned"] and not cover.strip():
            problems.append(f"{where} is marked mentioned, and there is no covering email")

    for pid, raised in points.items():
        ids = answered.get(pid, [])
        if not ids:
            problems.append(f"point {pid} ({raised.get('clause') or 'no clause'}) "
                            "is not answered")
        elif len(ids) > 1 and pid in ids:
            problems.append(f"point {pid} is answered more than once")
        elif len(ids) != len(set(ids)):
            problems.append(f"an ask of point {pid} is answered more than once")

    for n, u in enumerate(draft["unrequested"]):
        bad_changes(u["changes"], f"unrequested[{n}] ({u['clause']})")
        cited.update(u["changes"])
        if u["mentioned"] and not cover.strip():
            problems.append(f"unrequested[{n}] is marked mentioned, and there is no "
                            "covering email")

    missed = [i for i in range(count) if i not in cited]
    if missed:
        listed = ", ".join(str(i) for i in missed[:12])
        problems.append(f"change(s) {listed} are not accounted for: each change is the "
                        "evidence for a point, or is listed under unrequested")

    for n, c in enumerate(draft["claims"]):
        where = f"claims[{n}]"
        if not cover.strip():
            problems.append(f"{where}: there is no covering email to quote")
            break
        if not _quoted_in(c["quote"], cover):
            problems.append(f"{where}: \"{c['quote'][:80]}\" is not word for word in "
                            "their covering email")
        bad_changes(c["changes"], where)
        for pid in c["points"]:
            if pid.partition(".")[0] not in points:
                problems.append(f"{where} cites {pid}, which is not a point in the evidence")
        if c["verdict"] in ("false", "partly") and not (c["points"] or c["changes"]):
            problems.append(f"{where} is {c['verdict']}: cite the points or changes "
                            "that show it")

    return (None, problems) if problems else (draft, [])


def nothing_recorded(problems: list[str]) -> str:
    """The refusal, in the words the agent is told on both sides."""
    head = f"NOTHING WAS RECORDED. {len(problems)} problem(s):"
    return "\n- ".join([head, *problems]) + (
        "\nFix the draft and run validate_negotiation.py on it again, with every point.")
