"""Write a review's findings into a Word document as tracked changes. See SKILL.md.

This is the product's own writer, the one component that must never damage a
contract, running in the container on the findings the reviewer just
reported. It applies only the findings marked `auto_apply`, adds a Word
comment for each finding carrying a `question`, puts the reason for each
substantive change in the margin beside it, refuses ambiguous anchors
rather than guessing, and verifies its own output before writing it. The
JSON it prints is the record the lawyer's email is built from; the product
verifies the file again on its own side before it is attached.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from _bootstrap import fail, finish, read, write


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", help="the Word document to mark up (it is not modified)")
    parser.add_argument("--findings", required=True,
                        help="a JSON file holding the findings list as reported")
    parser.add_argument("--out", required=True, help="where to write the redline")
    args = parser.parse_args()

    from lra.models import Finding, Mode, ReviewResult
    from lra.pipeline import redline

    try:
        raw = json.loads(Path(args.findings).read_text())
    except (OSError, ValueError) as e:
        fail(f"could not read the findings JSON: {e}")
        return
    if isinstance(raw, dict):
        raw = raw.get("findings", [])
    if not isinstance(raw, list):
        fail("the findings file must hold a JSON list of finding objects")
        return

    findings: list = []
    unreadable: list[str] = []
    for i, item in enumerate(raw, start=1):
        try:
            findings.append(Finding(**item))
        except Exception as e:  # noqa: BLE001 - named in the output, never worked around
            unreadable.append(f"#{i}: {e}")
    if unreadable:
        fail("these findings could not be read; fix them and run again: "
             + "; ".join(unreadable))
        return

    original = read(args.input)
    result = ReviewResult(mode=Mode.REDLINE, summary="", findings=findings)
    try:
        out = redline.apply(original, result)
    except Exception as e:  # noqa: BLE001
        fail(f"the writer could not process this file: {e}")
        return

    def titles(items) -> list[str]:
        return [f.title for f in items]

    if not out.applied and not out.commented:
        finish({"file": None, "applied": [], "commented": [],
                "skipped": titles(out.skipped), "notes": out.notes, "verified": False,
                "reason": "nothing was marked auto_apply and no finding carried a question, "
                          "so there is nothing to write"})
        return

    ok, why = redline.verify(out.content, expect_revisions=bool(out.applied))
    if not ok:
        fail(f"the marked-up document did not verify ({why}); nothing was written")
        return

    finish({
        "file": write(args.out, out.content),
        "applied": titles(out.applied),
        "commented": titles(out.commented),
        "explained": titles(out.explained),
        "skipped": titles(out.skipped),
        "notes": out.notes,
        "verified": True,
    })


if __name__ == "__main__":
    main()
