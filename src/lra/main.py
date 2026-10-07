"""HTTP surface: one webhook in, health out. Everything else happens in handler."""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool

from lra import crypto, dms_mcp, oauth, policy, store
from lra.config import settings
from lra.handler import handle
from lra.mail import get_provider
from lra.models import InboundEmail

log = logging.getLogger(__name__)
logging.basicConfig(level=settings().log_level)
app = FastAPI(title="Legal Review Agent")

# How many reviews may be in flight before the webhook pushes back.
#
# A review is synchronous work run in Starlette's threadpool, and anyio's
# default limiter gives that pool 40 tokens on this install. /health and the
# consent callback are sync routes that need a token from the same pool. So
# accepting reviews without a bound has a specific failure: a slow model turns
# every queued job into a held thread, the pool saturates, and from the outside
# nothing looks wrong -- the webhook still answers 200 "accepted", the mail
# provider's dashboard shows 100% delivery, /health still says ok, and lawyers
# get nothing back. Refusing below the pool size leaves headroom for /health
# and consent, and turns an invisible backlog into a 503 the provider retries
# and an operator can see.
MAX_INFLIGHT_REVIEWS = 32

_inflight = 0
_inflight_lock = threading.Lock()


def _reserve() -> bool:
    """Take a review slot, or report that there is none."""
    global _inflight
    with _inflight_lock:
        if _inflight >= MAX_INFLIGHT_REVIEWS:
            return False
        _inflight += 1
        return True


def _refuse_while_paused() -> None:
    """SERVICE_PAUSED: 503, so whoever delivered this delivers it again
    later (a Workflow step, a mail vendor's webhook retry). Nothing
    is claimed, so the redelivery is new work once the switch is off."""
    if policy.paused():
        raise HTTPException(status_code=503, detail="paused", headers={"Retry-After": "600"})


def _release() -> None:
    global _inflight
    with _inflight_lock:
        _inflight -= 1


def _run_review(email: InboundEmail) -> None:
    """Run one review and give the slot back however it ends."""
    try:
        handle(email)
    finally:
        _release()


