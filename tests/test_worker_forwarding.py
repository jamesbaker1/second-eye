"""Every setting the app reads reaches the container on Cloudflare.

The container sees only the Worker vars named in FORWARDED
(cloudflare/src/index.ts). A setting missing from that list can be set in
wrangler.jsonc and still never take effect, which is how the playbook,
comments, closing and blackline agents went unconfigurable in production.
"""

import re
from pathlib import Path

from lra.config import Settings

INDEX_TS = Path(__file__).resolve().parents[1] / "cloudflare" / "src" / "index.ts"

# Settings that are deliberately not forwarded, each with the reason.
NOT_FORWARDED = {
    "MAIL_PROVIDER": "set to cloudflare by the container itself",
    "DATABASE_URL": "set to d1:// by the container itself",
    "EDGE_URL": "set to http://edge.internal by the container itself; the var is the public URL",
    "AGENTMAIL_API_KEY": "another mail provider, not used on Cloudflare",
    "AGENTMAIL_INBOX_ID": "another mail provider, not used on Cloudflare",
    "AGENTMAIL_WEBHOOK_SECRET": "another mail provider, not used on Cloudflare",
    "POSTMARK_SERVER_TOKEN": "another mail provider, not used on Cloudflare",
    "POSTMARK_INBOUND_SECRET": "another mail provider, not used on Cloudflare",
    "MANAGED_REVIEW_DETACHED_AGENT_ID": "read only by `lra agents apply`, run locally",
}


def _forwarded() -> set[str]:
    source = INDEX_TS.read_text()
    block = source[source.index("const FORWARDED = ["):]
    return set(re.findall(r'"([A-Z0-9_]+)"', block[: block.index("];")]))


def test_every_setting_is_forwarded_or_excused():
    settings = {name.upper() for name in Settings.model_fields}
    missing = settings - _forwarded() - NOT_FORWARDED.keys()
    assert not missing, f"set in wrangler.jsonc, never seen by the container: {sorted(missing)}"


def test_forwarded_names_are_settings():
    settings = {name.upper() for name in Settings.model_fields}
    assert not _forwarded() - settings


def test_excuses_are_not_stale():
    settings = {name.upper() for name in Settings.model_fields}
    assert NOT_FORWARDED.keys() <= settings
    assert not NOT_FORWARDED.keys() & _forwarded()
