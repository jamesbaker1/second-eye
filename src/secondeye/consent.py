"""First-run consent, delivered by email.

The one moment this product asks for a browser. It happens once per lawyer, it
is triggered by an email they were already expecting a reply to, and after it
every review can see their matters.
"""

from __future__ import annotations

from secondeye import oauth
from secondeye.config import settings
from secondeye.models import InboundEmail, OutboundEmail
from secondeye.pipeline import reply


def needs_consent(user_address: str) -> bool:
    cfg = settings()
    if cfg.dms_provider.lower() == "none":
        return False
    from secondeye import dms_mcp

    # With the query binding the grant is a vault credential, and our token
    # table is not read; the capability fallback keeps the token with us.
    if dms_mcp.vault_held():
        return not dms_mcp.connected(user_address)
    return oauth.get(user_address, cfg.dms_provider.lower()) is None


def consent_email(inbound: InboundEmail, reason: str = "") -> OutboundEmail:
    cfg = settings()
    system = cfg.dms_provider.lower()
    url = oauth.consent_url(
        inbound.from_address,
        system,
        cfg.dms_authorize_url,
        cfg.dms_client_id,
        # The same redirect_uri the callback will send at the token exchange.
        # The authorization server compares them.
        oauth.callback_url(cfg.public_base_url),
    )
    body = f"""I can review this document on its own right now. To check it against
the firm's precedents and the other documents on this matter, I need permission
to search the document system as you.

{url}

That link connects your own account, read-only. I see exactly the matters you
can already open and nothing else. It takes one click and you will not be asked
again. It is good for a day; reply "connect" if it has gone stale and I will
send another.

If you would rather not, ignore this and I will keep reviewing documents on
their own. Reply "revoke" at any time to disconnect.
"""
    if reason:
        body = reason + "\n\n" + body
    return OutboundEmail(
        to=[inbound.from_address],
        # Threaded like every other reply. Appending "- one-time setup" made
        # Gmail file this beside the conversation instead of in it.
        subject=reply._subject(inbound.subject),
        text_body=body,
        html_body=reply.text_as_html(body),
        in_reply_to=inbound.message_id,
        thread_id=inbound.thread_id,
    )

