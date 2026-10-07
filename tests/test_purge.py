"""`lra purge --client / --matter / --lawyer`: everything held for one scope,
Anthropic's copies included, and nothing outside it (purge.py).

The fixture builds two clients' work side by side: Acme (client 100) on two
matters and a conversation known to be Acme's only from its parties, and
Globex (client 200) on one matter, plus one old conversation with no client or
matter at all. Every Anthropic call goes through the SDK's own contract
(tests/api_contract.py).
"""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace as NS

import anthropic
import httpx2
import pytest

from lra import archive, audit, blackline, blobs, clients, closing, memory, purge, thread
from lra.memory import Kind, MemoryEntry, Scope
from lra.models import Attachment, InboundEmail
from lra.store import claim, connect
from tests import api_contract as contract
from tests import fake_sessions as fs
from tests.test_learning import FakeMemoryStores

JIM = "jim@firm.com"
KATE = "kate@firm.com"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _not_found(what: str):
    request = httpx2.Request("DELETE", "https://api.anthropic.com/v1/x")
    return anthropic.NotFoundError(f"{what} not found", response=httpx2.Response(404, request=request),
                                   body={"type": "error", "error": {"type": "not_found_error",
                                                                     "message": "not found"}})


class FakeAnthropic:
    """Memory stores, sessions and files, as one strict client."""

    def __init__(self):
        self.stores = FakeMemoryStores()
        self.stores.files["memstore_firm"] = {}
        self.sessions: dict[str, str] = {}          # id -> status
        self.outputs: dict[str, list[str]] = {}     # session id -> file ids it wrote
        self.files: set[str] = set()                # uploads still held
        self.deleted_stores: list[str] = []
        self.deleted_sessions: list[str] = []
        self.deleted_files: list[str] = []
        self.interrupted: list[str] = []
        # Ids whose next delete fails as a network error would, once each.
        self.fail_once: set[str] = set()
        memories = NS(create=self.stores.create_memory, list=self.stores.list_memories,
                      update=self.stores.update_memory)
        beta = NS(
            memory_stores=NS(create=self.stores.create_store, delete=self.delete_store,
                             memories=memories),
            sessions=NS(retrieve=self.retrieve, delete=self.delete_session,
                        events=NS(send=self.send)),
            files=NS(list=self.list_files, delete=self.delete_file),
        )
        self.client = contract.strict(NS(beta=beta))

    def calls(self) -> int:
        return (len(self.deleted_stores) + len(self.deleted_sessions) + len(self.deleted_files)
                + len(self.interrupted))

    def _maybe_fail(self, ident: str) -> None:
        if ident in self.fail_once:
            self.fail_once.discard(ident)
            raise anthropic.APIConnectionError(request=httpx2.Request("DELETE", "http://fake"),
                                               message="connection reset")

    def delete_store(self, store_id, **kw):
        self._maybe_fail(store_id)
        if store_id not in self.stores.files:
            raise _not_found(store_id)
        del self.stores.files[store_id]
        self.deleted_stores.append(store_id)
        return {"id": store_id, "type": "memory_store_deleted"}

    def retrieve(self, session_id, **kw):
        if session_id not in self.sessions:
            raise _not_found(session_id)
        return fs.session_object(session_id, self.sessions[session_id])

    def send(self, session_id, events, **kw):
        self.interrupted.append(session_id)
        self.sessions[session_id] = "idle"
        return {"data": []}

    def delete_session(self, session_id, **kw):
        self._maybe_fail(session_id)
        if session_id not in self.sessions:
            raise _not_found(session_id)
        del self.sessions[session_id]
        self.deleted_sessions.append(session_id)
        return {"id": session_id, "type": "session_deleted"}

    def list_files(self, **kw):
        sid = kw["scope_id"]
        return [fs.file_metadata(f, f"{f}.docx", 10, sid) for f in self.outputs.get(sid, [])
                if f in self.files]

    def delete_file(self, file_id, **kw):
        self._maybe_fail(file_id)
        if file_id not in self.files:
            raise _not_found(file_id)
        self.files.discard(file_id)
        self.deleted_files.append(file_id)
        return {"id": file_id, "type": "file_deleted"}


