"""The document system through our MCP server and Anthropic vaults
(docs/migration.md, phases 4 and 5).

Held here, against fakes of the vault and session APIs shaped from the SDK's
own types: the consent callback turns the grant into the lawyer's vault
credential and writes nothing to our token table; "revoke" archives it; a
session is bound to its matter with a signed URL through an agent override
and gets the lawyer's vault; the agents get an always_allow MCP toolset in
place of the custom DMS tools; the environment lets them reach it; and the
capability fallback works without a code change.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from types import SimpleNamespace as NS
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from lra import consent, dms_mcp, main, managed, oauth
from lra.config import settings
from lra.models import Attachment, Mode
from lra.pipeline import extract, review
from lra.tools import DEFINITIONS, build_tools
from tests import api_contract as contract
from tests import fake_sessions as fs

MCP_URL = "https://dms-mcp.test/mcp"
KEY = "test-signing-key"
TOKEN_URL = "https://dms.example.com/auth/oauth2/token"
JIM = "jim@firm.com"


class APIError(Exception):
    def __init__(self, status_code: int):
        super().__init__(f"status {status_code}")
        self.status_code = status_code


class FakeVaults:
    """`client.beta.vaults` and `.credentials`, as the SDK shapes them."""

    def __init__(self) -> None:
        self.vaults: dict[str, dict] = {}
        self.credentials: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.credentials_api = NS(create=self._create_credential, update=self._update,
                                  archive=self._archive)
        self.client = contract.strict(NS(beta=NS(vaults=NS(
            create=self._create_vault, delete=self._delete, credentials=self.credentials_api))))

    def _create_vault(self, *, display_name, metadata=None):
        vault_id = f"vlt_{len(self.vaults) + 1}"
        self.vaults[vault_id] = {"display_name": display_name, "metadata": metadata}
        self.calls.append(("vaults.create", vault_id))
        return NS(id=vault_id)

    def _delete(self, vault_id):
        self.calls.append(("vaults.delete", vault_id))
        if vault_id not in self.vaults:
            raise APIError(404)
        del self.vaults[vault_id]

    def _create_credential(self, vault_id, *, auth, display_name=None, metadata=None):
        assert vault_id in self.vaults
        credential_id = f"vcrd_{len(self.credentials) + 1}"
        self.credentials[credential_id] = {"vault_id": vault_id, "auth": auth,
                                           "archived": False}
        self.calls.append(("credentials.create", credential_id))
        return NS(id=credential_id, vault_id=vault_id)

    def _update(self, credential_id, *, vault_id, auth):
        found = self.credentials.get(credential_id)
        if not found or found["archived"] or found["vault_id"] != vault_id:
            raise APIError(404)
        self.calls.append(("credentials.update", credential_id, auth))
        return NS(id=credential_id)

    def _archive(self, credential_id, *, vault_id):
        found = self.credentials.get(credential_id)
        if not found:
            raise APIError(404)
        found["archived"] = True
        self.calls.append(("credentials.archive", credential_id))
        return NS(id=credential_id, vault_id=vault_id)


@pytest.fixture
def vaults(monkeypatch):
    fake = FakeVaults()
    monkeypatch.setattr(dms_mcp, "_client", lambda client=None: client or fake.client)
    return fake


@pytest.fixture(autouse=True)
def mcp_mode(monkeypatch, tmp_path):
    fs.configure(monkeypatch, DATABASE_URL=f"sqlite:///{tmp_path}/m.sqlite3",
                 DMS_PROVIDER="imanage", DMS_MCP_URL=MCP_URL,
                 DMS_MCP_SIGNING_KEY=KEY, DMS_MCP_BINDING="query",
                 DMS_TOKEN_URL=TOKEN_URL, DMS_CLIENT_ID="lra-client",
                 DMS_CLIENT_SECRET="shh",
                 DMS_AUTHORIZE_URL="https://dms.example.com/auth/oauth2/authorize",
                 PUBLIC_BASE_URL="https://review.firm.com")
    yield
    settings.cache_clear()


def grant(**extra) -> dict:
    return {"access_token": "tok-1", "refresh_token": "ref-1", "expires_in": 3600, **extra}


def token_rows() -> int:
    oauth.init()
    from lra.store import connect

    with connect() as c:
        return c.execute("SELECT COUNT(*) FROM oauth_tokens").fetchone()[0]


# --- the binding ---------------------------------------------------------------

def test_the_signature_matches_the_mcp_worker_byte_for_byte():
    # cloudflare/dms-mcp/test/server.test.ts holds the same vector.
    assert dms_mcp.sign(JIM, "FAL-001", 2000000000) == (
        "8f0c86c8e1d04d9fce17d0b8ddef505339f78782b19f4ec2aebf9b0612141bdd")


def test_the_bound_url_carries_lawyer_matter_expiry_and_signature():
    url = dms_mcp.bound_url("Jim@Firm.com", "FAL-001", 2000000000)
    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == MCP_URL
    q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    assert q == {"u": JIM, "m": "FAL-001", "exp": "2000000000",
                 "sig": dms_mcp.sign(JIM, "FAL-001", 2000000000)}


def test_a_capability_is_signed_over_its_payload():
    cap = dms_mcp.capability(JIM, "FAL-001", "tok-1", 2000000000)
    assert cap.startswith("cap1.")
    body, mac = cap[len("cap1."):].split(".")
    expected = hmac.new(KEY.encode(), ("cap1." + body).encode(), hashlib.sha256).digest()
    assert base64.urlsafe_b64decode(mac + "=" * (-len(mac) % 4)) == expected
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    assert payload == {"u": JIM, "m": "FAL-001", "exp": 2000000000, "t": "tok-1"}


# --- consent writes the vault, not our table ------------------------------------

def test_a_grant_becomes_the_lawyers_vault_credential_with_anthropic_refreshing_it(vaults):
    found = dms_mcp.store_grant(JIM, grant())
    assert found.vault_id == "vlt_1" and found.credential_id == "vcrd_1"
    auth = vaults.credentials["vcrd_1"]["auth"]
    assert auth["type"] == "mcp_oauth"
    assert auth["mcp_server_url"] == MCP_URL
    assert auth["access_token"] == "tok-1"
    assert auth["expires_at"]
    assert auth["refresh"] == {
        "refresh_token": "ref-1", "client_id": "lra-client", "token_endpoint": TOKEN_URL,
        "token_endpoint_auth": {"type": "client_secret_post", "client_secret": "shh"}}
    assert dms_mcp.record_for_vault("vlt_1").user_address == JIM
    assert token_rows() == 0


def test_a_second_grant_updates_the_credential_in_place(vaults):
    dms_mcp.store_grant(JIM, grant())
    again = dms_mcp.store_grant(JIM, grant(access_token="tok-2", refresh_token="ref-2"))
    assert (again.vault_id, again.credential_id) == ("vlt_1", "vcrd_1")
    [update] = [c for c in vaults.calls if c[0] == "credentials.update"]
    assert update[2]["access_token"] == "tok-2"
    assert update[2]["refresh"] == {"refresh_token": "ref-2"}
    assert len(vaults.vaults) == 1


def test_a_grant_after_revoke_makes_a_new_credential_in_the_same_vault(vaults):
    dms_mcp.store_grant(JIM, grant())
    assert dms_mcp.revoke(JIM)
    assert vaults.credentials["vcrd_1"]["archived"]
    assert not dms_mcp.connected(JIM)
    again = dms_mcp.store_grant(JIM, grant())
    assert (again.vault_id, again.credential_id) == ("vlt_1", "vcrd_2")


def test_needs_consent_reads_the_vault_record_not_the_token_table(vaults, monkeypatch):
    monkeypatch.setattr(oauth, "get", lambda *a: pytest.fail("token table read"))
    assert consent.needs_consent(JIM)
    dms_mcp.store_grant(JIM, grant())
    assert not consent.needs_consent(JIM)


def test_the_callback_stores_the_grant_in_the_vault_and_nothing_in_our_table(vaults,
                                                                              monkeypatch):
    cfg = settings()
    link = oauth.consent_url(JIM, "imanage", cfg.dms_authorize_url, cfg.dms_client_id,
                             oauth.callback_url(cfg.public_base_url))
    state = parse_qs(urlparse(link).query)["state"][0]
    monkeypatch.setattr(main.httpx, "post", lambda *a, **k: httpx.Response(
        200, json=grant(), request=httpx.Request("POST", TOKEN_URL)))
    page = TestClient(main.app).get(f"/oauth/callback?code=c&state={state}")
    assert page.status_code == 200 and "Connected." in page.text
    assert dms_mcp.connected(JIM)
    assert token_rows() == 0


def test_the_callback_says_so_when_the_vault_cannot_be_written(vaults, monkeypatch):
    cfg = settings()
    link = oauth.consent_url(JIM, "imanage", cfg.dms_authorize_url, cfg.dms_client_id,
                             oauth.callback_url(cfg.public_base_url))
    state = parse_qs(urlparse(link).query)["state"][0]
    monkeypatch.setattr(main.httpx, "post", lambda *a, **k: httpx.Response(
        200, json=grant(), request=httpx.Request("POST", TOKEN_URL)))

    def down(**kw):
        raise APIError(500)

    monkeypatch.setattr(vaults.client.beta.vaults, "create", down)
    page = TestClient(main.app).get(f"/oauth/callback?code=c&state={state}")
    assert page.status_code == 502 and "connect" in page.text
    assert not dms_mcp.connected(JIM)


def test_revoke_raises_rather_than_claim_a_disconnection_that_did_not_happen(vaults,
                                                                             monkeypatch):
    dms_mcp.store_grant(JIM, grant())

    def down(credential_id, *, vault_id):
        raise APIError(500)

    monkeypatch.setattr(vaults.credentials_api, "archive", down)
    with pytest.raises(APIError):
        dms_mcp.revoke(JIM)
    assert dms_mcp.connected(JIM)


# --- the custom-tool path is gone -------------------------------------------------

def test_there_are_no_custom_dms_tools_and_the_token_table_is_not_read(monkeypatch):
    monkeypatch.setattr(oauth, "get", lambda *a: pytest.fail("token table read"))
    doc = NS(blocks=[], filename="a.docx")
    handlers = build_tools(JIM, doc, "FAL-001")
    assert set(handlers) == {"read_document", "note_for_next_time"}
    assert not dms_mcp.token_table_in_use()


def test_with_no_document_system_nothing_is_bound(monkeypatch):
    monkeypatch.setenv("DMS_PROVIDER", "none")
    settings.cache_clear()
    assert not dms_mcp.enabled() and not dms_mcp.token_table_in_use()
    assert managed.networking() == managed.NETWORKING
    assert dms_mcp.for_session(JIM, "FAL-001") is None


# --- the agents and the environment ----------------------------------------------

def test_a_document_system_agent_gets_the_mcp_server_with_always_allow():
    for manifest, custom in (("review.agent.yaml", []), ("associate.agent.yaml", DEFINITIONS)):
        body = managed.agent_body(managed.load_manifest(managed.AGENTS / manifest), custom)
        assert "document_system" not in body
        assert body["mcp_servers"] == [{"type": "url", "name": "dms", "url": MCP_URL}]
        [toolset] = [t for t in body["tools"] if t["type"] == "mcp_toolset"]
        assert toolset["mcp_server_name"] == "dms"
        assert toolset["default_config"]["permission_policy"] == {"type": "always_allow"}
        names = {t.get("name") for t in body["tools"] if t["type"] == "custom"}
        assert not names & {"search_firm_documents", "read_firm_document", "matter_history"}
        if manifest == "review.agent.yaml":
            assert not names
            continue
        assert "note_for_next_time" in names


def test_an_agent_that_does_not_read_the_document_system_gets_no_server():
    body = managed.agent_body(managed.load_manifest(managed.AGENTS / "closing.agent.yaml"), [])
    # Empty, not absent: an update keeps what it omits, and a server left
    # behind with no mcp_toolset naming it is a 400.
    assert body["mcp_servers"] == []
    assert not [t for t in body["tools"] if t.get("type") == "mcp_toolset"]


def test_the_environment_lets_the_agents_reach_their_mcp_servers():
    assert managed.networking()["allow_mcp_servers"] is True
    created = {}
    client = contract.strict(NS(beta=NS(environments=NS(
        create=lambda **kw: created.update(kw) or NS(id="e")))))
    managed.apply_environment(client, "")
    assert created["config"]["networking"]["allow_mcp_servers"] is True


# --- each session is bound to its matter ------------------------------------------

def test_a_session_is_bound_to_its_matter_with_the_lawyers_vault(vaults):
    dms_mcp.store_grant(JIM, grant())
    access = dms_mcp.session_access(JIM, "FAL-001", now=1_000_000)
    [server] = access.overrides["mcp_servers"]
    assert server["name"] == "dms"
    q = {k: v[0] for k, v in parse_qs(urlparse(server["url"]).query).items()}
    assert q["m"] == "FAL-001" and q["u"] == JIM
    assert int(q["exp"]) == 1_000_000 + settings().dms_mcp_binding_ttl_seconds
    assert access.vault_ids == ["vlt_1"] and access.ephemeral == []


def test_a_session_with_no_matter_is_not_bound():
    assert dms_mcp.session_access(JIM, None).overrides is None
    assert dms_mcp.session_access(JIM, None).vault_ids == []


def test_an_unconnected_lawyers_session_is_bound_but_has_no_vault(vaults):
    access = dms_mcp.session_access(JIM, "FAL-001")
    assert access.overrides and access.vault_ids == []


def test_a_review_is_created_with_the_override_and_the_vault(vaults):
    dms_mcp.store_grant(JIM, grant())
    fake = fs.DetachedSessions([])
    d = __import__("docx").Document()
    d.add_paragraph("1. Term.")
    from io import BytesIO

    buf = BytesIO()
    d.save(buf)
    raw = buf.getvalue()
    doc = extract.extract(Attachment(
        filename="a.docx", size_bytes=len(raw), content=raw,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document"))
    review.start(doc, Mode.REDLINE, "", matter_id="FAL-001", original=raw, client=fake.client,
                 user_address=JIM)
    [create] = fake.created
    assert create["agent"]["type"] == "agent_with_overrides"
    assert create["agent"]["id"] == settings().managed_review_agent_id
    assert create["agent"]["mcp_servers"][0]["url"].startswith(MCP_URL + "?")
    assert create["vault_ids"] == ["vlt_1"]


def test_a_binding_failure_costs_the_lookups_not_the_review(monkeypatch):
    monkeypatch.setenv("DMS_MCP_SIGNING_KEY", "")
    settings.cache_clear()
    access = dms_mcp.for_session(JIM, "FAL-001")
    assert access is not None and access.overrides is None and access.vault_ids == []


# --- the capability fallback -------------------------------------------------------

def test_the_capability_fallback_uses_a_vault_for_the_one_session(vaults, monkeypatch):
    monkeypatch.setenv("DMS_MCP_BINDING", "capability")
    settings.cache_clear()
    assert not dms_mcp.vault_held() and dms_mcp.token_table_in_use()
    from datetime import UTC, datetime, timedelta

    oauth.save(oauth.Token(JIM, "imanage", "tok-held", "ref", [],
                           datetime.now(UTC) + timedelta(hours=1)))
    access = dms_mcp.session_access(JIM, "FAL-001")
    assert access.overrides is None, "the bare URL: the binding is in the bearer"
    [vault_id] = access.vault_ids
    assert access.ephemeral == [vault_id]
    [cred] = [c for c in vaults.credentials.values() if c["vault_id"] == vault_id]
    assert cred["auth"]["type"] == "static_bearer"
    assert cred["auth"]["mcp_server_url"] == MCP_URL
    body = cred["auth"]["token"][len("cap1."):].split(".")[0]
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    assert payload["m"] == "FAL-001" and payload["t"] == "tok-held"

    dms_mcp.attach_session(access, "sesn_1")
    managed.close_session(contract.strict(NS(beta=NS(
        sessions=NS(events=NS(send=lambda **k: {}), archive=lambda s: {"id": s},
                    retrieve=lambda s: {"id": s, "status": "idle"}),
        files=NS(list=lambda **k: [], delete=lambda f: {"id": f}),
        vaults=vaults.client.beta.vaults))), "sesn_1")
    assert vault_id not in vaults.vaults


def test_expired_session_vaults_are_swept(vaults, monkeypatch):
    monkeypatch.setenv("DMS_MCP_BINDING", "capability")
    monkeypatch.setenv("DMS_MCP_BINDING_TTL_SECONDS", "-1")
    settings.cache_clear()
    from datetime import UTC, datetime, timedelta

    oauth.save(oauth.Token(JIM, "imanage", "tok-held", None, [],
                           datetime.now(UTC) + timedelta(hours=1)))
    access = dms_mcp.session_access(JIM, "FAL-001")
    assert dms_mcp.sweep(vaults.client) == 1
    assert access.vault_ids[0] not in vaults.vaults
