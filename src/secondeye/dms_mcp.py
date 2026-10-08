"""The document system through our MCP server and Anthropic vaults.

docs/migration.md, phases 4 and 5: the only way an agent reaches the
document system. The review reaches iManage through cloudflare/dms-mcp,
so the session needs no client for it; and each lawyer's iManage tokens
are an `mcp_oauth` credential in that lawyer's own Anthropic vault, which
Anthropic refreshes against the iManage token endpoint, instead of rows in
our `oauth_tokens` table that we refresh and seal. James approved Anthropic
holding those tokens (2026-09-28).

The ethical wall is unchanged in kind. iManage still answers every call
under the lawyer's own token, so nothing they cannot open can be read. And
the review is still narrower than the lawyer: a session may only touch the
matter its email belongs to. That used to be a closure over `matter_id` in
tools/__init__.py; now it is a signature. Each session's MCP URL carries
`?u=<lawyer>&m=<matter>&exp=<unix>&sig=<HMAC>`, set per session with an
`agent_with_overrides` on `sessions.create`, and the server refuses a tool
call that does not verify or reaches outside that matter. The model never
names the matter.

Two things only a live run settles, and what happens if each goes wrong:

  - Vault credentials are matched to MCP servers by URL, normalised for
    scheme, host, port and a trailing slash. The docs do not say whether a
    query string is ignored, compared, or dropped on the way through. If
    ignored, the design above works. If not, DMS_MCP_BINDING=capability
    switches without a code change: the session's URL is the bare endpoint,
    and a vault made for that one session holds a `static_bearer` credential
    whose token is a signed capability carrying lawyer, matter, expiry and
    the lawyer's iManage access token. That token has to come from somewhere
    we hold, so capability mode keeps the `oauth_tokens` table in use, and
    oauth.py stays until the query form is proven.
  - iManage's refresh under `mcp_oauth`. If Anthropic's refresh fails, the
    `vault_credential.refresh_failed` webhook reaches the Worker, which
    emails the lawyer "Reply connect to reconnect your document system."

API shapes here are from the Python SDK's own types (anthropic 1.8:
`beta.vaults.create(display_name, metadata)`,
`beta.vaults.credentials.create(vault_id, auth, display_name, metadata)`,
`.update(credential_id, vault_id=, auth=)`, `.archive(credential_id,
vault_id=)`, `sessions.create(agent={"type": "agent_with_overrides", ...},
vault_ids=[...])`), none yet exercised against the live API.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from secondeye.config import settings
from secondeye.store import connect

log = logging.getLogger(__name__)

# The name the agents' mcp_toolset refers to. The URL changes per session;
# the name does not, so the toolset needs no override.
SERVER_NAME = "dms"

CAPABILITY_PREFIX = "cap1."

SCHEMA = """
CREATE TABLE IF NOT EXISTS dms_vaults (
    user_address TEXT PRIMARY KEY,
    vault_id TEXT NOT NULL,
    credential_id TEXT,
    mcp_server_url TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS dms_session_vaults (
    vault_id TEXT PRIMARY KEY,
    session_id TEXT,
    expires_at TEXT
);
"""


def init() -> None:
    with connect() as c:
        c.executescript(SCHEMA)


# --------------------------------------------------------------------------
# Which path is in use
# --------------------------------------------------------------------------


def enabled() -> bool:
    """Whether a document system is configured (DMS_PROVIDER)."""
    return settings().dms_provider.lower() != "none"


def vault_held() -> bool:
    """The lawyer's tokens are Anthropic's to hold and refresh; ours are not read."""
    return enabled() and settings().dms_mcp_binding == "query"


def token_table_in_use() -> bool:
    """Whether oauth.py's table is where a lawyer's token lives: only for the
    capability fallback, which embeds the token in each session's vault."""
    return enabled() and settings().dms_mcp_binding == "capability"


# --------------------------------------------------------------------------
# The agent definition: the server, and a toolset that never waits
# --------------------------------------------------------------------------


def server_definition(url: str | None = None) -> dict:
    return {"type": "url", "name": SERVER_NAME, "url": url or settings().dms_mcp_url.strip()}


def toolset() -> dict:
    """always_allow, because the default for an MCP toolset is always_ask,
    which idles the session on `requires_action` until someone confirms: in
    a session nobody is attached to, that is for ever. The server enforces
    the matter; there is nothing for a human to approve per call."""
    return {"type": "mcp_toolset", "mcp_server_name": SERVER_NAME,
            "default_config": {"enabled": True,
                               "permission_policy": {"type": "always_allow"}}}


# --------------------------------------------------------------------------
# The matter binding (cloudflare/dms-mcp/src/binding.ts checks it)
# --------------------------------------------------------------------------


def _key() -> bytes:
    secret = settings().dms_mcp_signing_key
    if not secret:
        raise RuntimeError("DMS_MCP_SIGNING_KEY is not set; no session can be bound to a matter")
    return secret.encode("utf-8")


def sign(user_address: str, matter_id: str, expires: int) -> str:
    message = f"{user_address.lower()}\n{matter_id}\n{int(expires)}".encode()
    return hmac.new(_key(), message, hashlib.sha256).hexdigest()


def bound_url(user_address: str, matter_id: str, expires: int) -> str:
    base = settings().dms_mcp_url.strip()
    query = urlencode({"u": user_address.lower(), "m": matter_id, "exp": str(int(expires)),
                       "sig": sign(user_address, matter_id, expires)})
    return f"{base}{'&' if '?' in base else '?'}{query}"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def capability(user_address: str, matter_id: str, access_token: str, expires: int) -> str:
    body = _b64url(json.dumps({"u": user_address.lower(), "m": matter_id, "exp": int(expires),
                               "t": access_token}, separators=(",", ":")).encode())
    mac = hmac.new(_key(), (CAPABILITY_PREFIX + body).encode(), hashlib.sha256).digest()
    return f"{CAPABILITY_PREFIX}{body}.{_b64url(mac)}"


# --------------------------------------------------------------------------
# Each lawyer's vault
# --------------------------------------------------------------------------


@dataclass
class VaultRecord:
    user_address: str
    vault_id: str
    credential_id: str | None
    mcp_server_url: str | None


def _row(row) -> VaultRecord | None:
    return VaultRecord(*row) if row else None


def record(user_address: str) -> VaultRecord | None:
    init()
    with connect() as c:
        return _row(c.execute(
            "SELECT user_address, vault_id, credential_id, mcp_server_url FROM dms_vaults "
            "WHERE user_address = ?", (user_address.lower(),)).fetchone())


def record_for_vault(vault_id: str) -> VaultRecord | None:
    init()
    with connect() as c:
        return _row(c.execute(
            "SELECT user_address, vault_id, credential_id, mcp_server_url FROM dms_vaults "
            "WHERE vault_id = ?", (vault_id,)).fetchone())


def connected(user_address: str) -> bool:
    found = record(user_address)
    return bool(found and found.credential_id)


def _save(user_address: str, vault_id: str, credential_id: str | None, url: str | None) -> None:
    init()
    with connect() as c:
        c.execute(
            "INSERT INTO dms_vaults (user_address, vault_id, credential_id, mcp_server_url, "
            "updated_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(user_address) DO UPDATE SET "
            "vault_id=excluded.vault_id, credential_id=excluded.credential_id, "
            "mcp_server_url=excluded.mcp_server_url, updated_at=excluded.updated_at",
            (user_address.lower(), vault_id, credential_id, url, datetime.now(UTC).isoformat()),
        )


def _client(client=None):
    if client is not None:
        return client
    from secondeye.config import anthropic_client

    return anthropic_client()


def _not_found(error: Exception) -> bool:
    return getattr(error, "status_code", None) == 404


def store_grant(user_address: str, body: dict, client=None) -> VaultRecord:
    """The consent callback's token response, as the lawyer's vault credential.

    One vault per lawyer, made on their first grant and recorded by id. A
    second grant (they replied "connect" again) updates the credential in
    place; a credential that has gone, or one keyed on a different server
    URL (credential keys are immutable), is replaced.
    """
    cfg = settings()
    client = _client(client)
    user = user_address.lower()
    url = cfg.dms_mcp_url.strip()
    if not url:
        raise RuntimeError("DMS_MCP_URL is not set")
    expires_at = None
    if body.get("expires_in"):
        expires_at = (datetime.now(UTC) + timedelta(seconds=int(body["expires_in"]))).isoformat()

    existing = record(user)
    if existing is None:
        vault = client.beta.vaults.create(display_name=f"Document system: {user}"[:255],
                                          metadata={"lra": "dms", "lra_user": user[:512]})
        _save(user, vault.id, None, None)
        existing = VaultRecord(user, vault.id, None, None)

    if existing.credential_id and existing.mcp_server_url == url:
        auth: dict = {"type": "mcp_oauth", "access_token": body["access_token"]}
        if expires_at:
            auth["expires_at"] = expires_at
        if body.get("refresh_token"):
            auth["refresh"] = {"refresh_token": body["refresh_token"]}
        try:
            client.beta.vaults.credentials.update(existing.credential_id,
                                                  vault_id=existing.vault_id, auth=auth)
            _save(user, existing.vault_id, existing.credential_id, url)
            return VaultRecord(user, existing.vault_id, existing.credential_id, url)
        except Exception as e:
            if not _not_found(e):
                raise
            log.info("credential %s has gone; making a new one", existing.credential_id)
    elif existing.credential_id:
        _archive(client, existing.vault_id, existing.credential_id)

    auth = {"type": "mcp_oauth", "mcp_server_url": url, "access_token": body["access_token"]}
    if expires_at:
        auth["expires_at"] = expires_at
    if body.get("refresh_token"):
        # Anthropic posts the refresh_token grant here before the access
        # token expires. Omitted, the connection lasts until it does.
        auth["refresh"] = {
            "refresh_token": body["refresh_token"],
            "client_id": cfg.dms_client_id,
            "token_endpoint": cfg.dms_token_url,
            # The same client authentication the code exchange uses (main.py
            # posts client_secret in the body).
            "token_endpoint_auth": (
                {"type": "client_secret_post", "client_secret": cfg.dms_client_secret}
                if cfg.dms_client_secret else {"type": "none"}),
        }
    credential = client.beta.vaults.credentials.create(
        existing.vault_id, auth=auth, display_name=f"{cfg.dms_provider} as {user}"[:255],
        metadata={"lra_user": user[:512]})
    _save(user, existing.vault_id, credential.id, url)
    return VaultRecord(user, existing.vault_id, credential.id, url)


def _archive(client, vault_id: str, credential_id: str) -> None:
    try:
        client.beta.vaults.credentials.archive(credential_id, vault_id=vault_id)
    except Exception as e:
        if not _not_found(e):
            raise


def revoke(user_address: str, client=None) -> bool:
    """"revoke": archive the lawyer's credential, which purges the secret at
    Anthropic, and forget it. Raises if the archive fails, so nobody is told
    they are disconnected while the credential still works."""
    found = record(user_address)
    if found is None:
        return False
    if found.credential_id:
        _archive(_client(client), found.vault_id, found.credential_id)
    init()
    with connect() as c:
        c.execute("UPDATE dms_vaults SET credential_id = NULL, mcp_server_url = NULL, "
                  "updated_at = ? WHERE user_address = ?",
                  (datetime.now(UTC).isoformat(), found.user_address))
    return True


# --------------------------------------------------------------------------
# One session's access
# --------------------------------------------------------------------------


@dataclass
class SessionAccess:
    """What `sessions.create` needs for the document system: an agent
    override setting the matter-bound URL, the vaults to attach, and the
    ones made for this session alone, deleted when it ends."""

    overrides: dict | None = None
    vault_ids: list[str] = field(default_factory=list)
    ephemeral: list[str] = field(default_factory=list)


def session_access(user_address: str, matter_id: str | None, client=None,
                   now: float | None = None) -> SessionAccess:
    """The document-system part of one session.

    With no matter, nothing: the agent's own URL is the bare endpoint, and
    the server answers every tool call with "no matter is attached". With a
    matter, the URL is bound to it whether or not the lawyer has connected,
    so an unconnected lawyer's review is told so by the server in a sentence.
    """
    if not enabled() or not matter_id or not user_address:
        return SessionAccess()
    cfg = settings()
    expires = int((now if now is not None else time.time()) + cfg.dms_mcp_binding_ttl_seconds)
    if cfg.dms_mcp_binding == "query":
        found = record(user_address)
        return SessionAccess(
            overrides={"mcp_servers": [server_definition(bound_url(user_address, matter_id,
                                                                  expires))]},
            vault_ids=[found.vault_id] if found and found.credential_id else [])

    # The capability fallback: a vault for this session alone, holding a
    # signed bearer for the bare URL.
    token = _held_token(user_address)
    if not token:
        return SessionAccess()
    client = _client(client)
    sweep(client)
    vault = client.beta.vaults.create(
        display_name="Review session: document system",
        metadata={"lra": "dms-session", "lra_user": user_address.lower()[:512]})
    try:
        client.beta.vaults.credentials.create(vault.id, auth={
            "type": "static_bearer", "mcp_server_url": cfg.dms_mcp_url.strip(),
            "token": capability(user_address, matter_id, token, expires)})
    except Exception:
        _delete_vault(client, vault.id)
        raise
    init()
    with connect() as c:
        c.execute("INSERT INTO dms_session_vaults (vault_id, session_id, expires_at) "
                  "VALUES (?, NULL, ?)",
                  (vault.id, datetime.fromtimestamp(expires, UTC).isoformat()))
    return SessionAccess(vault_ids=[vault.id], ephemeral=[vault.id])


def for_session(user_address: str, matter_id: str | None, client=None) -> SessionAccess | None:
    """`session_access`, or None when the document system is not on MCP.

    A failure here (no signing key, the vault API down) costs the review its
    document-system lookups, not the review: it is logged and the session
    starts unbound, where every tool call is answered "no matter is
    attached", which the model passes on."""
    if not enabled():
        return None
    try:
        return session_access(user_address, matter_id, client=client)
    except Exception:
        log.exception("could not bind the document system to this session; starting without it")
        return SessionAccess()


def _held_token(user_address: str) -> str | None:
    """The lawyer's current iManage access token from our own table, for the
    capability fallback only; refreshed first if it is about to expire."""
    from secondeye import oauth

    cfg = settings()
    token = oauth.get(user_address, cfg.dms_provider.lower())
    if token is None:
        return None
    if token.expired:
        try:
            token = oauth.refresh(token, cfg.dms_token_url, cfg.dms_client_id,
                                  cfg.dms_client_secret)
        except Exception:  # noqa: BLE001 - no token means no lookups, said by the server
            log.warning("could not refresh the document-system token for %s", user_address)
            return None
    return token.access_token


def attach_session(access: SessionAccess, session_id: str) -> None:
    """Note which session a per-session vault belongs to, so whoever finishes
    it (maybe another process) can delete the vault."""
    if not access.ephemeral:
        return
    init()
    with connect() as c:
        for vault_id in access.ephemeral:
            c.execute("UPDATE dms_session_vaults SET session_id = ? WHERE vault_id = ?",
                      (session_id, vault_id))


def release(client, *, session_id: str | None = None, vault_ids: list[str] | None = None) -> None:
    """Delete the vaults made for one session. Best-effort: an expired
    capability is refused by the server anyway, and `sweep` retries."""
    ids = list(vault_ids or [])
    if session_id:
        init()
        with connect() as c:
            ids += [r[0] for r in c.execute(
                "SELECT vault_id FROM dms_session_vaults WHERE session_id = ?",
                (session_id,)).fetchall()]
    for vault_id in dict.fromkeys(ids):
        if _delete_vault(client, vault_id):
            with connect() as c:
                c.execute("DELETE FROM dms_session_vaults WHERE vault_id = ?", (vault_id,))


def sweep(client, now: datetime | None = None) -> int:
    """Per-session vaults whose capability has expired, left by a session
    that was never finished here."""
    init()
    cutoff = (now or datetime.now(UTC)).isoformat()
    with connect() as c:
        ids = [r[0] for r in c.execute(
            "SELECT vault_id FROM dms_session_vaults WHERE expires_at < ?", (cutoff,)).fetchall()]
    if ids:
        release(client, vault_ids=ids)
    return len(ids)


def _delete_vault(client, vault_id: str) -> bool:
    try:
        client.beta.vaults.delete(vault_id)
        return True
    except Exception as e:  # noqa: BLE001 - best-effort; sweep retries
        if _not_found(e):
            return True
        log.warning("could not delete session vault %s", vault_id)
        return False


def any_dms_session_state() -> bool:
    """Whether a session could need `release` at all: saves a query on the
    common path, where the fallback has never been used."""
    return enabled() and settings().dms_mcp_binding == "capability"
