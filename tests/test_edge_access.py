"""How the application reaches the edge Worker, and what that is not.

The container calls the Worker by an internal name its runtime hands to the
Worker's outbound handler (`http://edge.internal`), so /internal/* is not on
the internet at all. From a laptop, an operator signs each request under
OPERATOR_SECRET, which the Worker checks (cloudflare/src/operator.ts,
cloudflare/test/internal.test.ts). These pin the Python half.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest

from secondeye import edge

ROOT = Path(__file__).resolve().parents[1]
OPERATOR = "o" * 40
# The real one: with SECOND_EYE_TEST_BACKEND=d1, conftest replaces it with the fake
# Worker for every test, and these are about the real request on the wire.
_REAL_CALL = edge.call


@pytest.fixture
def wire(monkeypatch):
    """edge.call against a recording transport instead of the network."""
    from secondeye.config import settings

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"results": []})

    monkeypatch.setattr(edge, "call", _REAL_CALL)
    monkeypatch.setattr(edge, "_client", httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.delenv("OPERATOR_SECRET", raising=False)
    settings.cache_clear()
    yield seen
    settings.cache_clear()


def test_the_signature_matches_the_workers_byte_for_byte():
    # The same vector is asserted against operator.ts's sign() in
    # cloudflare/test/internal.test.ts: the two halves cannot drift apart.
    assert edge.operator_signature(OPERATOR, "POST", "/internal/db", "1790000000",
                                   "abcdefabcdefabcdef", b'{"statements":[]}') == (
        "3307803425551c43d0a83a8635e1c9e5ab5cb05a59a08334991fe56eac204712")


def test_the_container_sends_its_bearer_and_no_operator_headers(wire, monkeypatch):
    monkeypatch.setenv("EDGE_URL", "http://edge.internal")
    monkeypatch.setenv("EDGE_SECRET", "s3cret")
    edge.call("/internal/db", json={"statements": []})
    [request] = wire
    assert str(request.url) == "http://edge.internal/internal/db"
    assert request.headers["authorization"] == "Bearer s3cret"
    assert "x-second-eye-signature" not in request.headers


def test_an_operator_signs_every_request_with_a_fresh_nonce(wire, monkeypatch):
    monkeypatch.setenv("EDGE_URL", "https://legal-review-agent.example.workers.dev")
    monkeypatch.setenv("EDGE_SECRET", "")
    monkeypatch.setenv("OPERATOR_SECRET", OPERATOR)
    assert edge.configured()
    edge.call("/internal/db", json={"statements": []})
    edge.call("/internal/blob/job/" + "a" * 32, method="GET")
    first, second = wire
    assert "authorization" not in first.headers
    assert first.headers["x-second-eye-nonce"] != second.headers["x-second-eye-nonce"]
    for request in wire:
        expected = edge.operator_signature(
            OPERATOR, request.method, request.url.raw_path.decode(),
            request.headers["x-second-eye-time"], request.headers["x-second-eye-nonce"],
            request.content)
        assert request.headers["x-second-eye-signature"] == expected


def test_nothing_is_configured_without_a_url_or_a_credential(monkeypatch):
    from secondeye.config import settings

    monkeypatch.setenv("EDGE_URL", "https://edge.test")
    monkeypatch.setenv("EDGE_SECRET", "")
    monkeypatch.delenv("OPERATOR_SECRET", raising=False)
    settings.cache_clear()
    try:
        assert not edge.configured()
    finally:
        settings.cache_clear()


def worker_source() -> str:
    return (ROOT / "cloudflare" / "src" / "index.ts").read_text()


def test_the_container_is_given_the_internal_name_not_the_public_url():
    source = worker_source()
    assert 'export const EDGE_HOST = "edge.internal"' in source
    assert "EDGE_URL: `http://${EDGE_HOST}`" in source
    # The public URL is not forwarded: the container has no use for it.
    forwarded = source[source.index("const FORWARDED"):source.index("];", source.index("const FORWARDED"))]
    assert '"EDGE_URL"' not in forwarded
    assert '"OPERATOR_SECRET"' not in forwarded


def test_the_public_hostname_serves_internal_only_to_a_signed_operator():
    source = worker_source()
    handler = source[source.index("async fetch("):source.index("async scheduled(")]
    assert "operatorEnabled(env.OPERATOR_SECRET)" in handler
    assert handler.index("operatorEnabled(") < handler.index("verifyOperator(") < handler.index("internal(")
    assert "EDGE_SECRET" not in handler


def test_the_image_trusts_the_egress_ca_only_when_it_is_there():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "/etc/cloudflare/certs/cloudflare-containers-ca.crt" in dockerfile
    assert re.search(r"SSL_CERT_FILE", dockerfile)