@pytest.fixture
def api(monkeypatch, tmp_path):
    import lra.config

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/purge.sqlite3")
    monkeypatch.setenv("INTERNAL_DOMAINS", "firm.com")
    monkeypatch.setenv("ARCHIVE_ENABLED", "true")
    # The suggestion outcomes a purge deletes are recorded only when learning.
    monkeypatch.setenv("LEARN_FROM_OUTCOMES", "true")
    fs.configure(monkeypatch, MANAGED_FIRM_MEMORY_STORE_ID="memstore_firm")
    fake = FakeAnthropic()
    monkeypatch.setattr(lra.config, "anthropic_client", lambda: fake.client)
    yield fake
    from lra.config import settings

    settings.cache_clear()


def _email(job_id: str, sender: str, matter: str | None = None, to: tuple = (),
           in_reply_to: str | None = None, filename: str = "Draft.docx") -> InboundEmail:
    return InboundEmail(
        message_id=f"m-{job_id}", from_address=sender, to=["review@firm.com", *to],
        in_reply_to=in_reply_to, subject="Draft for review",
        text_body=f"Matter no. {matter}" if matter else "Have a look, please.",
        attachments=[Attachment(filename=filename, content_type=DOCX, size_bytes=3,
                                content=b"doc")],
        received_at=datetime.now(UTC))


@contextlib.contextmanager
def _job(api: FakeAnthropic, job_id: str, email: InboundEmail, sessions=(), outputs=(),
         uploads=()):
    """One job as the handler runs it: claimed, audited, and in its context
    while whatever it does runs; then its sessions and uploads, as managed
    records them."""
    claim(job_id, email.message_id, email.from_address)
    audit.received(job_id, email)
    token = audit.begin(job_id)
    try:
        yield
        for sid in sessions:
            audit.session(sid)
            api.sessions[sid] = "idle"
            api.outputs[sid] = [f"{sid}_out"]
            api.files.add(f"{sid}_out")
        audit.uploaded(list(uploads))
        api.files.update(uploads)
    finally:
        audit.end(token)


def _note(scope: Scope, key: str, statement: str, kind: Kind = Kind.POSITION,
          owner: str = "", matter: str = "") -> None:
    memory.remember(MemoryEntry(scope=scope, scope_key=key, kind=kind, statement=statement,
                                confidence=0.9, owner=owner,
                                provenance={"source": "agent", "matter": matter}))


