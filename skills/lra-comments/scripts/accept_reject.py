"""Accept or reject other people's tracked changes, one by one, by id. See SKILL.md."""

from __future__ import annotations

import argparse
from io import BytesIO

from _bootstrap import carry, fail, finish, journal, read, write


def _ids(raw: str | None) -> list[str]:
    return [part.strip() for part in (raw or "").replace(" ", ",").split(",") if part.strip()]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", help="the working copy of the document")
    parser.add_argument("--accept", help="ids to accept, comma separated")
    parser.add_argument("--reject", help="ids to reject, comma separated")
    parser.add_argument("--list", action="store_true", help="only list the tracked changes")
    parser.add_argument("--out", help="where to write the result (default: in place)")
    args = parser.parse_args()

    from docx import Document

    from lra.pipeline import ooxml

    content = read(args.input)
    try:
        document = Document(BytesIO(content))
    except Exception as e:  # noqa: BLE001
        fail(f"could not open this document: {e}")
        return

    if args.list or not (args.accept or args.reject):
        finish({"tracked_changes": [c.as_dict() for c in ooxml.tracked_changes(document)],
                "decided_so_far": journal(args.input)})
        return

    try:
        done = ooxml.decide(document, accept=_ids(args.accept), reject=_ids(args.reject))
    except (ooxml.RevisionNotFound, ooxml.CannotDecide) as e:
        fail(f"{e}; nothing was changed")
        return
    buffer = BytesIO()
    document.save(buffer)
    out = args.out or args.input
    write(out, buffer.getvalue())
    carry(args.input, out, add=done)
    finish({"file": out, **done,
            "still_tracked": [c.as_dict() for c in ooxml.tracked_changes(
                Document(BytesIO(buffer.getvalue())))]})


if __name__ == "__main__":
    main()
