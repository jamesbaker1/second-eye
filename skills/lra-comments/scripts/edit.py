"""Make changes as tracked changes, with the product's own writer. See SKILL.md.

The writer is the one lra-document-tools' redline.py runs (pipeline/redline.py
over pipeline/ooxml.py): it refuses an anchor that is missing, ambiguous,
spans paragraphs or runs into someone else's tracked change, rather than
guessing, and verifies its output before anything is written.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from _bootstrap import author, carry, fail, finish, read, write


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", help="the working copy of the document")
    parser.add_argument("--changes", required=True,
                        help='a JSON file: [{"anchor", "text", "kind", "title"}, ...]')
    parser.add_argument("--author", help="who the tracked changes are from")
    parser.add_argument("--out", help="where to write the result (default: in place)")
    args = parser.parse_args()

    from secondeye.models import Finding, Mode, ReviewResult, Severity
    from secondeye.pipeline import redline

    try:
        raw = json.loads(Path(args.changes).read_text())
    except (OSError, ValueError) as e:
        fail(f"could not read the changes JSON: {e}")
        return
    if isinstance(raw, dict):
        raw = raw.get("changes", [])
    if not isinstance(raw, list) or not raw:
        fail("the changes file must hold a non-empty JSON list of changes")
        return

    findings, unreadable = [], []
    for i, change in enumerate(raw, start=1):
        if not isinstance(change, dict):
            unreadable.append(f"#{i} is not an object")
            continue
        anchor = str(change.get("anchor") or "").strip()
        text = str(change.get("text") or "").strip()
        kind = str(change.get("kind") or "replace")
        if not anchor or not text or kind not in ("replace", "insert_after"):
            unreadable.append(f"#{i} needs an anchor, a text and a kind of replace or "
                              "insert_after")
            continue
        findings.append(Finding(
            severity=Severity.SUBSTANTIVE, category="instruction",
            title=str(change.get("title") or f"Change {i}"), explanation="",
            anchor=anchor, suggested_text=text, auto_apply=True, edit_kind=kind))
    if unreadable:
        fail("nothing was changed; fix these and run again: " + "; ".join(unreadable))
        return

    result = ReviewResult(mode=Mode.REDLINE, summary="", findings=findings)
    try:
        out = redline.apply(read(args.input), result, author=author(args.author),
                            comment_questions=False)
    except Exception as e:  # noqa: BLE001
        fail(f"the writer could not process this file: {e}")
        return

    target = args.out or args.input
    if out.applied:
        write(target, out.content)
        carry(args.input, target)
    finish({
        "file": target if out.applied else None,
        "applied": [f.title for f in out.applied],
        "skipped": [f.title for f in out.skipped],
        "notes": out.notes,
        "verified": bool(out.applied),
    })


if __name__ == "__main__":
    main()
