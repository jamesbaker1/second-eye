"""The one email a slow review sends before its verdict.

A lawyer who forwards a draft and hears nothing for five minutes does not know
whether it arrived, and forwards it again. The review is usually back well
inside that, so nothing is said by default; only a model-backed review still
running after a short wait sends one line saying which file and roughly when.

This module words it. The Workflow sends it (cloudflare/src/review-flow.ts):
at most once per instance, through the outbox, and only from inside the wait
for the session, so never after the verdict or a failure reply. The timer
thread that used to send it from the container, with its lock and its guard
on every other send, was removed with the queue in docs/migration.md, phase 5.
"""

from __future__ import annotations

import math

from secondeye.models import InboundEmail, OutboundEmail


def wording(filename: str, seconds_left: float) -> str:
    """ "Reviewing Acme NDA.docx; back in about 4 minutes."

    A duration, not a clock time: the lawyer's time zone is unknown, and a
    clock time in the wrong one is worse than none. Rounded up, because
    arriving early is fine and arriving late breaks the only promise made.
    """
    minutes = max(1, math.ceil(max(0.0, seconds_left) / 60))
    return (f"Reviewing {filename}; back in about {minutes} "
            f"minute{'s' if minutes != 1 else ''}.")


def compose(inbound: InboundEmail, filename: str, seconds_left: float) -> OutboundEmail:
    """Threaded as a reply to the lawyer's message, like every other reply,
    so it sits in their conversation and does not start one."""
    from secondeye.pipeline import reply

    text = wording(filename, seconds_left)
    return OutboundEmail(
        to=[inbound.from_address],
        subject=reply._subject(inbound.subject),
        text_body=text + "\n",
        html_body=reply._as_html([("summary", text)]),
        in_reply_to=inbound.message_id,
        thread_id=inbound.thread_id,
    )
