from lra.config import settings
from lra.mail.base import MailProvider

KNOWN = ("agentmail", "postmark", "cloudflare", "console")


def _missing(pairs: list[tuple[str, str]]) -> list[str]:
    return [name for name, value in pairs if not (value or "").strip()]


def _check_credentials(name: str) -> None:
    """Refuse to run a vendor provider that is only half configured.

    A missing webhook secret means verify() rejects every message and the firm
    sees silence; a missing API key means we accept mail and fail to send the
    review. Neither is visible from /health, which reports the provider name
    and "ok" either way. The one moment an operator is looking is startup, so
    the complaint belongs here, naming the exact variables to set.
    """
    cfg = settings()
    required = {
        "agentmail": [
            ("AGENTMAIL_WEBHOOK_SECRET", cfg.agentmail_webhook_secret),
            ("AGENTMAIL_API_KEY", cfg.agentmail_api_key),
            ("AGENTMAIL_INBOX_ID", cfg.agentmail_inbox_id),
        ],
        "postmark": [
            ("POSTMARK_INBOUND_SECRET", cfg.postmark_inbound_secret),
            ("POSTMARK_SERVER_TOKEN", cfg.postmark_server_token),
        ],
        "cloudflare": [
            ("EDGE_URL", cfg.edge_url),
            ("EDGE_SECRET", cfg.edge_secret),
        ],
    }.get(name, [])
    missing = _missing(required)
    if missing:
        raise RuntimeError(
            f"MAIL_PROVIDER={name} but {', '.join(missing)} "
            f"{'is' if len(missing) == 1 else 'are'} not set. Refusing to start: "
            "an unconfigured vendor provider rejects or drops every message "
            "while still reporting healthy."
        )


def get_provider() -> MailProvider:
    """Resolve the configured provider, or refuse to run.

    A typo in MAIL_PROVIDER used to fall through to the console provider, which
    accepts unsigned webhooks and prints replies instead of sending them. The
    service would start, report healthy, and quietly do nothing while accepting
    unauthenticated requests. Failing loudly is the only safe default.
    """
    name = settings().mail_provider.strip().lower()
    if name == "agentmail":
        from lra.mail.agentmail import AgentMailProvider

        _check_credentials(name)
        return AgentMailProvider()
    if name == "postmark":
        from lra.mail.postmark import PostmarkProvider

        _check_credentials(name)
        return PostmarkProvider()
    if name == "cloudflare":
        from lra.mail.cloudflare import CloudflareProvider

        _check_credentials(name)
        return CloudflareProvider()
    if name == "console":
        from lra.mail.console import ConsoleProvider

        return ConsoleProvider()
    raise RuntimeError(
        f"MAIL_PROVIDER is {settings().mail_provider!r}, which is not one of "
        f"{', '.join(KNOWN)}. Refusing to start rather than silently falling back."
    )
