"""Turn one page of a returned scan or photo into a one-page PDF, and print
what can be read off it (its text layer and our stamp). See SKILL.md."""

from __future__ import annotations

import argparse
from pathlib import Path

from _bootstrap import fail, finish, read, write


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input")
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    from secondeye.pipeline import closing_docs

    try:
        pdf, info = closing_docs.receive(read(args.input), Path(args.input).name, args.page)
    except closing_docs.ClosingError as e:
        fail(str(e))
        return
    except Exception as e:  # noqa: BLE001
        fail(f"could not read {args.input}: {e}")
        return
    finish({"file": write(args.out, pdf), **info})


if __name__ == "__main__":
    main()
