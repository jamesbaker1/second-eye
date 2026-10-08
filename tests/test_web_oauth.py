"""The HTTP surface and the consent loop.

Two things are being protected here. The consent page is the only screen this
product ever shows a lawyer, so every path through it has to render as a page
somebody wrote rather than as a JSON blob or a stack trace. And the consent
link is the one credential-shaped thing we put in a mailbox, so it has to
expire and it has to work exactly once.
"""

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from secondeye import main, oauth
from secondeye.config import settings
from secondeye.models import InboundEmail
from secondeye.store import connect

AUTHORIZE = "https://dms.example.com/auth/oauth2/authorize"
TOKEN = "https://dms.example.com/auth/oauth2/token"
CALLBACK = "https://review.firm.com/oauth/callback"


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/oauth.sqlite3")
    monkeypatch.setenv("DMS_PROVIDER", "imanage")
    monkeypatch.setenv("DMS_AUTHORIZE_URL", AUTHORIZE)
    monkeypatch.setenv("DMS_TOKEN_URL", TOKEN)
    monkeypatch.setenv("DMS_CLIENT_ID", "second-eye-client")
    monkeypatch.setenv("DMS_CLIENT_SECRET", "shh")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://review.firm.com")
    # The token table, which only the capability binding still uses.
    monkeypatch.setenv("DMS_MCP_BINDING", "capability")
    settings.cache_clear()
    yield settings()
    settings.cache_clear()


@pytest.fixture
def client(env):
    return TestClient(main.app)


def issue_link(cfg, address="jim@firm.com") -> str:
    return oauth.consent_url(
        address,
        "imanage",
        cfg.dms_authorize_url,
        cfg.dms_client_id,
        oauth.callback_url(cfg.public_base_url),
    )


def state_of(url: str) -> str:
    return parse_qs(urlparse(url).query)["state"][0]


def backdate(state: str, hours: int) -> None:
    when = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    with connect() as c:
        c.execute("UPDATE oauth_pending SET created_at = ? WHERE state = ?", (when, state))


def created_at(state: str) -> str | None:
    with connect() as c:
        row = c.execute(
            "SELECT created_at FROM oauth_pending WHERE state = ?", (state,)
        ).fetchone()
    return row[0] if row else None


def pending_rows() -> int:
    with connect() as c:
        return c.execute("SELECT COUNT(*) FROM oauth_pending").fetchone()[0]


def token_response(**body) -> httpx.Response:
    payload = {"access_token": "tok-1", "refresh_token": "ref-1", "expires_in": 3600}
    payload.update(body)
    return httpx.Response(200, json=payload, request=httpx.Request("POST", TOKEN))


# --- the authorization request ------------------------------------------


def test_the_consent_link_points_at_the_authorize_endpoint_itself(env):
    """DMS_AUTHORIZE_URL is the authorize endpoint. Appending /authorize to it
    is a 404 on the first link the firm ever clicks."""
    parsed = urlparse(issue_link(env))

    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == AUTHORIZE
    assert parsed.path.count("/authorize") == 1


def test_the_consent_link_carries_the_redirect_uri_the_token_exchange_will_send(env, client, monkeypatch):
    """A conformant server compares the two and rejects the grant as
    invalid_grant if they differ. They used to differ absolutely: the
    authorization request had no redirect_uri at all."""
    link = issue_link(env)
    query = parse_qs(urlparse(link).query)
    assert query["redirect_uri"] == [CALLBACK]

    posted = {}

    def fake_post(url, data=None, timeout=None):
        posted.update(data)
        return token_response()

    monkeypatch.setattr(main.httpx, "post", fake_post)
    client.get("/oauth/callback", params={"code": "abc", "state": state_of(link)})

    assert posted["redirect_uri"] == query["redirect_uri"][0]


