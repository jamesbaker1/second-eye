"""What a finished review is, checked the same way on both sides of the sandbox.

The review's findings used to arrive as the input to one custom tool,
`report_findings`, whose schema the platform enforced and whose handler, on
our side of the stream, validated every finding again and answered "NOTHING
WAS RECORDED" when one would not parse. A session with no client attached
cannot call a custom tool (it would wait for ever for an answer), so the same
check now runs in the sandbox as well: `validate_findings.py` in the
lra-document-tools skill reads the agent's draft, and only a draft in which
every finding passes is written to the session outputs. When one fails, the
agent is told the same thing the tool used to tell it, and tries again.

The schema is `skills/lra-document-tools/findings.schema.json`, held equal to
`models.Finding` by a test. It is checked here by hand, not with pydantic or a
JSON Schema library: the container may have neither, and pydantic's stand-in
there (skills/lra-document-tools/shims) validates nothing. This module is
bundled into the skill by `second-eye skills sync`, so it imports nothing beyond the
standard library.
"""

from __future__ import annotations

from typing import Any

# What JSON Schema's type names mean in Python. A bool is an int to Python and
# not a number to JSON Schema, so it is excluded where it would slip through.
_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


def _is(value: Any, kind: str) -> bool:
    if kind in ("number", "integer") and isinstance(value, bool):
        return False
    return isinstance(value, _TYPES.get(kind, (object,)))


def problems(value: Any, schema: dict, where: str = "") -> list[str]:
    """Every way `value` fails `schema`, one sentence each, naming the field.

    Only the parts of JSON Schema the findings schema uses: type, enum,
    properties, required, additionalProperties and items. A key whose value is
    null is treated as absent: the optional fields are optional, and a model
    writing `"question": null` means it has no question, not that it has a
    malformed one.
    """
    label = where or "the value"
    kind = schema.get("type")
    if kind and not _is(value, kind):
        return [f"{label} must be a {kind}, not {type(value).__name__}"]
    out: list[str] = []
    if "enum" in schema and value not in schema["enum"]:
        allowed = ", ".join(str(v) for v in schema["enum"])
        out.append(f"{label} is {value!r}; it must be exactly one of {allowed}")
    if kind == "object":
        props = schema.get("properties", {})
        present = {k: v for k, v in value.items() if v is not None}
        for key in schema.get("required", []):
            if key not in present:
                out.append(f"{key} is missing")
        if schema.get("additionalProperties") is False:
            for key in present:
                if key not in props:
                    out.append(f"{key} is not a field it has")
        for key, sub in props.items():
            if key in present:
                out += problems(present[key], sub, key)
    if kind == "array" and "items" in schema:
        for i, item in enumerate(value):
            out += problems(item, schema["items"], f"{label}[{i}]")
    return out


def check_report(report: Any, schema: dict) -> tuple[dict | None, list[str]]:
    """The report, cleaned, or the reasons it cannot be recorded.

    All or nothing, like the tool it replaces: one unreadable finding and
    nothing is recorded, because the summary was written about all of them and
    the verdict line the lawyer reads is computed from the list. Each reason
    names the finding by number and title, so the agent can find it.
    """
    if isinstance(report, list):
        # A bare list of findings is what redline.py takes; accept it here too
        # rather than fail a review over the wrapper.
        report = {"summary": "", "findings": report}
    if not isinstance(report, dict):
        return None, [f"the report must be a JSON object, not {type(report).__name__}"]

    # The report's own fields first, without the list, which is checked one
    # finding at a time below so each refusal can name its finding.
    outer = dict(schema,
                 properties={k: v for k, v in schema["properties"].items() if k != "findings"},
                 required=[r for r in schema.get("required", []) if r != "findings"])
    rest = {k: v for k, v in report.items() if k != "findings"}
    rejected = [f"the report: {p}" for p in problems(rest, outer, "the report")]

    findings = report.get("findings")
    if not isinstance(findings, list):
        rejected.append("the report: findings must be a list of finding objects")
        return None, rejected

    item_schema = schema["properties"]["findings"]["items"]
    cleaned: list[dict] = []
    for i, finding in enumerate(findings, start=1):
        title = str((finding or {}).get("title") or "untitled")[:80] \
            if isinstance(finding, dict) else "untitled"
        found = problems(finding, item_schema, "the finding")
        if found:
            rejected.append(f"#{i} ({title}): " + "; ".join(found))
            continue
        cleaned.append({k: v for k, v in finding.items() if v is not None})

    if rejected:
        return None, rejected
    return {
        "summary": report.get("summary") or "",
        "findings": cleaned,
        "research_notes": report.get("research_notes") or "",
    }, []


def nothing_recorded(rejected: list[str],
                     again: str = "write the draft again and run validate_findings.py on it"
                     ) -> str:
    """What the agent is told when its report is refused. The same words on
    both paths, so a prompt tuned against one is tuned against the other."""
    return (
        "NOTHING WAS RECORDED. These findings could not be read:\n"
        + "\n".join(rejected)
        + "\n\nseverity must be exactly one of blocker, substantive, style, "
        "formatting, and every finding needs category, title, explanation "
        f"and anchor. Fix them and {again} with the COMPLETE list, including "
        "the findings that were fine."
    )


__all__ = ["check_report", "nothing_recorded", "problems"]
