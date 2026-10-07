"""Check the turned document against the one you were sent, and only then
write it to the outputs, with a record of what was done. See SKILL.md."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from _bootstrap import author, fail, finish, journal, read, write

RECORD = "turned.json"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("original", help="the document as you were sent it")
    parser.add_argument("work", help="your working copy, with everything done")
    parser.add_argument("--out", required=True, help="where the turned document goes")
    parser.add_argument("--blackline", help="also write our turn compared with their version")
    parser.add_argument("--author", help="who our replies and changes are from")
    args = parser.parse_args()

    from lra.pipeline import turning

    original, work = read(args.original), read(args.work)
    decided = journal(args.work)
    signer = author(args.author)
    try:
        exam = turning.examine(original, work, signer, decided["accepted"],
                               decided["rejected"])
    except Exception as e:  # noqa: BLE001
        fail(f"could not examine the working copy: {e}")
        return

    record_path = Path(args.out).parent / RECORD
    record = {**exam.as_dict(), "author": signer, "file": None, "blackline": None,
              "blackline_note": ""}
    if not exam.ok:
        write(str(record_path), json.dumps(record, ensure_ascii=False, indent=2).encode())
        fail("NOTHING WAS WRITTEN. " + exam.report()
             + "\nFix the working copy (start again from the original if you have to) "
               "and run finish.py again.")
        return

    record["file"] = write(args.out, work)
    if args.blackline:
        content, note = turning.blackline(original, work, signer)
        record["blackline_note"] = note
        if content:
            record["blackline"] = write(args.blackline, content)
    write(str(record_path), json.dumps(record, ensure_ascii=False, indent=2).encode())
    finish({"file": record["file"], "blackline": record["blackline"],
            "blackline_note": record["blackline_note"], "report": exam.report()})


if __name__ == "__main__":
    main()
