"""The seam between us and whichever mail vendor we end up on.

Everything above this line in the stack speaks InboundEmail/OutboundEmail only,
so swapping AgentMail for Postmark (or Cloudflare, or SES) is a one-file change.
"""

from __future__ import annotations

from typing import Any, Protocol

from lra.models import InboundEmail, OutboundEmail


class MailProvider(Protocol):
    name: str

    def verify(self, headers: dict[str, str], raw_body: bytes) -> bool:
        """Authenticate the webhook call itself. Reject anything unsigned."""
        ...

    def parse_inbound(self, payload: dict[str, Any]) -> InboundEmail:
        """Normalize the vendor's webhook JSON into our shape."""
        ...

    def send(self, email: OutboundEmail) -> str:
        """Send a reply. Returns the provider's message id."""
        ...
