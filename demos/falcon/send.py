"""One step of the story as a ready-to-send .eml.

    python -m demos.falcon.send 4
    python -m demos.falcon.send 7b --in-reply-to "<id of the agent's reply to 7a>"
    python -m demos.falcon.send --list

Sending from Python would need the lawyer's Gmail OAuth, so this writes the
message instead, attachments and all, from the one allowlisted address to
Second Eye (or, for step 6, to Harrowgate with Second Eye on Bcc). Then:

- Apple Mail: open the .eml, then Message > Send Again.
- Outlook: open it, then Actions > Resend This Message.
- Gmail: there is no .eml import; forward it from Mail or Outlook, or copy
  the subject, body and attachments (all under build/) into a new message.
  For a step that replies to an earlier one, press Reply on the agent's
  reply in Gmail and paste the body: that threads it properly.
- Locally: `second-eye replay <file>.eml` runs it through the handler, sending
  nothing. The rehearsal (`python -m demos.falcon.rehearse`) does every step
  in order with the threading done for you.
"""

from __future__ import annotations

import argparse
import sys
from email.utils import parsedate_to_datetime
from pathlib import Path

from demos import identity
from demos.falcon import build, cast, mail

DEFAULT_OUT = build.DEFAULT_OUT / "emails"


def write(step_id: str, out: Path = DEFAULT_OUT, in_reply_to: str | None = None) -> Path:
    step = mail.step(step_id)
    files = build.build_files()
    out.mkdir(parents=True, exist_ok=True)
    target = out / build.eml_name(step)
    target.write_bytes(mail.build_message(step, files, in_reply_to=in_reply_to))
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write one step of Project Falcon as .eml.")
    parser.add_argument("step", nargs="?")
    parser.add_argument("--in-reply-to", help="the Message-ID of the agent's reply this "
                                              "answers, to thread it")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--list", action="store_true", help="list the steps")
    parser.add_argument("--example", action="store_true",
                        help="write it with the invented addresses (demos/identity.py), to look at")
    args = parser.parse_args(argv)
    if args.list or not args.step:
        for s in mail.steps():
            thread = f" (reply to {s['reply_to']})" if s.get("reply_to") else ""
            when = parsedate_to_datetime(s["when"]).strftime("%a %d %b %H:%M")
            print(f"{s['id']:>3}  {when}  {s['title']}{thread}")
        return 0
    if not args.example:
        identity.require(cast.IDENTITY, "python -m demos.falcon.send")
    path = write(args.step, args.out, args.in_reply_to)
    step = mail.step(args.step)
    print(path)
    if step.get("reply_to") and not args.in_reply_to:
        print(f"This step replies to step {step['reply_to']}: in Gmail, press Reply on the "
              "agent's reply to it and paste the body, or pass --in-reply-to.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
