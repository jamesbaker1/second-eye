"""What changed between two Word versions, counted, as JSON. See SKILL.md.

    python scripts/summary.py "/workspace/V1 SPA v1.docx" "/workspace/V4 SPA v4.docx"

Counts by kind and by clause, and every change with its index, clause and a
short before and after. Read it to decide which changes are material and
what each one does; the counts are the product's, not yours, and go into the
PDF and the email exactly as printed here.
"""

from __future__ import annotations

import argparse

from _bootstrap import fail, finish, read


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("earlier")
    parser.add_argument("later")
    args = parser.parse_args()

    from lra.pipeline import blackline_pdf, compare

    earlier, later = read(args.earlier), read(args.later)
    try:
        result = compare.compare(earlier, later)
    except Exception as e:  # noqa: BLE001 - reported, never worked around
        fail(f"could not compare these files: {e}")
        return
    summary = blackline_pdf.summarise(result)
    finish({"earlier": args.earlier, "later": args.later, "identical": result.identical,
            **summary})


if __name__ == "__main__":
    main()
