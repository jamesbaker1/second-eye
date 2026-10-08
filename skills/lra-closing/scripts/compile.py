"""Substitute the signed pages into each execution copy and render the closing
index. Refuses while any page is outstanding. See SKILL.md."""

from __future__ import annotations

import argparse
from pathlib import Path

from _bootstrap import fail, finish, load_json


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True)
    parser.add_argument("--dir", action="append", default=[],
                        help="where to find execution copies and signed pages (repeatable)")
    parser.add_argument("--out", default="/mnt/session/outputs")
    args = parser.parse_args()

    from secondeye.pipeline import closing_docs

    dirs = [Path(d) for d in (args.dir or ["/mnt/session/outputs", "/workspace"])]
    try:
        result = closing_docs.compile_set(load_json(args.state), dirs, Path(args.out))
    except closing_docs.ClosingError as e:
        fail(str(e))
        return
    except Exception as e:  # noqa: BLE001
        fail(f"could not compile the executed set: {e}")
        return
    finish(result)


if __name__ == "__main__":
    main()
