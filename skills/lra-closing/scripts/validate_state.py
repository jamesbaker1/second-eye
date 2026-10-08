"""Check a closing update and, only if it passes, write closing.json.

The host runs the same checks again (src/secondeye/pipeline/closing_state.py) and
stores nothing that fails them, so an update this script refuses would be
refused there too. See SKILL.md and reference/state.md.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from _bootstrap import fail, finish, load_json


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--update", required=True,
                        help='{"schema", "state", "message", "attach"} as you drafted it')
    parser.add_argument("--previous", default="/workspace/closing-state.json")
    parser.add_argument("--outputs", default="/mnt/session/outputs")
    parser.add_argument("--held", default="/workspace",
                        help="the folder holding what the host already keeps")
    parser.add_argument("--write", default="/mnt/session/outputs/closing.json")
    args = parser.parse_args()

    from secondeye.pipeline import closing_state

    update = load_json(args.update)
    previous = load_json(args.previous) if Path(args.previous).is_file() else None
    outputs = {p.name for p in Path(args.outputs).iterdir() if p.is_file()} \
        if Path(args.outputs).is_dir() else set()
    outputs.discard(Path(args.write).name)
    held = {p.name for p in Path(args.held).iterdir() if p.is_file()} \
        if Path(args.held).is_dir() else set()
    errors = closing_state.validate_update(update, previous, outputs, held)
    if errors:
        fail("NOTHING WAS WRITTEN. Fix these and run this again:\n- " + "\n- ".join(errors))
    update["state"] = closing_state.normalise(update["state"])
    Path(args.write).parent.mkdir(parents=True, exist_ok=True)
    Path(args.write).write_text(json.dumps(update, indent=2, ensure_ascii=False))
    finish({"written": args.write, "tally": closing_state.tally(update["state"]),
            "checklist": closing_state.checklist_line(update["state"])})


if __name__ == "__main__":
    main()
