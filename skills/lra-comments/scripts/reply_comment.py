"""Reply in a comment's thread, as Word does, optionally resolving it. See SKILL.md."""

from __future__ import annotations

import argparse

from _bootstrap import author, carry, fail, finish, read, write


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", help="the working copy of the document")
    parser.add_argument("--id", required=True, help="the comment to reply to")
    parser.add_argument("--text", required=True, help="the reply")
    state = parser.add_mutually_exclusive_group()
    state.add_argument("--resolve", action="store_true",
                       help="mark the thread resolved at the same time")
    state.add_argument("--open", action="store_true",
                       help="leave (or set) the thread open, e.g. when the reply asks a question")
    parser.add_argument("--author", help="who the reply is from")
    parser.add_argument("--out", help="where to write the result (default: in place)")
    args = parser.parse_args()

    from lra.pipeline import wordcomments

    out = args.out or args.input
    resolve = True if args.resolve else (False if args.open else None)
    try:
        content, new_id = wordcomments.reply(read(args.input), args.id, args.text,
                                             author(args.author), resolve=resolve)
    except wordcomments.CommentError as e:
        fail(str(e))
        return
    thread = next(c for c in wordcomments.read(content) if c.id == new_id)
    write(out, content)
    carry(args.input, out)
    finish({"file": out, "reply_id": new_id, "thread": thread.parent,
            "resolved": thread.done})


if __name__ == "__main__":
    main()
