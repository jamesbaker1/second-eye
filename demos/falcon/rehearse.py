"""Replay the whole of Project Falcon through the real handler, in order.

    python -m demos.falcon.rehearse --stub        scripted model, no network, free
    python -m demos.falcon.rehearse --live        the real Managed Agents path (needs
                                                  credit, and the agents applied)
    options: --out DIR   --until STEP   --only STEP[,STEP]

Each step of emails.yaml is built as the .eml a mailbox would receive (the
Bcc line gone, as delivery removes it), threaded under the agent's reply it
answers, parsed by the console provider exactly as `second-eye replay` parses a
file, and handed to `handler.handle`. Every reply is written to
out/<step>/: the text, the HTML, and each attachment. With --stub every reply
is also held to the step's headline lines, and the run fails if one is
missing.

The settings are production's (one allowlisted Gmail sender, replies to the
sender only, FIRM_DOMAINS the firm's own domain alone) with one change
RUNSHEET.md's setup also makes: the lawyer is a playbook admin. The database is a fresh SQLite file in the output folder every run.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from demos.falcon import build, cast, mail, signing

HERE = Path(__file__).resolve().parent
DEFAULT_OUT = HERE / "out"

SETTINGS = {
    "MAIL_PROVIDER": "console",
    "MAIL_AGENT_ADDRESS": cast.AGENT,
    "MAIL_AGENT_NAME": cast.AGENT_NAME,
    "FIRM_DOMAINS": cast.FIRM_DOMAIN,
    "ALLOWED_SENDERS": cast.LAWYER,
    "PLAYBOOK_ADMINS": cast.LAWYER,
    "ONE_TAP_LINKS": "false",
    "REPLY_POLICY": "sender_only",
    "EDGE_URL": "",
    "ARCHIVE_ENABLED": "false",
}


class Capture:
    """The console provider, keeping every reply instead of printing it."""

    def __init__(self) -> None:
        from secondeye.mail.console import ConsoleProvider

        self._console = ConsoleProvider()
        self.name = "console"
        self.sent: list = []
        self.ids: list[str] = []
        self.step = ""

    def parse_eml(self, raw: bytes):
        return self._console.parse_eml(raw)

    def verify(self, headers, raw_body) -> bool:
        return True

    def send(self, out) -> str:
        self.sent.append(out)
        sent_id = f"<falcon-reply-{self.step}-{len(self.sent)}@{cast.AGENT.split('@')[1]}>"
        self.ids.append(sent_id)
        return sent_id


def configure(out: Path, live: bool) -> dict[str, str | None]:
    """Settings for the run, as environment variables (they win over .env).
    Returns what they replaced, for `restore`."""
    from secondeye.config import settings

    env = dict(SETTINGS)
    env["DATABASE_URL"] = f"sqlite:///{out / 'rehearsal.sqlite3'}"
    if not live:
        from demos.falcon import stub

        env.update(stub.AGENT_IDS)
        env["ANTHROPIC_API_KEY"] = "sk-stub-no-network"
        env["ANTHROPIC_WORKSPACE_ID"] = ""
    before = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    settings.cache_clear()
    return before


def restore(before: dict[str, str | None]) -> None:
    from secondeye.config import settings

    for key, value in before.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    settings.cache_clear()


def _write_reply(folder: Path, n: int, out) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    head = [f"To: {', '.join(out.to)}", f"Subject: {out.subject}"]
    if out.in_reply_to:
        head.append(f"In-Reply-To: {out.in_reply_to}")
    (folder / f"reply-{n}.txt").write_text("\n".join(head) + "\n\n" + out.text_body)
    if out.html_body:
        (folder / f"reply-{n}.html").write_text(out.html_body)
    if out.attachments:
        files = folder / f"reply-{n}"
        files.mkdir(exist_ok=True)
        for a in out.attachments:
            (files / Path(a.filename).name).write_bytes(a.content)


def _returned_pages(step: dict, replies: dict[str, list], files: dict) -> dict:
    """The signed pages a step returns, made from the packets the agent
    actually sent at 7e when there are any: a live agent names its own
    signatories and pages, and the built files follow the scripted plan."""
    keys = [k for k in step.get("attachments") or [] if k.startswith("signed_")]
    sent = [a for out in replies.get("7e", []) for a in out.attachments
            if a.filename.startswith("Signature packet")]
    if not keys or not sent:
        return files
    from io import BytesIO

    from pypdf import PdfReader

    def pages(packets):
        return [(p.content, n, signing.SIGNERS.get(_signatory(p.filename), "Director"))
                for p in packets for n in range(2, len(PdfReader(BytesIO(p.content)).pages) + 1)]

    beta = [p for p in sent if "beta" in p.filename.lower()]
    ours = [p for p in sent if p not in beta]
    made = dict(files)
    if "signed_beta" in keys and beta:
        made["signed_beta"] = (files["signed_beta"][0], signing.sign_pages(pages(beta)))
    if "signed_northwind" in keys and ours:
        made["signed_northwind"] = (files["signed_northwind"][0],
                                    signing.sign_pages(pages(ours)))
    return made


def _signatory(filename: str) -> str:
    lowered = filename.lower()
    for sid in signing.SIGNERS:
        if sid.split("-")[0] in lowered:
            return sid
    return ""


def run(out: Path = DEFAULT_OUT, live: bool = False, until: str | None = None,
        only: list[str] | None = None, echo=print) -> dict:
    """Run the story. Returns {step id: {"replies": [...], "missing": [...]}}."""
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    saved = configure(out, live)
    try:
        return _run(out, live, until, only, echo)
    finally:
        restore(saved)


def _run(out: Path, live: bool, until: str | None, only: list[str] | None, echo) -> dict:
    from secondeye import clients, handler, managed

    files = build.build_files()
    if live and not managed.configured():
        raise SystemExit("--live needs the agents applied: ANTHROPIC_API_KEY and the "
                         "MANAGED_* ids in .env (second-eye skills sync; second-eye agents apply).")
    clients.register(cast.MATTER, cast.CLIENT, aliases=["Northwind"],
                     domains=[cast.CLIENT_DOMAIN])
    clients.link(cast.MATTER, cast.MATTER)

    provider = Capture()
    replies: dict[str, list] = {}
    reply_ids: dict[str, list[str]] = {}
    refs: dict[str, list[str]] = {}
    results: dict[str, dict] = {}

    stack = None
    script = None
    if not live:
        from demos.falcon import stub

        script = stub.Script()
        stack = stub.patches(script)
        stack.__enter__()
    try:
        for step in mail.steps():
            sid = str(step["id"])
            if only and sid not in only:
                continue
            in_reply_to, references = None, None
            parent = step.get("reply_to")
            if parent is not None:
                parent = str(parent)
                if not reply_ids.get(parent):
                    raise SystemExit(f"step {sid} answers step {parent}, which has no reply")
                in_reply_to = reply_ids[parent][-1]
                references = refs[parent] + [in_reply_to]
            refs[sid] = (references or []) + [mail.message_id(sid)]
            attached = _returned_pages(step, replies, files)
            raw = mail.build_message(step, attached, in_reply_to=in_reply_to,
                                     references=references, with_bcc=False)
            folder = out / sid
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "sent.eml").write_bytes(raw)

            provider.step = sid
            if script is not None:
                script.step = sid
            before = len(provider.sent)
            handler.handle(provider.parse_eml(raw), provider=provider)
            got = provider.sent[before:]
            replies[sid] = got
            reply_ids[sid] = provider.ids[before:]
            for n, reply in enumerate(got, 1):
                _write_reply(folder, n, reply)

            text = "\n".join(r.text_body for r in got) + "\n" + "\n".join(
                a.filename for r in got for a in r.attachments)
            missing = [h for h in step.get("headline") or [] if h not in text]
            missing += [f"(should not say) {h}" for h in step.get("absent") or []
                        if h in text]
            results[sid] = {"replies": got, "missing": missing,
                            "first": got[0].text_body.strip().splitlines()[0] if got else ""}
            mark = "ok " if not missing and got else "!! "
            echo(f"{mark}{sid:>3}  {step['title']}")
            echo(f"       {results[sid]['first'] or '(no reply)'}")
            for m in missing:
                echo(f"       missing: {m}")
            if until and sid == str(until):
                break
    finally:
        if stack is not None:
            stack.__exit__(None, None, None)
    if script is not None:
        results["_calls"] = {"calls": script.calls}
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rehearse Project Falcon end to end.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--stub", action="store_true", help="scripted model (the default)")
    mode.add_argument("--live", action="store_true", help="the real Managed Agents path")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--until", help="stop after this step")
    parser.add_argument("--production", action="store_true",
                        help="production's settings exactly: now always the case, so "
                             "this changes nothing")
    parser.add_argument("--only", help="comma-separated steps to run (threads need their "
                                       "parents)")
    args = parser.parse_args(argv)
    results = run(args.out, live=args.live, until=args.until,
                  only=args.only.split(",") if args.only else None)
    failed = [s for s, r in results.items() if not s.startswith("_")
              and (r["missing"] or not r["replies"])]
    print(f"\nReplies are in {args.out}/<step>/.")
    if failed and not args.live:
        print(f"{len(failed)} step(s) did not show their headline: {', '.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