@pytest.fixture
def held(api):
    clients.register("100", "Acme Corporation", aliases=["Acme"], domains=["acme.com"])
    clients.register("200", "Globex Limited", aliases=["Globex"], domains=["globex.com"])

    # Acme, matter 100-0001: a conversation with an earlier version and a
    # negotiation ledger, a closing with a signed page, archived mail.
    email = _email("job-a", JIM, "100-0001", filename="Acme SPA.docx")
    with _job(api, "job-a", email, sessions=["sesn_a"], uploads=["file_a"]):
        thread.start("t-acme", JIM, "Acme SPA.docx", b"v1", "100-0001", client_id="100")
        thread.add_alias("t-acme", email.message_id)
        thread.supersede("t-acme", "Acme SPA v2.docx", b"v2")
        archive.store(email, JIM, "received", matter_id="100-0001", client_id="100")
    negotiate("t-acme")
    email = _email("job-close", JIM, "100-0001")
    with _job(api, "job-close", email):
        closing.create("close-acme", JIM, "Acme closing", matter_id="100-0001", client_id="100")
        audit.thread("close-acme")
    closing.keep_file("close-acme", "Acme signed page.pdf", "signed", b"signed")

    # A reply on the Acme conversation that names no matter.
    reply = _email("job-r", JIM, in_reply_to="m-job-a")
    with _job(api, "job-r", reply, sessions=["sesn_r"]):
        assert thread.resolve(JIM, None, reply.message_id, reply.in_reply_to) == "t-acme"

    # Acme, known only from its parties: no matter number.
    with _job(api, "job-b", _email("job-b", JIM, to=("counsel@acme.com",)), sessions=["sesn_b"]):
        thread.start("t-acme-parties", JIM, "Side letter.docx", b"side", None, client_id="100")

    # Acme's other matter, another lawyer: Acme's by its number alone.
    with _job(api, "job-c", _email("job-c", KATE, "100-0002"), sessions=["sesn_c"]):
        thread.start("t-acme-2", KATE, "Acme lease.docx", b"lease", "100-0002")

    # Globex: everything that must survive.
    email = _email("job-g", JIM, "200-0001", filename="Globex APA.docx")
    with _job(api, "job-g", email, sessions=["sesn_g"], uploads=["file_g"]):
        thread.start("t-globex", JIM, "Globex APA.docx", b"g1", "200-0001", client_id="200")
        thread.supersede("t-globex", "Globex APA v2.docx", b"g2")
        archive.store(email, JIM, "received", matter_id="200-0001", client_id="200")
    negotiate("t-globex")
    closing.create("close-globex", JIM, "Globex closing", matter_id="200-0001", client_id="200")

    # From before any of this was recorded: no client, no matter.
    with _job(api, "job-old", _email("job-old", JIM)):
        thread.start("t-old", JIM, "Old.docx", b"old", None)

    # What was learned.
    _note(Scope.CLIENT, "100", "Acme has never accepted an uncapped indemnity.",
          matter="100-0001")
    _note(Scope.CLIENT, "100", "Acme's counterparties concede on caps late.", matter="100-0002")
    _note(Scope.MATTER, "100-0001", "The buyer conceded the escrow in round 2.",
          matter="100-0001")
    _note(Scope.MATTER, "100-0002", "The landlord wants a five-year term.", matter="100-0002")
    memory.remember(MemoryEntry(
        scope=Scope.CLIENT, scope_key="100", kind=Kind.SUPPRESSION, confidence=1.0,
        statement='Asked not to be told about this again: "the Acme earn-out point"',
        owner=JIM, provenance={"source": "reply", "matter": "100-0001"}))
    _note(Scope.CLIENT, "200", "Globex always asks for a longer survival period.",
          matter="200-0001")
    _note(Scope.MATTER, "200-0001", "Globex accepted the cap at 1x.", matter="200-0001")
    _note(Scope.PERSONAL, JIM, "Uses 'will', not 'shall'.", kind=Kind.PREFERENCE)
    _note(Scope.PERSONAL, KATE, "Uses the Oxford comma.", kind=Kind.PREFERENCE)
    memory.record_outcome("job-a", JIM, "100-0001", "indemnity", "major", "Cap", "", None,
                          "rejected")
    memory.record_outcome("job-c", KATE, "100-0002", "term", "major", "Term", "", None,
                          "rejected")
    memory.record_outcome("job-g", JIM, "200-0001", "indemnity", "major", "Cap", "", None,
                          "rejected")
    return api


def negotiate(thread_id: str) -> None:
    from lra import negotiation

    negotiation.init()
    now = datetime.now(UTC).isoformat()
    with connect() as c:
        c.execute("INSERT INTO negotiation_points (thread_id, round, source, asked, created_at, "
                  "updated_at) VALUES (?, 1, 'issue', 'Cap the indemnity', ?, ?)",
                  (thread_id, now, now))
        c.execute("INSERT INTO negotiation_sent (thread_id, filename, content, created_at) "
                  "VALUES (?, 'sent.docx', ?, ?)", (thread_id, blobs.stash(b"sent", "doc"), now))


def store_id(scope: str, key: str) -> str | None:
    row = _one("SELECT store_id FROM memory_stores WHERE scope = ? AND scope_key = ?",
               (scope, key))
    return row[0] if row else None


def _one(sql, args=()):
    with connect() as c:
        return c.execute(sql, args).fetchone()


def _all(sql, args=()):
    with connect() as c:
        return [tuple(r) for r in c.execute(sql, args).fetchall()]


