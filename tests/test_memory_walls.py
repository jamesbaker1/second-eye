"""Memory walled by client (ABA Formal Opinion 512; docs/memory.md).

What is learned from one client's documents is read only on that client's
matters. A lawyer's personal memory carries drafting style and nothing from a
client. Firm memory comes only from the firm's playbook.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace as NS

import pytest

from secondeye import clients, memory
from secondeye.memory import Kind, MemoryEntry, Scope, Status
from secondeye.models import InboundEmail
from secondeye.pipeline import identity
from tests.test_learning import FakeMemoryStores

# What undos, dismissals and the sent version teach: LEARN_FROM_OUTCOMES on.
pytestmark = pytest.mark.usefixtures("learning")

JIM = "jim@firm.com"


@pytest.fixture(autouse=True)
def database(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/walls.sqlite3")
    monkeypatch.setenv("INTERNAL_DOMAINS", "firm.com")
    from secondeye.config import settings

    settings.cache_clear()
    clients.register("100", "Acme Corporation", aliases=["Acme"], domains=["acme.com"])
    clients.register("200", "Globex Limited", aliases=["Globex"], domains=["globex.com"])
    yield
    settings.cache_clear()


@pytest.fixture
def stores(monkeypatch):
    import secondeye.config
    from tests import api_contract as contract
    from tests import fake_sessions as fs

    fs.configure(monkeypatch, MANAGED_FIRM_MEMORY_STORE_ID="memstore_firm")
    fake = FakeMemoryStores()
    fake.files["memstore_firm"] = {}
    memories = NS(create=fake.create_memory, list=fake.list_memories, update=fake.update_memory)
    client = contract.strict(NS(beta=NS(memory_stores=NS(create=fake.create_store,
                                                         memories=memories))))
    monkeypatch.setattr(secondeye.config, "anthropic_client", lambda: client)
    yield fake


def everything(stores, mounts: dict[str, str]) -> str:
    return "\n".join(stores.files.get(sid, {}).get(memory.STORE_PATH, "")
                     for sid in mounts.values())


@dataclass
class Undone:
    category: str
    title: str = "A change"


# --- the headline: client A's note never reaches a client B review -----------

def test_a_note_learned_on_client_a_never_reaches_a_client_b_review(stores):
    memory.remember(MemoryEntry(
        scope=Scope.CLIENT, scope_key="100", kind=Kind.POSITION,
        statement="Acme's counterparties always push for a 24-month survival period.",
        confidence=0.9, provenance={"source": "agent", "matter": "100-0001"}))

    # Another of Acme's matters reads it.
    a_again = memory.stores_for(JIM, "100-0002")
    assert "24-month survival" in everything(stores, a_again)
    assert any("24-month" in e.statement for e in memory.recall(JIM, "100-0002"))

    # Globex's matter, the same lawyer, reads nothing of it: no store of
    # Acme's is mounted, and nothing mounted holds the sentence.
    b = memory.stores_for(JIM, "200-0001")
    assert a_again["client"] not in b.values()
    assert "24-month survival" not in everything(stores, b)
    assert not any("24-month" in e.statement for e in memory.recall(JIM, "200-0001"))
    # Nor a review whose client nobody could name.
    unknown = memory.stores_for(JIM, "M-77")
    assert "client" not in unknown
    assert "24-month survival" not in everything(stores, unknown)


def test_a_note_the_agent_writes_on_client_a_stays_there(stores, monkeypatch):
    from secondeye import tools as tools_mod

    doc = NS(filename="acme-spa.docx", blocks=[])
    on_a = tools_mod.build_tools(JIM, doc, "100-0001",
                                 instruction="Remember that Acme conceded the cap.")
    out = on_a["note_for_next_time"](statement="Acme conceded the liability cap at 1x fees.",
                                     scope="client")
    assert out.startswith("Noted (client)")

    assert any("conceded" in e.statement for e in memory.recall(JIM, "100-0009"))
    assert not any("conceded" in e.statement for e in memory.recall(JIM, "200-0001"))
    assert "conceded" not in everything(stores, memory.stores_for(JIM, "200-0001"))
    assert "conceded" not in everything(stores, memory.stores_for(JIM))


# --- personal memory is style only -------------------------------------------

@pytest.mark.parametrize("statement", [
    "Jim accepts uncapped liability.",
    "Jim writes 'will' on Acme deals.",
    "Jim writes 'will' when acme.com is on the email.",
    "Jim writes 'shall' for Initech.",
    "Uses 'will', not 'shall', except in the $5,000,000 cap.",
])
def test_a_personal_note_carrying_client_content_is_refused(statement, monkeypatch):
    refusal = memory.note_refusal(statement, "personal", "100-0001", 0)
    assert refusal and "drafting style only" in refusal
    with pytest.raises(memory.WallError):
        memory.remember(MemoryEntry(scope=Scope.PERSONAL, scope_key=JIM,
                                    kind=Kind.PREFERENCE, statement=statement,
                                    confidence=0.9))


def test_a_style_preference_follows_the_lawyer_across_clients():
    memory.remember(MemoryEntry(scope=Scope.PERSONAL, scope_key=JIM, kind=Kind.PREFERENCE,
                                statement="Jim writes 'will', not 'shall'.", confidence=0.9))
    memory.remember(MemoryEntry(scope=Scope.PERSONAL, scope_key=JIM, kind=Kind.PREFERENCE,
                                statement="Writes dates as 1 January 2026.", confidence=0.9))
    for matter in ("100-0001", "200-0001", None):
        said = [e.statement for e in memory.recall(JIM, matter)]
        assert "Jim writes 'will', not 'shall'." in said
        assert "Writes dates as 1 January 2026." in said


def test_a_position_is_never_personal_whatever_it_says():
    with pytest.raises(memory.WallError):
        memory.remember(MemoryEntry(scope=Scope.PERSONAL, scope_key=JIM, kind=Kind.POSITION,
                                    statement="Prefers 'will' to 'shall'.", confidence=0.9))


def test_firm_memory_comes_only_from_the_playbook():
    assert "Firm-wide" in memory.note_refusal("Dates as 1 January.", "firm", "100-1", 0)
    with pytest.raises(memory.WallError):
        memory.remember(MemoryEntry(scope=Scope.FIRM, scope_key="", kind=Kind.CONVENTION,
                                    statement="Dates as 1 January 2026.", confidence=0.9,
                                    provenance={"source": "agent"}))
    memory.remember(MemoryEntry(scope=Scope.FIRM, scope_key="", kind=Kind.CONVENTION,
                                statement="Dates as 1 January 2026.", confidence=0.9,
                                provenance={"source": "playbook", "version": 3}))
    assert [e.scope for e in memory.recall(JIM)] == [Scope.FIRM]


def test_a_client_note_needs_a_known_client():
    assert "No client identified" in memory.note_refusal("A fact.", "client", "M-77", 0)
    assert memory.note_refusal("A fact.", "client", "100-0001", 0) is None


# --- what the lawyer does and says is walled too -----------------------------

def test_undos_of_a_deal_category_are_learned_and_kept_under_that_client():
    for _ in range(5):
        memory.record_rejections("job", JIM, "100-0001", [Undone("indemnity-cap")])
    on_a = memory.as_prompt_block(memory.recall(JIM, "100-0003"))
    assert "indemnity-cap" in on_a and f"For {JIM}" in on_a
    assert "indemnity-cap" not in memory.as_prompt_block(memory.recall(JIM, "200-0001"))
    assert "indemnity-cap" not in memory.as_prompt_block(memory.recall(JIM))
    # Announced on Acme's documents, never on Globex's.
    assert memory.next_announcement(JIM, "200-0001") is None
    assert memory.next_announcement(JIM) is None
    assert "indemnity-cap" in memory.next_announcement(JIM, "100-0003")[1]


def test_undos_on_another_client_do_not_count_towards_this_ones():
    for _ in range(3):
        memory.record_rejections("job", JIM, "100-0001", [Undone("indemnity-cap")])
    for _ in range(3):
        memory.record_rejections("job", JIM, "200-0001", [Undone("indemnity-cap")])
    assert "indemnity-cap" not in memory.as_prompt_block(memory.recall(JIM, "100-0001"))
    assert "indemnity-cap" not in memory.as_prompt_block(memory.recall(JIM, "200-0001"))


def test_undos_of_a_style_category_stay_personal():
    for _ in range(5):
        memory.record_rejections("job", JIM, "100-0001", [Undone("date-format")])
    assert "date-format" in memory.as_prompt_block(memory.recall(JIM, "200-0001"))


def test_stop_flagging_something_from_a_document_is_kept_under_its_client():
    asked = memory.suppression_request("stop flagging the Acme change of control point")
    assert memory.suppression_home(asked, JIM, None) is None
    assert not memory.remember_suppression(JIM, asked)
    assert memory.recall(JIM) == []

    assert memory.remember_suppression(JIM, asked, matter_id="100-0001")
    assert any("change of control" in e.statement for e in memory.recall(JIM, "100-0002"))
    assert not any("change of control" in e.statement for e in memory.recall(JIM, "200-0001"))
    # "Flag it again" on Globex's thread cannot reach it, nor say it exists.
    assert memory.forget_suppression(JIM, "change of control", "200-0001") == 0
    assert memory.forget_suppression(JIM, "change of control", "100-0002") == 1


def test_stop_flagging_a_point_that_names_no_client_is_personal():
    for said in ("stop flagging the Oxford comma", "stop flagging the governing law clause"):
        asked = memory.suppression_request(said)
        assert memory.suppression_home(asked, JIM, "100-0001") == (Scope.PERSONAL, JIM)


# --- whose matter is it -------------------------------------------------------

def email(**kw) -> InboundEmail:
    base = {"message_id": "m", "from_address": JIM, "to": ["review@example.com"],
            "subject": "SPA", "text_body": "", "received_at": datetime.now(UTC)}
    return InboundEmail(**{**base, **kw})


def test_the_client_comes_from_the_matter_number():
    assert clients.for_matter("100-0042") == "100"
    assert clients.for_matter("100.0042") == "100"
    assert clients.for_matter("300-0001") is None, "an unregistered client is unknown"
    assert clients.for_matter("M123") is None


def test_failing_that_from_one_clients_domain_or_name():
    on_email = email(cc=["gc@acme.com"])
    assert identity.resolve_client(on_email, "X-1") == "100"
    assert clients.for_matter("X-1") == "100", "linked, so replies on it find the client"
    forwarded = email(text_body="---------- Forwarded ----------\nFrom: Bob <bob@globex.com>")
    assert identity.resolve_client(forwarded, None) == "200"
    assert identity.resolve_client(email(), None, "between Acme Corporation and Initech") == "100"


def test_two_clients_or_none_is_no_client():
    assert identity.resolve_client(email(), None, "between Acme and Globex Limited") is None
    assert identity.resolve_client(email(cc=["a@acme.com", "b@globex.com"]), "Y-1") is None
    assert clients.for_matter("Y-1") is None
    assert identity.resolve_client(email(cc=["c@initech.com"]), None, "Initech") is None


def test_the_matter_number_wins_over_the_parties():
    assert identity.resolve_client(email(cc=["b@globex.com"]), "100-0001") == "100"


# --- rows from before the wall -------------------------------------------------

LEGACY = """
DROP TABLE IF EXISTS memory;
DROP TABLE IF EXISTS suggestion_outcomes;
CREATE TABLE memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, scope_key TEXT NOT NULL,
    kind TEXT NOT NULL, statement TEXT NOT NULL, confidence REAL NOT NULL,
    samples INTEGER NOT NULL DEFAULT 1, provenance TEXT,
    status TEXT NOT NULL DEFAULT 'confirmed', announced_at TEXT,
    created_at TEXT, updated_at TEXT);
