"""Run the send-moment demo here, with no email and no model.

    python -m demos.outbound            write the files, then print the reply
    python -m demos.outbound --build    only write the files

The email in files/email.eml goes through the same handler production runs,
with the console provider printing the reply instead of sending it. Nothing
reaches the network: the model is switched off for the run (as it is in
production until the account has credit), so the reply is the mechanical
review a lawyer gets today. The run's database is a temporary file, so the
demo leaves nothing behind and can be run again.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from pathlib import Path


def _isolate(folder: str) -> None:
    """Settings for a local run, whatever .env says: no model, no mail vendor,
    no sender allowlist, a database of its own."""
    os.environ.update({
        "DATABASE_URL": f"sqlite:///{Path(folder) / 'demo.sqlite3'}",
        "MAIL_PROVIDER": "console",
        "MAIL_AGENT_ADDRESS": "review@legal.example.com",
        "ALLOWED_SENDERS": "",
        "FIRM_DOMAINS": "firm.example",
        "ANTHROPIC_API_KEY": "",
        "TRIAGE": "rules",
    })


def main(argv: list[str]) -> int:
    from demos.outbound import build

    written = build.write()
    print("Wrote:")
    for path in written:
        print(f"  {path.relative_to(Path.cwd()) if path.is_relative_to(Path.cwd()) else path}")
    if "--build" in argv:
        return 0

    with tempfile.TemporaryDirectory() as folder:
        _isolate(folder)
        # The handler logs the model being unavailable as a failure, with a
        # traceback; for the demo that is expected, and only the reply matters.
        logging.disable(logging.CRITICAL)
        from secondeye.config import settings
        from secondeye.handler import handle
        from secondeye.mail.console import ConsoleProvider

        settings.cache_clear()
        console = ConsoleProvider()
        handle(console.parse_eml((build.FILES / "email.eml").read_bytes()), provider=console)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
