"""Mark a comment's thread resolved, or open it again. See SKILL.md."""

from __future__ import annotations

import argparse

from _bootstrap import carry, fail, finish, read, write


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", help="the working copy of the document")
    parser.add_argument("--id", required=True, help="any comment in the thread")
    parser.add_argument("--reopen", action="store_true", help="mark it open instead")
    parser.add_argument("--out", help="where to write the result (default: in place)")
    args = parser.parse_args()

    from secondeye.pipeline import wordcomments

    out = args.out or args.input
    try:
        content = wordcomments.resolve(read(args.input), args.id, done=not args.reopen)
    except wordcomments.CommentError as e:
        fail(str(e))
        return
    write(out, content)
    carry(args.input, out)
    finish({"file": out, "id": args.id, "resolved": not args.reopen})


if __name__ == "__main__":
    main()
