"""The one client for the Cloudflare Worker that fronts this service.

On Cloudflare the application runs in a container, and a container cannot bind
to D1, R2 or the email service: only a Worker can. So the Worker in `cloudflare/`
exposes three internal endpoints -- database, blobs, send -- and everything in
this process that needs one of them comes through here.

The container holds exactly one credential for all of it, EDGE_SECRET, and no
Cloudflare API token at all. A token that can reach D1 over Cloudflare's public
API can reach every database on the account; this secret can reach three
endpoints of one Worker.

Those endpoints are not on the internet. In the container EDGE_URL is
`http://edge.internal`, a name the container runtime hands to the Worker's
outbound handler without it ever leaving Cloudflare (cloudflare/src/index.ts).
The Worker's public hostname answers 404 for /internal/*, unless an operator
has set OPERATOR_SECRET on it for a job. Then a laptop running `lra purge` or
`lra audit` against production sets the same OPERATOR_SECRET in its shell
(never in a file) and EDGE_URL to the Worker's public URL, and every request
is signed with a timestamp and a one-time nonce (cloudflare/src/operator.ts),
so a captured request cannot be replayed, edited or kept.

Retries are deliberately narrow. A request that never connected is safe to send
again. One that connected and then timed out may have been carried out, and
repeating an INSERT or a send is worse than reporting the failure, so those are
raised.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time

import httpx

from lra.config import settings

log = logging.getLogger(__name__)

_CONNECT_RETRIES = 3
_client: httpx.Client | None = None


class EdgeError(RuntimeError):
    """The Worker refused, or could not be reached."""


def _operator_secret() -> str:
    """Read from the process environment only, never from .env: it opens
    production from outside, and is set for one job and unset after."""
    return os.environ.get("OPERATOR_SECRET", "").strip()


def configured() -> bool:
    cfg = settings()
    return bool(cfg.edge_url.strip() and (cfg.edge_secret.strip() or _operator_secret()))


def operator_signature(secret: str, method: str, path_and_query: str, timestamp: str,
                       nonce: str, body: bytes) -> str:
    """HMAC-SHA256 over the request, exactly as operator.ts checks it."""
    message = "\n".join(["v1", method.upper(), path_and_query, timestamp, nonce,
                         hashlib.sha256(body).hexdigest()])
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def _operator_headers(secret: str, request: httpx.Request) -> dict[str, str]:
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(16)
    target = request.url.raw_path.decode("ascii")
    signature = operator_signature(secret, request.method, target, timestamp, nonce,
                                   request.content)
    return {"x-lra-time": timestamp, "x-lra-nonce": nonce, "x-lra-signature": signature}


def _http() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0))
    return _client


def call(path: str, *, json: dict | None = None, content: bytes | None = None,
         method: str = "POST") -> httpx.Response:
    cfg = settings()
    if not configured():
        raise EdgeError("EDGE_URL and EDGE_SECRET are not set")
    url = cfg.edge_url.rstrip("/") + path
    headers = {"authorization": f"Bearer {cfg.edge_secret}"} if cfg.edge_secret.strip() else {}
    operator = _operator_secret()

    for attempt in range(_CONNECT_RETRIES):
        try:
            request = _http().build_request(method, url, json=json, content=content,
                                            headers=headers)
            if operator:
                # A fresh nonce on every attempt: each one is a new request.
                request.headers.update(_operator_headers(operator, request))
            response = _http().send(request)
            break
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            if attempt == _CONNECT_RETRIES - 1:
                raise EdgeError(f"could not reach the edge worker: {e}") from e
            time.sleep(0.5 * (attempt + 1))
        except httpx.HTTPError as e:
            raise EdgeError(f"edge request failed: {e}") from e

    if response.status_code == 404 and method in ("GET", "DELETE"):
        return response
    if response.status_code >= 400:
        # The body is ours (the Worker's own JSON), never document content.
        raise EdgeError(f"edge worker returned {response.status_code} for {path}: "
                        f"{response.text[:300]}")
    return response
