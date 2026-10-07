"""Per-user delegated OAuth.

The agent never holds a firm-wide service account for the document management
system. It holds a token per lawyer, granted by that lawyer, and every DMS call
is made as them.

This is not a convenience. It is the ethical wall, enforced structurally: if a
lawyer is not on a matter, their token cannot read it, so the agent cannot read
it either, so the agent cannot leak it into a review. No application-level
access-control code can be as trustworthy as not having the credential.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlencode

import httpx

from lra import crypto
from lra.store import connect

TOKEN_SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_tokens (
    user_address TEXT NOT NULL,
    system TEXT NOT NULL,          -- dms | calendar | mail
    access_token TEXT NOT NULL,    -- encrypted at rest in production
    refresh_token TEXT,
    scopes TEXT,
    expires_at TEXT,
    granted_at TEXT,
    PRIMARY KEY (user_address, system)
);
CREATE TABLE IF NOT EXISTS oauth_pending (
    state TEXT PRIMARY KEY,
    user_address TEXT NOT NULL,
    system TEXT NOT NULL,
    created_at TEXT
);
"""

# Read-only by default. The agent reads the firm's documents; it does not file
# anything back without a separate, explicitly granted write scope.
DEFAULT_SCOPES = {
    "imanage": ["user", "documents:read", "workspaces:read", "matters:read"],
    "netdocuments": ["read"],
    "sharepoint": ["Files.Read.All", "Sites.Read.All", "offline_access"],
}

# How long an emailed consent link stays good.
#
# The usual advice for an OAuth state is ten minutes, which assumes the flow
# starts in the same browser that finishes it. Ours starts in a mailbox: the
# link arrives as a reply to a document the lawyer just sent, and they may well
# open it after the meeting they sent it from. A day survives that. It also
# closes the case this is really for -- a consent email still sitting in an
# archived mailbox, an mbox export or a mail-security appliance months later,
# where anyone who can read that mailbox could complete the flow against their
# own DMS account and have the agent search their document store under the
# partner's address. Expiry is recoverable by design: the callback tells them
# to reply "connect" for a fresh link.
PENDING_TTL = timedelta(hours=24)

# The callback path, in one place. RFC 6749 4.1.3 has the authorization server
# compare the redirect_uri in the token request against the one in the
# authorization request character for character, so these cannot be built
# twice.
CALLBACK_PATH = "/oauth/callback"


def callback_url(public_base_url: str) -> str:
    """Where the lawyer's browser lands, built the one way it is ever built."""
    return f"{public_base_url.rstrip('/')}{CALLBACK_PATH}"


@dataclass
class Token:
    user_address: str
    system: str
    access_token: str
    refresh_token: str | None
    scopes: list[str]
    expires_at: datetime | None

    @property
    def expired(self) -> bool:
        if self.expires_at is None:
            return False
        return datetime.now(UTC) >= self.expires_at - timedelta(seconds=60)


@dataclass
class Pending:
    """A consent link that has been emailed but not yet completed."""

    state: str
    user_address: str
    system: str
    created_at: datetime | None


def init() -> None:
    with connect() as c:
        c.executescript(TOKEN_SCHEMA)


def get(user_address: str, system: str) -> Token | None:
    init()
    with connect() as c:
        row = c.execute(
            "SELECT access_token, refresh_token, scopes, expires_at FROM oauth_tokens "
            "WHERE user_address = ? AND system = ?",
            (user_address.lower(), system),
        ).fetchone()
    if not row:
        return None
    return Token(
        user_address=user_address.lower(),
        system=system,
        access_token=crypto.unseal_text(row[0]),
        refresh_token=crypto.unseal_text(row[1]) if row[1] else row[1],
        scopes=json.loads(row[2] or "[]"),
        expires_at=datetime.fromisoformat(row[3]) if row[3] else None,
    )


def save(token: Token) -> None:
    init()
    now = datetime.now(UTC).isoformat()
    with connect() as c:
        c.execute(
            "INSERT INTO oauth_tokens (user_address, system, access_token, refresh_token, "
            "scopes, expires_at, granted_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_address, system) DO UPDATE SET "
            "access_token=excluded.access_token, refresh_token=excluded.refresh_token, "
            "scopes=excluded.scopes, expires_at=excluded.expires_at",
            (
                token.user_address.lower(),
                token.system,
                # Sealed when DATA_KEY is set. These are a lawyer's standing
                # credentials to the firm's document system, and they used to
                # sit in plain text beside everything else in the database.
                crypto.seal_text(token.access_token),
                crypto.seal_text(token.refresh_token) if token.refresh_token else None,
                json.dumps(token.scopes),
                token.expires_at.isoformat() if token.expires_at else None,
                now,
            ),
        )


def revoke(user_address: str, system: str | None = None) -> None:
    """A lawyer can cut the agent off entirely by replying 'revoke' to any email."""
    init()
    with connect() as c:
        if system:
            c.execute(
                "DELETE FROM oauth_tokens WHERE user_address = ? AND system = ?",
                (user_address.lower(), system),
            )
        else:
            c.execute("DELETE FROM oauth_tokens WHERE user_address = ?", (user_address.lower(),))


