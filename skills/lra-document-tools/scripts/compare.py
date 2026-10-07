"""Compare two Word documents into tracked changes. See SKILL.md."""

from __future__ import annotations

import argparse
from dataclasses import asdict

from _bootstrap import fail, finish, read, write


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("earlier")
    parser.add_argument("later")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    from lra.pipeline import compare

    try:
        result = compare.compare(read(args.earlier), read(args.later))
    except Exception as e:  # noqa: BLE001 - reported, never worked around
        fail(f"could not compare these files: {e}")
        return

    finish({
        "file": write(args.out, result.content) if result.content else None,
        "proven": result.proven,
        "identical": result.identical,
        "changes": [
            {k: v for k, v in asdict(c).items() if k not in ("risk", "impact", "response")}
            for c in result.changes
        ],
        "notes": result.notes,
    })


if __name__ == "__main__":
    main()
