"""The evidence, clause by clause: each point we raised beside the changes at its clause. See SKILL.md.

The comparison lists changes in document order and the ledger lists points in
the order we raised them. Deciding what happened to each point means reading
the two side by side, so this prints them that way, and lists apart the
changes at clauses where we raised nothing: those are the first place to look
for what they changed without being asked.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

EVIDENCE = "/workspace/negotiation-evidence.json"
# "Clause 10.1: Liability cap", "under 10.1", "from 4 to 7", "Schedule 2".
_NUMBER = re.compile(r"\b((?:schedule|annex|exhibit|appendix)\s+\w+|\d+(?:\.\d+)*[A-Z]?)\b",
                     re.IGNORECASE)


def clause_of(text: str) -> str:
    """The clause a label names, to its top two levels: 10.1(a) and 10.1 are one
    clause here; 10.1 and 10.2 are not."""
    m = _NUMBER.search(text or "")
    if not m:
        return ""
    found = m.group(1).lower()
    if found[0].isdigit():
        return ".".join(found.split(".")[:2])
    return found


def group(evidence: dict) -> dict:
    by: dict[str, dict] = {}
    for p in evidence.get("points") or []:
        key = clause_of(p.get("clause", "")) or "(no clause)"
        by.setdefault(key, {"points": [], "changes": []})["points"].append(p)
    elsewhere = []
    for c in evidence.get("changes") or []:
        keys = {clause_of(w) for w in re.split(r"\bto\b", c.get("where", "")) if clause_of(w)}
        hit = [k for k in keys if k in by]
        for k in hit:
            by[k]["changes"].append(c)
        if not hit:
            elsewhere.append(c)
    return {
        "clauses": [{"clause": k, **v} for k, v in by.items()],
        "changes_at_clauses_we_did_not_raise": elsewhere,
        "cover": evidence.get("cover") or {},
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", default=EVIDENCE)
    args = parser.parse_args()
    try:
        evidence = json.loads(Path(args.evidence).read_text())
    except (OSError, ValueError) as e:
        print(json.dumps({"error": f"the evidence file could not be read: {e}"}))
        raise SystemExit(1) from e
    print(json.dumps(group(evidence), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