def ids(sql, args=()) -> set:
    return {r[0] for r in _all(sql, args)}


def snapshot() -> dict:
    tables = ["threads", "changes", "thread_aliases", "blackline_versions", "negotiation_points",
              "negotiation_sent", "closings", "closing_files", "messages", "attachments",
              "message_fts", "memory", "suggestion_outcomes", "memory_stores", "jobs",
              "audit_log", "access_log", "purges", "purge_items"]
    purge.init()
    return {t: sorted(map(repr, _all(f"SELECT * FROM {t}"))) for t in tables}


# --------------------------------------------------------------------------


def test_a_dry_run_lists_everything_and_touches_nothing(held):
    before = snapshot()
    stores_before = dict(held.stores.files)

    result = purge.run("client", "100")

    assert not result.applied
    assert snapshot() == before
    assert held.calls() == 0 and held.stores.files == stores_before
    p = result.plan
    assert p.matters == ["100-0001", "100-0002"]
    assert set(p.items["conversation"]) == {"t-acme", "t-acme-parties", "t-acme-2"}
    assert p.items["closing"] == ["close-acme"]
    assert len(p.items["version"]) == 1 and len(p.items["negotiation"]) == 2
    assert set(p.items["session"]) == {"sesn_a", "sesn_r", "sesn_b", "sesn_c"}
    assert p.items["file"] == ["file_a"]
    assert len(p.items["suppression"]) == 1 and len(p.items["note"]) == 4
    assert {i.split("|")[2] for i in p.items["store"]} == {
        store_id("client", "100"), store_id("matter", "100-0001"),
        store_id("matter", "100-0002")}
    assert p.unattributed == {"conversation": 1, "audit": 1}


def test_a_client_purge_deletes_every_kind_and_leaves_the_other_client(held, monkeypatch):
    monkeypatch.setattr("lra.purge._actor", lambda: "gc")
    globex_stores = {store_id("client", "200"), store_id("matter", "200-0001")}
    acme_stores = {store_id("client", "100"), store_id("matter", "100-0001"),
                   store_id("matter", "100-0002")}
    globex_audit = _one("SELECT * FROM audit_log WHERE job_id = 'job-g'")

    result = purge.run("client", "100", apply=True)

    assert result.done, result.failed
    # Our rows, and the documents they held.
    assert ids("SELECT id FROM threads") == {"t-globex", "t-old"}
    assert ids("SELECT DISTINCT thread_id FROM blackline_versions") == {"t-globex"}
    assert ids("SELECT DISTINCT thread_id FROM negotiation_points") == {"t-globex"}
    assert ids("SELECT DISTINCT thread_id FROM negotiation_sent") == {"t-globex"}
    assert ids("SELECT DISTINCT thread_id FROM thread_aliases") <= {"t-globex", "t-old"}
    assert ids("SELECT id FROM closings") == {"close-globex"}
    assert ids("SELECT closing_id FROM closing_files") == set()
    assert ids("SELECT matter_id FROM messages") == {"200-0001"}
    assert len(_all("SELECT * FROM attachments")) == 1
    assert len(_all("SELECT * FROM message_fts")) == 1
    # What was learned.
    assert ids("SELECT scope || ':' || scope_key FROM memory") == {
        "client:200", "matter:200-0001", f"personal:{JIM}", f"personal:{KATE}"}
    assert ids("SELECT matter_id FROM suggestion_outcomes") == {"200-0001"}
    assert ids("SELECT id FROM jobs") == {"job-g", "job-old"}
    # Anthropic's copies.
    assert set(held.deleted_stores) == acme_stores
    assert globex_stores <= set(held.stores.files)
    assert ids("SELECT store_id FROM memory_stores") & acme_stores == set()
    assert set(held.deleted_sessions) == {"sesn_a", "sesn_r", "sesn_b", "sesn_c"}
    assert set(held.deleted_files) == {"file_a", "sesn_a_out", "sesn_r_out", "sesn_b_out",
                                       "sesn_c_out"}
    assert "sesn_g" in held.sessions and {"file_g", "sesn_g_out"} <= held.files
    # The audit rows: kept, and blank where they named Acme's work.
    rows = {r["job_id"]: r for r in audit.rows("2000-01-01")}
    for job in ("job-a", "job-r", "job-b", "job-c", "job-close"):
        row = rows[job]
        assert row["documents"] == [] and row["session_ids"] == [] and row["file_ids"] == []
        assert not row["matter_id"] and not row["client_id"] and not row["thread_id"]
        assert row["purged"] == result.purge_id
        assert row["sender"] in (JIM, KATE) and row["received_at"]
    assert _one("SELECT * FROM audit_log WHERE job_id = 'job-g'") == globex_audit
    # The record of the purge itself.
    [record] = purge.history()
    assert record["scope"] == "client" and record["scope_key"] == "100"
    assert record["actor"] == "gc" and record["finished_at"]
    assert record["counts"]["conversation"] == 3 and record["counts"]["session"] == 4
    assert record["counts"]["audit"] == 5
    assert _all("SELECT * FROM purge_items") == []


