"""Every comment in a Word document, with its thread, and every tracked change.
See SKILL.md."""

from __future__ import annotations

import argparse

from _bootstrap import fail, finish, read


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", help="the Word document (it is not modified)")
    args = parser.parse_args()

    from io import BytesIO

    from docx import Document

    from lra.pipeline import ooxml, wordcomments

    content = read(args.input)
    try:
        comments = wordcomments.read(content)
        changes = ooxml.tracked_changes(Document(BytesIO(content)))
    except Exception as e:  # noqa: BLE001 - reported, never worked around
        fail(f"could not read this document: {e}")
        return

    finish({
        "comments": [c.as_dict() for c in comments],
        "tracked_changes": [c.as_dict() for c in changes],
        "open_threads": sum(1 for c in comments if not c.parent and not c.done),
        "authors": sorted({c.author for c in comments} | {c.author for c in changes}),
    })


if __name__ == "__main__":
    main()
