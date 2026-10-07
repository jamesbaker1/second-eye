"""Check a negotiation draft against the evidence and record it only if it holds. See SKILL.md.

The only thing that writes /mnt/session/outputs/negotiation.json. It writes
nothing when any check fails, prints what was wrong, and exits non-zero, so a
good file recorded earlier stays in place.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rules

OUTPUTS = "/mnt/session/outputs"
EVIDENCE = "/workspace/negotiation-evidence.json"


def say(payload: dict, code: int = 0) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    raise SystemExit(code)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("draft", help="a JSON file shaped as negotiation.schema.json says")
    parser.add_argument("--evidence", default=EVIDENCE,
                        help=f"the evidence file you were given (default {EVIDENCE})")
    parser.add_argument("--out", default=f"{OUTPUTS}/negotiation.json",
                        help="where the recorded file goes (default: the session outputs)")
    args = parser.parse_args()

    try:
        evidence = json.loads(Path(args.evidence).read_text())
    except (OSError, ValueError) as e:
        say({"error": f"the evidence file could not be read: {e}"}, 1)
    try:
        draft = json.loads(Path(args.draft).read_text())
    except (OSError, ValueError) as e:
        say({"error": rules.nothing_recorded([f"the draft could not be read as JSON: {e}"])}, 1)

    outcome, problems = rules.check(draft, evidence)
    if outcome is None:
        say({"error": rules.nothing_recorded(problems)}, 1)

    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(outcome, ensure_ascii=False, indent=2))
    accepted = sum(1 for p in outcome["points"] if p["status"] == "accepted")
    say({
        "recorded": str(target),
        "points": len(outcome["points"]),
        "accepted": accepted,
        "unrequested": len(outcome["unrequested"]),
        "claims": len(outcome["claims"]),
        "message": "Recorded. Run it again with the whole file if anything changes; the "
                   "last one recorded is the one that counts.",
    })


if __name__ == "__main__":
    main()