def test_a_purge_run_again_finds_nothing_and_calls_nothing(held):
    assert purge.run("client", "100", apply=True).done
    calls = held.calls()
    again = purge.run("client", "100", apply=True)
    assert again.done and again.counts == {} and held.calls() == calls


def test_a_matter_purge_leaves_the_clients_other_matter(held):
    client_store = store_id("client", "100")
    matter_store = store_id("matter", "100-0001")

    result = purge.run("matter", "100-0001", apply=True)

    assert result.done, result.failed
    assert ids("SELECT id FROM threads") == {"t-acme-parties", "t-acme-2", "t-globex", "t-old"}
    assert ids("SELECT id FROM closings") == {"close-globex"}
    assert ids("SELECT matter_id FROM messages") == {"200-0001"}
    statements = ids("SELECT statement FROM memory")
    assert "Acme has never accepted an uncapped indemnity." not in statements
    assert "The buyer conceded the escrow in round 2." not in statements
    assert not any("earn-out" in s for s in statements)
    assert "Acme's counterparties concede on caps late." in statements
    assert "The landlord wants a five-year term." in statements
    assert ids("SELECT matter_id FROM suggestion_outcomes") == {"100-0002", "200-0001"}
    # The matter's store is deleted; the client's, which it shared, is
    # rewritten without what was learned on this matter.
    assert held.deleted_stores == [matter_store]
    assert client_store in held.stores.files
    projected = held.stores.files[client_store][memory.STORE_PATH]
    assert "uncapped" not in projected and "earn-out" not in projected
    assert "concede on caps late" in projected
    assert set(held.deleted_sessions) == {"sesn_a", "sesn_r"}
    assert {"sesn_b", "sesn_c", "sesn_g"} <= set(held.sessions)


def test_a_lawyer_purge_takes_their_personal_memory_and_leaves_colleagues(held, monkeypatch):
    revoked = []
    monkeypatch.setattr("lra.oauth.revoke", lambda who: revoked.append(who))
    kate_store = store_id("personal", KATE)
    jim_store = store_id("personal", JIM)

    result = purge.run("lawyer", JIM, apply=True)

    assert result.done, result.failed
    assert ids("SELECT owner FROM threads") == {KATE}
    assert ids("SELECT owner FROM closings") == set()
    assert ids("SELECT owner FROM messages") == set()
    assert ids("SELECT scope_key FROM memory WHERE scope = 'personal'") == {KATE}
    assert not any("earn-out" in s for s in ids("SELECT statement FROM memory"))
    assert "Acme has never accepted an uncapped indemnity." in ids("SELECT statement FROM memory")
    assert ids("SELECT user_address FROM suggestion_outcomes") == {KATE}
    assert jim_store in held.deleted_stores and kate_store in held.stores.files
    assert set(held.sessions) == {"sesn_c"}
    assert revoked == [JIM]
    # The client store held Jim's suppression: rewritten without it.
    assert "earn-out" not in held.stores.files[store_id("client", "100")][memory.STORE_PATH]