CREATE TABLE suggestion_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, user_address TEXT, matter_id TEXT,
    category TEXT, severity TEXT, title TEXT, anchor TEXT, suggested_text TEXT,
    outcome TEXT, observed_at TEXT);
"""


def test_rows_that_would_cross_the_wall_are_moved_or_dropped(monkeypatch, tmp_path):
    from secondeye.config import settings
    from secondeye.store import connect

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/legacy.sqlite3")
    settings.cache_clear()
    rows = [
        ("personal", JIM, "preference", "Jim writes 'will', not 'shall'.", {}),
        ("personal", JIM, "preference", "Acme conceded the cap.", {"matter": "100-0001"}),
        ("personal", JIM, "preference", "Counterparty accepts uncapped liability.", {}),
        ("personal", JIM, "position", "Always asks for a 12-month cap.", {}),
        ("firm", "", "convention", "Firms like this accept uncapped liability.",
         {"source": "agent"}),
    ]
    with connect() as c:
        c.executescript(LEGACY)
        for scope, key, kind, statement, provenance in rows:
            c.execute("INSERT INTO memory (scope, scope_key, kind, statement, confidence, "
                      "provenance) VALUES (?, ?, ?, ?, 0.9, ?)",
                      (scope, key, kind, statement, json.dumps(provenance)))
    clients.register("100", "Acme Corporation", aliases=["Acme"], domains=["acme.com"])

    everywhere = [e.statement for e in memory.recall(JIM, "200-0001")]
    assert everywhere == ["Jim writes 'will', not 'shall'."]
    on_the_matter = memory.recall(JIM, "100-0001")
    moved = [e for e in on_the_matter if e.statement == "Acme conceded the cap."]
    assert [(e.scope, e.scope_key) for e in moved] == [(Scope.MATTER, "100-0001")]
    with connect() as c:
        left = {r[0] for r in c.execute("SELECT statement FROM memory")}
    assert "Counterparty accepts uncapped liability." not in left
    assert "Always asks for a 12-month cap." not in left
    assert "Firms like this accept uncapped liability." not in left


def test_registering_a_client_walls_a_note_that_names_it():
    memory.remember(MemoryEntry(scope=Scope.PERSONAL, scope_key=JIM, kind=Kind.PREFERENCE,
                                statement="Uses 'shall' throughout for initech.",
                                confidence=0.9, status=Status.CONFIRMED,
                                provenance={"matter": "300-0001"}))
    assert memory.recall(JIM, "200-0001")
    clients.register("300", "Initech")
    assert memory.recall(JIM, "200-0001") == []
    assert [e.scope for e in memory.recall(JIM, "300-0001")] == [Scope.MATTER]


def test_a_review_mounts_the_client_its_parties_name(stores):
    from secondeye import handler

    job = NS(email=email(cc=["gc@globex.com"]), matter=None,
             doc=NS(blocks=[NS(text="Globex Limited and Initech LLC")]))
    assert handler._client_of(job) == "200"
    mounts = memory.stores_for(JIM, None, handler._client_of(job))
    assert mounts["client"] == memory.store_for(Scope.CLIENT, "200")
    assert memory.store_for(Scope.CLIENT, "100") not in mounts.values()