def _max_body_bytes() -> int:
    """Cap on an inbound webhook body.

    MAX_ATTACHMENT_MB limits the document; the body carries it base64-encoded,
    which is a third larger again, plus the rest of the message. Two megabytes
    of slack covers the JSON around it.
    """
    return (settings().max_attachment_mb * 4 // 3 + 2) * 1024 * 1024


def _refuse_to_store_unencrypted() -> None:
    """On a hosted database, documents are encrypted or the service does not run.

    A laptop with a local file and no key is fine. A server holding a firm's
    documents in somebody else's object store with no key is the configuration
    that must not start quietly, so it does not start at all.
    """
    if store.is_d1():
        crypto.require_key()


_refuse_to_store_unencrypted()


# ---------------------------------------------------------------------------
# The Workflow path (cloudflare/src/review-flow.ts, flow.py), the only way a
# message reaches this container on Cloudflare. One route per step. Each
# answers quickly with JSON the Worker can act on:
#   2xx  done, with the step's result
#   404  no state for this job (never prepared, or already finished)
#   422  retrying will not help; the Worker goes to the failure path
#   503  {"detail": "busy"}: no review slot; the Worker retries the step
#   500  anything else; the Worker retries the step
# ---------------------------------------------------------------------------


def _from_worker(request: Request) -> str:
    """The caller is our Worker, and names the job. Returns the job id."""
    provider = get_provider()
    if not hasattr(provider, "parse_raw"):
        raise HTTPException(status_code=404, detail="not enabled for this provider")
    headers = {k.lower(): v for k, v in request.headers.items()}
    if not provider.verify(headers, b""):
        raise HTTPException(status_code=401, detail="bad credentials")
    _refuse_while_paused()
    job_id = headers.get("x-job-id", "")
    if not job_id or len(job_id) > 100:
        raise HTTPException(status_code=400, detail="x-job-id is required")
    return job_id


async def _step(fn, *args, slot: bool = False):
    from lra import flow

    if slot and not _reserve():
        raise HTTPException(status_code=503, detail="busy", headers={"Retry-After": "60"})
    try:
        return await run_in_threadpool(fn, *args)
    except flow.Gone as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except flow.Permanent as e:
        log.error("workflow step %s failed for good: %s", fn.__name__, e)
        raise HTTPException(status_code=422, detail=str(e)) from e
    except crypto.KeyMissing as e:
        log.error("could not unseal a message for the workflow: %s", e)
        raise HTTPException(status_code=500, detail="cannot unseal message") from e
    finally:
        if slot:
            _release()


async def _json(request: Request) -> dict:
    try:
        body = json.loads(await _read_capped(request, 64 * 1024) or b"{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="body is not JSON") from None
    return body if isinstance(body, dict) else {}


@app.post("/prepare")
async def prepare_step(request: Request) -> dict:
    """The sealed raw message from R2; everything up to the review session."""
    from lra import flow

    job_id = _from_worker(request)
    raw = await _read_capped(request, _max_body_bytes())
    return await _step(flow.prepare, job_id, raw, slot=True)


@app.post("/session/start")
async def session_start_step(request: Request) -> dict:
    from lra import flow

    return await _step(flow.session_start, _from_worker(request))


@app.post("/session/status")
async def session_status_step(request: Request) -> dict:
    from lra import flow

    job_id = _from_worker(request)
    body = await _json(request)
    return await _step(flow.session_status, job_id, str(body.get("sessionId") or ""))


@app.post("/finish")
async def finish_step(request: Request) -> dict:
    from lra import flow

    job_id = _from_worker(request)
    body = await _json(request)
    notice_id = body.get("noticeMessageId")
    return await _step(flow.finish, job_id, str(notice_id) if notice_id else None, slot=True)


@app.post("/finished")
async def finished_step(request: Request) -> dict:
    from lra import flow

    job_id = _from_worker(request)
    body = await _json(request)
    message_id = body.get("messageId")
    return await _step(flow.finished, job_id, str(body.get("kind") or "review"),
                       str(message_id) if message_id else None)


@app.post("/fail")
async def fail_step(request: Request) -> dict:
    from lra import flow

    job_id = _from_worker(request)
    body = await _json(request)
    notice_id = body.get("noticeMessageId")
    return await _step(flow.fail, job_id, str(body.get("error") or ""),
                       str(notice_id) if notice_id else None)


@app.post("/retention/sweep")
async def retention_sweep(request: Request) -> dict:
    """The daily retention sweep (retention.py), called by the Worker's cron.
    Only our Worker may call it. It runs under the kill switch too: the
    switch stops reviewing and sending, and deleting what is past its
    retention is neither."""
    from lra import retention

    provider = get_provider()
    headers = {k.lower(): v for k, v in request.headers.items()}
    if not hasattr(provider, "parse_raw") or not provider.verify(headers, b""):
        raise HTTPException(status_code=401, detail="bad credentials")
    return {"deleted": await run_in_threadpool(retention.sweep)}


@app.get("/health")
def health() -> dict[str, object]:
    """Liveness, plus the one number that goes wrong first.

    Queue depth is here because the failure this service actually has is a
    backlog nobody can see: "ok" on its own stayed true while every review
    silently queued behind a saturated threadpool.
    """
    return {
        "status": "ok",
        "provider": settings().mail_provider,
        "reviews_in_flight": _inflight,
        "review_capacity": MAX_INFLIGHT_REVIEWS,
    }


async def _read_capped(request: Request, limit: int) -> bytes:
    """Read the body, but never more than `limit` of it.

    The body has to be buffered whole because the signature is computed over
    it, so without a cap an unauthenticated POST decides how much memory this
    process spends. The Content-Length check is the polite half: it refuses an
    honest sender before a byte arrives. The streaming check is the half that
    holds, because a chunked request declares no length at all and would
    otherwise be buffered in full before anyone looked at its size.
    """
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail="message too large")

    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > limit:
            raise HTTPException(status_code=413, detail="message too large")
        chunks.append(chunk)
    return b"".join(chunks)


@app.post("/webhooks/inbound")
async def inbound(request: Request, background: BackgroundTasks) -> dict[str, str]:
    raw = await _read_capped(request, _max_body_bytes())

    provider = get_provider()
    headers = {k.lower(): v for k, v in request.headers.items()}

    try:
        verified = provider.verify(headers, raw)
    except Exception:
        # A signature header we cannot even compare is a failed signature, not
        # a 500: hmac.compare_digest raises TypeError on a non-ASCII str, so a
        # header with one accent in it used to return a stack trace where an
        # ordinary wrong secret returns 401. Fail closed, identically.
        log.exception("signature verification raised; treating the request as unsigned")
        verified = False
    if not verified:
        raise HTTPException(status_code=401, detail="bad signature")
    _refuse_while_paused()

    try:
        payload = json.loads(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="body is not JSON") from None

    if not _reserve():
        log.warning(
            "refusing an inbound webhook: %d reviews already in flight", _inflight
        )
        # 503 rather than a 200 we cannot honour. The provider retries, and
        # store.claim() makes a retry that lands after the backlog drains
        # idempotent, so backpressure costs a delay rather than a review.
        raise HTTPException(
            status_code=503, detail="busy", headers={"Retry-After": "120"}
        )

    try:
        # Parsing is not free and it is not async: the AgentMail adapter
        # fetches each attachment over HTTP with a blocking client. Left on the
        # event loop, one 20 MB document stalls every other connection on the
        # worker -- including /health and including the signature check on the
        # next message -- for as long as the provider takes to hand it over.
        email = await run_in_threadpool(provider.parse_inbound, payload)
    except Exception:
        _release()
        # A payload we cannot parse will not parse on the retry either, so a
        # 5xx would only buy a retry storm. The log line is the signal: it
        # means the provider's payload and this adapter disagree.
        log.exception("could not parse an inbound webhook payload")
        raise HTTPException(status_code=400, detail="unparseable payload") from None

    # Return 200 immediately; mail providers time out webhooks well before a
    # model call finishes, and a retry storm means duplicate reviews.
    background.add_task(_run_review, email)
    return {"status": "accepted"}


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{heading}</title></head>
<body style="font-family:system-ui,sans-serif;max-width:32rem;margin:6rem auto;\
padding:0 1.5rem;line-height:1.55;color:#111">
<h2 style="font-weight:600;margin:0 0 1rem">{heading}</h2>
{body}
</body></html>"""


def _page(heading: str, body: str, status: int = 200) -> HTMLResponse:
    """Render the only screen this product has.

    Every path through the callback comes through here, including the ones that
    used to surface as a JSON {"detail": ...} blob or a bare 500. A lawyer who
    clicks Deny, or clicks the link twice, is looking at the firm's new legal
    tool for the first and only time, and it should read like a sentence
    somebody wrote rather than like developer output.
    """
    return HTMLResponse(PAGE.format(heading=heading, body=body), status_code=status)


def _expires_at(body: dict) -> datetime | None:
    """When the access token dies, if the server says.

    `expires_in` is seconds, but servers send it as a string often enough that
    taking it on trust would throw after the grant already succeeded. No value
    means an opaque token whose death we can only discover by using it, which
    Token.expired already treats as "not expired until proven otherwise".
    """
    try:
        seconds = int(body.get("expires_in"))
    except (TypeError, ValueError):
        return None
    return datetime.now(UTC) + timedelta(seconds=seconds) if seconds > 0 else None


@app.get("/oauth/callback", response_class=HTMLResponse)
def oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> HTMLResponse:
    """Where the lawyer lands after granting access.

    Single-use state, exchanged immediately for a token stored against that one
    lawyer. The page they see is deliberately plain: the product lives in
    email, and this is the only screen it has.

    Every parameter is optional because every one of them is absent in some
    real case. A lawyer who clicks Deny arrives with `error` and no `code`, and
    FastAPI answered that with a 422 and a list of validation errors.

    Deliberately sync: it makes one blocking token exchange, and Starlette runs
    a sync route in the threadpool instead of on the event loop.
    """
    if error:
        # Logged, never echoed: error_description is attacker-controllable text
        # arriving in a query string, and this page is HTML.
        log.info("consent not granted: %s (%s)", error, error_description)
        return _page(
            "No problem.",
            "<p>Nothing has been connected. I will keep reviewing documents on their "
            "own, which works fine -- I just will not be able to check them against "
            "the firm's precedents or the rest of the matter.</p>"
            '<p style="color:#666">Reply <b>connect</b> to any of my emails if you '
            "change your mind.</p>",
        )

    if not code or not state:
        return _page(
            "That link is incomplete.",
            "<p>It looks like part of the address was lost on the way here, which "
            "mail clients sometimes do to a long link.</p>"
            '<p style="color:#666">Reply <b>connect</b> to any of my emails and I '
            "will send a fresh one.</p>",
            status=400,
        )

    claimed = oauth.claim_state(state)
    if not claimed:
        return _page(
            "That link has already been used, or it has expired.",
            "<p>A consent link is good for one click and lasts a day, so this one has "
            "either done its job already or sat too long.</p>"
            '<p style="color:#666">Reply <b>connect</b> to any of my emails and I '
            "will send a fresh one.</p>",
            status=400,
        )

    cfg = settings()
    try:
        r = httpx.post(
            cfg.dms_token_url,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": cfg.dms_client_id,
                "client_secret": cfg.dms_client_secret,
                # Identical to the one in the authorization request, because
                # the server compares them.
                "redirect_uri": oauth.callback_url(cfg.public_base_url),
            },
            timeout=30,
        )
        r.raise_for_status()
        body = r.json()
        access_token = body["access_token"]
    except Exception:
        log.exception("token exchange failed for %s", claimed.user_address)
        # Put the state back. The exchange failing is our problem or the DMS's,
        # and burning the lawyer's only link over it left them with no way
        # forward except knowing to reply "connect" unprompted.
        oauth.restore_pending(claimed)
        return _page(
            "I could not finish connecting.",
            "<p>The document system did not complete the handover. Nothing has "
            "changed, and your link still works -- try it again in a minute.</p>"
            '<p style="color:#666">Reply <b>connect</b> to any of my emails if it '
            "keeps failing, and I will send a new link.</p>",
            status=502,
        )

    if dms_mcp.vault_held():
        # The query binding: the grant becomes the lawyer's vault credential,
        # which Anthropic refreshes against the token endpoint; nothing is
        # written to our token table (docs/migration.md, phase 4).
        try:
            dms_mcp.store_grant(claimed.user_address, body)
        except Exception:
            log.exception("could not store the grant for %s in their vault",
                          claimed.user_address)
            # The code is spent, so the link cannot be retried.
            return _page(
                "I could not finish connecting.",
                "<p>The document system agreed, but I could not store the connection. "
                "Nothing has been connected.</p>"
                '<p style="color:#666">Reply <b>connect</b> to any of my emails and I '
                "will send a new link.</p>",
                status=502,
            )
    if dms_mcp.token_table_in_use():
        oauth.save(
            oauth.Token(
                user_address=claimed.user_address,
                system=claimed.system,
                access_token=access_token,
                refresh_token=body.get("refresh_token"),
                # What the server granted, not what we asked for. This record
                # is what an audit reads to answer "what can the agent see as
                # me".
                scopes=oauth.granted_scopes(body, claimed.system),
                expires_at=_expires_at(body),
            )
        )
    return _page(
        "Connected.",
        "<p>You can close this tab. Send me a document whenever you are ready, and I "
        "will check it against the firm's precedents.</p>"
        '<p style="color:#666">Reply <b>revoke</b> to any of my emails to '
        "disconnect.</p>",
    )
