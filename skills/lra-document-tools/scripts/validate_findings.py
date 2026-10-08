"""Check a review's findings and record them only if every one is readable. See SKILL.md.

The one way a review's findings reach the lawyer when no client is attached to
the session: the product reads `/mnt/session/outputs/findings.json` once the
session stops. This script is the only thing that writes that file, and it
writes it only when every finding passes the schema beside this skill
(`findings.schema.json`). Otherwise it writes nothing, prints what was wrong,
and exits non-zero, so an earlier good draft stays in place.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from _bootstrap import ROOT, fail, finish, write

OUTPUTS = "/mnt/session/outputs"
AGAIN = ("write the draft again and run validate_findings.py on it")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("draft", help="a JSON file: {summary, findings, research_notes}")
    parser.add_argument("--out", default=f"{OUTPUTS}/findings.json",
                        help="where the recorded findings go (default: the session outputs)")
    args = parser.parse_args()

    from secondeye.report import check_report, nothing_recorded

    schema = json.loads((ROOT / "findings.schema.json").read_text())
    try:
        draft = json.loads(Path(args.draft).read_text())
    except (OSError, ValueError) as e:
        fail(nothing_recorded([f"the draft could not be read as JSON: {e}"], AGAIN))
        return

    report, rejected = check_report(draft, schema)
    if report is None:
        fail(nothing_recorded(rejected, AGAIN))
        return

    written = write(args.out, json.dumps(report, ensure_ascii=False, indent=2).encode())
    count = len(report["findings"])
    finish({
        "recorded": count,
        "file": written,
        "message": f"Recorded {count} finding(s). Write the file again with the complete "
                   "list if anything changes; the last one recorded is the one that counts.",
    })


if __name__ == "__main__":
    main()
