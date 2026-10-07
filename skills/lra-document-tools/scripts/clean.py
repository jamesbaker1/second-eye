"""Make a clean copy of a Word document. See SKILL.md."""

from __future__ import annotations

import argparse

from _bootstrap import fail, finish, read, write


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    from lra.pipeline import clean

    try:
        result = clean.clean(read(args.input))
    except clean.CannotClean as e:
        fail(str(e))
        return
    except Exception as e:  # noqa: BLE001
        fail(f"could not clean this file: {e}")
        return

    finish({
        "file": write(args.out, result.content) if result.changed else None,
        "already_clean": not result.changed,
        "removed": result.removed,
    })


if __name__ == "__main__":
    main()