def test_the_consent_link_asks_for_the_read_only_scopes(env):
    query = parse_qs(urlparse(issue_link(env)).query)

    assert query["response_type"] == ["code"]
    assert query["client_id"] == ["second-eye-client"]
    assert query["scope"][0].split() == oauth.DEFAULT_SCOPES["imanage"]
    assert query["state"][0]
    # Spaces as %20, not "+": a few authorization servers take the plus sign
    # literally and then reject the scope list as unknown.
    assert "+" not in urlparse(issue_link(env)).query.split("scope=")[1].split("&")[0]


# --- the life of a consent link -----------------------------------------


def test_a_consent_link_can_only_be_claimed_once(env):
    state = state_of(issue_link(env))

    assert oauth.claim_state(state) is not None
    assert oauth.claim_state(state) is None


def test_a_consent_link_stops_working_after_a_day(env):
    """The link sits in a mailbox. A year later that mailbox may be an mbox
    export or a mail-security appliance, and whoever reads it could otherwise
    bind their own document system to this lawyer's address."""
    state = state_of(issue_link(env))
    backdate(state, hours=25)

    assert oauth.claim_state(state) is None


def test_a_consent_link_survives_a_lawyer_reading_their_email_after_lunch(env):
    """The other half of the TTL. Ten minutes is the usual advice and it is
    wrong here: this link arrives by email, not by browser redirect."""
    state = state_of(issue_link(env))
    backdate(state, hours=8)

    claimed = oauth.claim_state(state)
    assert claimed is not None
    assert claimed.user_address == "jim@firm.com"


def test_issuing_a_link_clears_out_the_ones_nobody_clicked(env):
    """Nothing else ever deleted a pending row, so every link a lawyer ignored
    stayed in the table permanently."""
    stale = [state_of(issue_link(env)) for _ in range(3)]
    for state in stale:
        backdate(state, hours=48)
    assert pending_rows() == 3

    issue_link(env)

    assert pending_rows() == 1


# --- the only screen this product has -----------------------------------


def test_an_expired_link_renders_a_page_rather_than_a_json_error(client):
    r = client.get("/oauth/callback", params={"code": "abc", "state": "never-issued"})

    assert r.status_code == 400
    assert r.headers["content-type"].startswith("text/html")
    assert '"detail"' not in r.text
    assert "connect" in r.text


def test_declining_consent_renders_a_page_rather_than_a_validation_error(client):
    """A partner who reads the scope list and decides to check with IT first
    arrives here with an error and no code. That used to be a 422 listing
    FastAPI's validation internals."""
    r = client.get(
        "/oauth/callback",
        params={
            "error": "access_denied",
            "error_description": "<b>user declined</b>",
            "state": "abc",
        },
    )

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "No problem" in r.text
    # The description is attacker-controllable query-string text on an HTML
    # page, so it is logged and never echoed.
    assert "user declined" not in r.text


def test_a_failed_token_exchange_renders_a_page_and_leaves_the_link_usable(env, client, monkeypatch):
    """The exchange failing is our problem or the DMS's. Burning the lawyer's
    only link over it left them with a 500 and no way forward."""
    link = issue_link(env)
    state = state_of(link)
    issued_at = created_at(state)

    def boom(*a, **k):
        raise httpx.ConnectError("dms unreachable")

    monkeypatch.setattr(main.httpx, "post", boom)
    r = client.get("/oauth/callback", params={"code": "abc", "state": state})

    assert r.status_code == 502
    assert r.headers["content-type"].startswith("text/html")
    assert '"detail"' not in r.text
    # Still claimable, and the retry did not walk the expiry forward.
    assert created_at(state) == issued_at
    assert oauth.claim_state(state) is not None


def test_a_rejected_token_exchange_does_not_store_a_token(env, client, monkeypatch):
    link = issue_link(env)
    monkeypatch.setattr(
        main.httpx,
        "post",
        lambda *a, **k: httpx.Response(
            400, json={"error": "invalid_grant"}, request=httpx.Request("POST", TOKEN)
        ),
    )

    r = client.get("/oauth/callback", params={"code": "abc", "state": state_of(link)})

    assert r.status_code == 502
    assert oauth.get("jim@firm.com", "imanage") is None