def test_a_failure_half_way_is_finished_by_running_it_again(held, capsys, monkeypatch):
    from lra import cli

    held.fail_once |= {"sesn_b", "file_a"}
    monkeypatch.setattr("sys.argv", ["lra", "purge", "--client", "100", "--apply"])

    assert cli.main() == 1
    out = capsys.readouterr().out
    assert "run the same command again" in out and "sesn_b" in out and "file_a" in out
    [record] = purge.history()
    assert record["finished_at"] is None
    # Everything else went, the audit rows included: the items remember
    # what is left, not the rows.
    assert ids("SELECT id FROM threads") == {"t-globex", "t-old"}
    assert "sesn_b" in held.sessions and "file_a" in held.files

    assert cli.main() == 0
    assert "Done." in capsys.readouterr().out
    [record] = purge.history()
    assert record["finished_at"] and record["counts"]["session"] == 4
    assert record["counts"]["file"] == 1
    assert "sesn_b" not in held.sessions and "file_a" not in held.files


def test_a_session_still_running_is_interrupted_and_left_for_the_next_run(held):
    held.sessions["sesn_b"] = "running"

    first = purge.run("client", "100", apply=True)

    assert [(k, i) for k, i, _ in first.failed] == [("session", "sesn_b")]
    assert held.interrupted == ["sesn_b"]
    assert purge.run("client", "100", apply=True).done
    assert "sesn_b" in held.deleted_sessions


def test_objects_already_gone_at_anthropic_count_as_deleted(held):
    held.sessions.clear()
    held.files.clear()
    for sid in list(held.stores.files):
        if sid != "memstore_firm":
            del held.stores.files[sid]
    assert purge.run("client", "100", apply=True).done