def consent_url(
    user_address: str,
    system: str,
    authorize_url: str,
    client_id: str,
    redirect_uri: str,
) -> str:
    """A one-time link we email the lawyer the first time we need their DMS.

    The consent step is the only moment this product asks for a browser. It
    happens once, it is initiated by an email the lawyer already expected, and
    it is what makes every later review able to see their matters.

    `authorize_url` is used verbatim. DMS_AUTHORIZE_URL is named and documented
    as the authorize endpoint itself, and this used to append `/authorize` to
    it a second time, which on a real iManage instance is a 404 on the first
    link we ever send anyone.
    """
    init()
    state = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    with connect() as c:
        # Sweep before inserting. Nothing else ever deletes a pending row, so
        # every link a lawyer ignored used to sit in this table forever.
        c.execute(
            "DELETE FROM oauth_pending WHERE created_at IS NULL OR created_at < ?",
            ((now - PENDING_TTL).isoformat(),),
        )
        c.execute(
            "INSERT INTO oauth_pending (state, user_address, system, created_at) "
            "VALUES (?, ?, ?, ?)",
            (state, user_address.lower(), system, now.isoformat()),
        )
    params = {
        "response_type": "code",
        "client_id": client_id,
        # Required here because the token exchange sends one. A server that
        # compares the two (RFC 6749 4.1.3) rejects the grant as invalid_grant
        # when the authorize step omitted it, which is how this failed before.
        "redirect_uri": redirect_uri,
        "scope": " ".join(DEFAULT_SCOPES.get(system, [])),
        "state": state,
    }
    # quote_via=quote so the spaces between scopes arrive as %20 rather than
    # "+", which a few authorization servers take literally.
    query = urlencode(params, quote_via=quote)
    separator = "&" if "?" in authorize_url else "?"
    return f"{authorize_url}{separator}{query}"


def claim_state(state: str) -> Pending | None:
    """Exchange a callback state for the user it belongs to.

    Single use, and only within PENDING_TTL. The delete is the claim: doing it
    with RETURNING means two callbacks racing on the same state cannot both
    win, and a state that has expired is removed on the way past rather than
    left to accumulate.
    """
    init()
    with connect() as c:
        row = c.execute(
            "DELETE FROM oauth_pending WHERE state = ? "
            "RETURNING user_address, system, created_at",
            (state,),
        ).fetchone()
    if not row:
        return None
    created_at = _parse_timestamp(row[2])
    # A row with no timestamp predates this column being written. There is no
    # way to tell whether it is a minute or a year old, and the whole point of
    # the TTL is the year-old case, so it does not get the benefit of the doubt.
    if created_at is None or datetime.now(UTC) - created_at > PENDING_TTL:
        return None
    return Pending(state=state, user_address=row[0], system=row[1], created_at=created_at)


def restore_pending(pending: Pending) -> None:
    """Put a claimed state back after a token exchange we could not complete.

    The claim is a delete, so a DMS that 500s halfway through consent used to
    burn the lawyer's only link: clicking it again said "expired", and the sole
    route back was knowing to reply "connect". The original created_at goes
    back with it, so retrying cannot walk the TTL forward indefinitely.
    """
    init()
    with connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO oauth_pending (state, user_address, system, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                pending.state,
                pending.user_address,
                pending.system,
                pending.created_at.isoformat() if pending.created_at else None,
            ),
        )


def granted_scopes(body: dict, system: str) -> list[str]:
    """What the authorization server actually granted, not what we asked for.

    The stored scope list is the audit story: "the agent reads the document
    system as you, read-only". Recording DEFAULT_SCOPES here, as this used to,
    makes that a record of our own intent rather than of the grant, which is
    exactly the wrong thing to show someone asking what the agent can do.

    RFC 6749 5.1 makes `scope` optional in the response when the grant matches
    the request exactly, so an absent value does mean "what we asked for".
    """
    granted = body.get("scope")
    if isinstance(granted, str) and granted.split():
        return granted.split()
    if isinstance(granted, list) and granted:
        return [str(s) for s in granted]
    return list(DEFAULT_SCOPES.get(system, []))


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    # Rows written before timestamps carried an offset read back naive.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def refresh(token: Token, token_url: str, client_id: str, client_secret: str) -> Token:
    if not token.refresh_token:
        raise PermissionError("no refresh token; the lawyer needs to re-consent")
    r = httpx.post(
        token_url,
        data={
            "grant_type": "refresh_token",
            "refresh_token": token.refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=30,
    )
    r.raise_for_status()
    body = r.json()
    token.access_token = body["access_token"]
    token.refresh_token = body.get("refresh_token", token.refresh_token)
    if body.get("expires_in"):
        token.expires_at = datetime.now(UTC) + timedelta(seconds=body["expires_in"])
    save(token)
    return token