def test_a_granted_consent_stores_the_scopes_the_server_actually_granted(env, client, monkeypatch):
    """The stored scope list is the audit story. Recording what we asked for
    makes it a record of our intent instead of of the grant."""
    link = issue_link(env)
    monkeypatch.setattr(
        main.httpx, "post", lambda *a, **k: token_response(scope="documents:read user")
    )

    r = client.get("/oauth/callback", params={"code": "abc", "state": state_of(link)})

    assert r.status_code == 200
    assert "Connected" in r.text
    token = oauth.get("jim@firm.com", "imanage")
    assert token is not None
    assert token.scopes == ["documents:read", "user"]
    assert token.expires_at is not None


def test_a_server_that_omits_the_scope_falls_back_to_what_was_requested(env, client, monkeypatch):
    """RFC 6749 5.1 lets a server omit `scope` when the grant matches the
    request, so absent really does mean "what you asked for"."""
    link = issue_link(env)
    monkeypatch.setattr(main.httpx, "post", lambda *a, **k: token_response())

    client.get("/oauth/callback", params={"code": "abc", "state": state_of(link)})

    token = oauth.get("jim@firm.com", "imanage")
    assert token.scopes == oauth.DEFAULT_SCOPES["imanage"]


def test_an_expires_in_sent_as_a_string_does_not_break_a_completed_grant(env, client, monkeypatch):
    link = issue_link(env)
    monkeypatch.setattr(main.httpx, "post", lambda *a, **k: token_response(expires_in="3600"))

    r = client.get("/oauth/callback", params={"code": "abc", "state": state_of(link)})

    assert r.status_code == 200
    assert oauth.get("jim@firm.com", "imanage").expires_at is not None


# --- the inbound webhook ------------------------------------------------


class StubProvider:
    """A mail provider whose verify and parse we can steer."""

    name = "stub"

    def __init__(self, verified=True, verify_error=None, parse_error=None):
        self.verified = verified
        self.verify_error = verify_error
        self.parse_error = parse_error
        self.verify_calls = 0
        self.parsed_on_event_loop = None

    def verify(self, headers, raw_body):
        self.verify_calls += 1
        if self.verify_error:
            raise self.verify_error
        return self.verified

    def parse_inbound(self, payload):
        import asyncio

        try:
            asyncio.get_running_loop()
            self.parsed_on_event_loop = True
        except RuntimeError:
            self.parsed_on_event_loop = False
        if self.parse_error:
            raise self.parse_error
        return InboundEmail(
            message_id="m-1",
            from_address="jim@firm.com",
            to=["review@firm.com"],
            subject="NDA",
            received_at=datetime.now(UTC),
        )

    def send(self, email):
        return "stub"


@pytest.fixture
def stub(monkeypatch):
    provider = StubProvider()
    monkeypatch.setattr(main, "get_provider", lambda: provider)
    monkeypatch.setattr(main, "handle", lambda email: None)
    return provider


def test_parsing_an_inbound_message_does_not_run_on_the_event_loop(client, stub):
    """AgentMail's parse fetches every attachment over HTTP with a blocking
    client. On the event loop that stalls every other connection on the
    worker, /health included, for as long as the fetch takes."""
    r = client.post("/webhooks/inbound", json={"message": {}})

    assert r.status_code == 200
    assert stub.parsed_on_event_loop is False


def test_a_body_over_the_cap_is_refused_before_it_is_authenticated(client, stub, monkeypatch):
    monkeypatch.setenv("MAX_ATTACHMENT_MB", "1")
    settings.cache_clear()

    r = client.post("/webhooks/inbound", content=b"x" * (4 * 1024 * 1024))

    assert r.status_code == 413
    # Not read, not parsed, and not even compared against a secret: an
    # unauthenticated POST does not get to decide how much memory we spend.
    assert stub.verify_calls == 0