def test_without_an_api_key_our_rows_go_and_anthropics_wait(held, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    from lra.config import settings

    settings.cache_clear()
    result = purge.run("client", "100", apply=True)
    assert not result.done
    assert {k for k, _, _ in result.failed} <= set(purge.ANTHROPIC)
    assert "ANTHROPIC_API_KEY" in result.failed[0][2]
    assert ids("SELECT id FROM threads") == {"t-globex", "t-old"}


def test_the_command_line_is_a_dry_run_unless_told(held, capsys, monkeypatch):
    from lra import cli

    monkeypatch.setattr("sys.argv", ["lra", "purge", "--client", "100"])
    assert cli.main() == 0
    out = capsys.readouterr().out
    assert "Dry run for client 100 (Acme Corporation)" in out
    assert "sesn_a" in out and "file_a" in out and store_id("client", "100") in out
    assert "1 conversations" in out  # the one with no client or matter
    assert ids("SELECT id FROM threads") >= {"t-acme"}

    monkeypatch.setattr("sys.argv", ["lra", "purge", "--matter", "100-0002", "--apply",
                                     "--by", "gc@firm.com"])
    assert cli.main() == 0
    monkeypatch.setattr("sys.argv", ["lra", "purge", "--history"])
    assert cli.main() == 0
    out = capsys.readouterr().out
    assert "matter 100-0002  by gc@firm.com  finished" in out

    # The old form, an address, is a lawyer's purge, and a dry run too.
    monkeypatch.setattr("sys.argv", ["lra", "purge", KATE])
    assert cli.main() == 0
    assert "Dry run for lawyer kate@firm.com" in capsys.readouterr().out


# --- the links written so a purge can find things --------------------------------


def test_a_review_records_whose_it_is_where_a_purge_looks(monkeypatch, tmp_path):
    """Through the handler: a conversation known to be Acme's only from the
    counterparty on the email carries the client, and so does its audit row,
    with the session it ran."""
    from lra import handler
    from tests.test_handler import Captured, email_with_sample, stub_review

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/links.sqlite3")
    monkeypatch.setenv("INTERNAL_DOMAINS", "firm.com")
    from lra.config import settings

    settings.cache_clear()
    monkeypatch.setattr(handler, "get_provider", lambda: Captured())
    clients.register("100", "Acme Corporation", domains=["acme.com"])

    def review_with_a_session(doc, mode, instructions, **kw):
        audit.session("sesn_links")
        return stub_review()(doc, mode, instructions, **kw)

    monkeypatch.setattr(handler.review, "review", review_with_a_session)
    email = email_with_sample("Side letter")
    email.cc = ["counsel@acme.com"]
    handler.handle(email)

    [(thread_id, client)] = _all("SELECT id, client_id FROM threads")
    assert client == "100"
    [row] = audit.rows("2000-01-01")
    assert row["client_id"] == "100" and row["thread_id"] == thread_id
    assert purge.plan("client", "100").items["session"] == ["sesn_links"]
    settings.cache_clear()


def test_uploads_are_recorded_against_the_job(monkeypatch, tmp_path):
    from lra import managed

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/uploads.sqlite3")
    fs.configure(monkeypatch)
    fake = fs.FakeSessions([])
    token = audit.begin("job-up")
    try:
        managed.file_resources(fake.client, [("a.docx", b"a"), ("b.pdf", b"b")])
    finally:
        audit.end(token)
    [row] = [r for r in audit.rows("2000-01-01") if r["job_id"] == "job-up"]
    assert sorted(row["file_ids"]) == ["file_1", "file_2"]


def test_a_closing_records_its_matter_and_client(api):
    clients.register("100", "Acme Corporation", domains=["acme.com"])
    closing.create("c1", JIM, "Acme", matter_id="100-0001", client_id="100")
    assert _one("SELECT matter_id, client_id FROM closings WHERE id = 'c1'") == ("100-0001",
                                                                                 "100")


def test_the_purge_record_holds_no_contents(held):
    purge.run("client", "100", apply=True)
    [row] = _all("SELECT * FROM purges")
    text = json.dumps(row)
    for leak in ("Acme SPA", "indemnity", "sesn_", "t-acme", "memstore"):
        assert leak not in text


def test_the_documents_go_from_object_storage_too(held, _storage_backend):
    """On Cloudflare a row holds a reference and the document is in R2: the
    object goes before its row, and Globex's stay."""
    if _storage_backend is None:
        pytest.skip("object storage is the D1 backend's (LRA_TEST_BACKEND=d1)")

    def refs(sql) -> set[str]:
        return {bytes(r[0]).split(b":", 2)[2].decode() for r in _all(sql)
                if r[0] and bytes(r[0]).startswith(b"blobref:")}

    acme, globex = set(), set()
    for sql in ("SELECT original FROM threads WHERE id {}",
                "SELECT content FROM blackline_versions WHERE thread_id {}",
                "SELECT content FROM negotiation_sent WHERE thread_id {}"):
        acme |= refs(sql.format("IN ('t-acme', 't-acme-parties', 't-acme-2')"))
        globex |= refs(sql.format("= 't-globex'"))
    acme |= refs("SELECT blob FROM closing_files WHERE closing_id = 'close-acme'")
    attached = ("SELECT a.blob FROM attachments a JOIN messages m ON m.id = a.message_pk "
                "WHERE m.matter_id = ?")
    acme |= {bytes(r[0]).split(b":", 2)[2].decode() for r in _all(attached, ("100-0001",))}
    globex |= {bytes(r[0]).split(b":", 2)[2].decode() for r in _all(attached, ("200-0001",))}
    assert len(acme) == 7 and len(globex) == 4
    assert acme | globex <= set(_storage_backend.blobs)

    assert purge.run("client", "100", apply=True).done

    assert not acme & set(_storage_backend.blobs)
    assert globex <= set(_storage_backend.blobs)


def test_versions_left_for_the_sweep_are_not_the_purges_business(held):
    """blackline.sweep still removes versions orphaned by retention; the
    purge takes its scope's at once."""
    purge.run("client", "100", apply=True)
    blackline.sweep()
    assert ids("SELECT DISTINCT thread_id FROM blackline_versions") == {"t-globex"}