def test_a_body_with_no_declared_length_is_still_capped(client, stub, monkeypatch):
    """A chunked request declares no Content-Length, so the cap has to hold
    while the body is being read rather than before it."""
    monkeypatch.setenv("MAX_ATTACHMENT_MB", "1")
    settings.cache_clear()

    def chunks():
        for _ in range(8):
            yield b"x" * (1024 * 1024)

    r = client.post("/webhooks/inbound", content=chunks())

    assert r.status_code == 413
    assert stub.verify_calls == 0


def test_a_signature_header_that_cannot_be_compared_is_a_401_not_a_500(client, monkeypatch):
    """hmac.compare_digest raises TypeError on a non-ASCII str, so one accent
    in a shared-secret header returned a stack trace where an ordinary wrong
    secret returns 401."""
    provider = StubProvider(verify_error=TypeError("non-ASCII"))
    monkeypatch.setattr(main, "get_provider", lambda: provider)

    # Sent as the bytes a real client puts on the wire; a server hands those
    # to the app as the str that then blows up inside compare_digest.
    r = client.post(
        "/webhooks/inbound",
        json={"message": {}},
        headers={"x-inbound-secret": "sé".encode("latin-1")},
    )

    assert r.status_code == 401


def test_an_unsigned_webhook_is_rejected(client, monkeypatch):
    provider = StubProvider(verified=False)
    monkeypatch.setattr(main, "get_provider", lambda: provider)

    assert client.post("/webhooks/inbound", json={"message": {}}).status_code == 401


def test_a_body_that_is_not_json_is_a_400_not_a_500(client, stub):
    r = client.post("/webhooks/inbound", content=b"not json at all")

    assert r.status_code == 400


def test_a_payload_the_adapter_cannot_parse_is_not_retried_forever(client, monkeypatch):
    """A payload we cannot parse will not parse on the retry either, so a 5xx
    would buy a retry storm and nothing else."""
    provider = StubProvider(parse_error=ValueError("no message id"))
    monkeypatch.setattr(main, "get_provider", lambda: provider)

    r = client.post("/webhooks/inbound", json={"message": {}})

    assert r.status_code == 400
    assert main._inflight == 0


def test_the_webhook_pushes_back_when_every_review_slot_is_taken(client, stub, monkeypatch):
    """Better a 503 the provider retries than a 200 "accepted" for a review
    that queues invisibly behind a saturated threadpool."""
    monkeypatch.setattr(main, "MAX_INFLIGHT_REVIEWS", 0)

    r = client.post("/webhooks/inbound", json={"message": {}})

    assert r.status_code == 503
    assert r.headers["retry-after"]


def test_a_review_holds_a_slot_while_it_runs_and_gives_it_back(client, stub, monkeypatch):
    seen = {}

    def fake_handle(email):
        seen["inflight"] = main._inflight

    monkeypatch.setattr(main, "handle", fake_handle)

    client.post("/webhooks/inbound", json={"message": {}})

    assert seen["inflight"] == 1
    assert main._inflight == 0


def test_a_review_that_raises_still_gives_its_slot_back(client, stub, monkeypatch):
    """Otherwise a run of failures walks the service down to zero capacity."""
    def boom(email):
        raise RuntimeError("review blew up")

    monkeypatch.setattr(main, "handle", boom)

    with pytest.raises(RuntimeError):
        client.post("/webhooks/inbound", json={"message": {}})

    assert main._inflight == 0


def test_health_reports_the_review_queue_depth(client, stub):
    body = client.get("/health").json()

    assert body["status"] == "ok"
    assert body["reviews_in_flight"] == 0
    assert body["review_capacity"] == main.MAX_INFLIGHT_REVIEWS
