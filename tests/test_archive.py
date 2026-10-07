"""The archive.

This holds privileged client correspondence, so the tests that matter most are
the ones proving it stays off unless switched on, that one lawyer can never
read another's mail, and that a deletion path actually deletes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO

import pytest
from docx import Document

from lra import archive
from lra.models import Attachment, InboundEmail

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.fixture
def db(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/a.sqlite3")
    monkeypatch.setenv("ARCHIVE_ENABLED", "true")
    from lra.config import settings

    settings.cache_clear()
    yield
    settings.cache_clear()


def docx_bytes(text="A clause about indemnities.") -> bytes:
    d = Document()
    d.add_paragraph(text)
    buf = BytesIO()
    d.save(buf)
    return buf.getvalue()


def mail(mid="m1", subject="Acme NDA", body="Please review the indemnity cap.",
         attachments=None, to=None) -> InboundEmail:
    return InboundEmail(
        message_id=mid,
        thread_id="t1",
        from_address="jim@firm.com",
        to=to or ["review@legal.firm.com"],
        subject=subject,
        text_body=body,
        attachments=attachments or [],
        received_at=datetime.now(UTC),
    )


# --- the off switch -------------------------------------------------------

def test_nothing_is_stored_unless_the_firm_switched_it_on(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/off.sqlite3")
    monkeypatch.setenv("ARCHIVE_ENABLED", "false")
    from lra.config import settings

    settings.cache_clear()
    try:
        assert archive.store(mail(), owner="jim@firm.com", direction="received") == ""
        assert archive.search("jim@firm.com", "indemnity") == []
    finally:
        settings.cache_clear()


# --- storing and finding --------------------------------------------------

def test_a_message_can_be_found_by_its_body(db):
    archive.store(mail(), owner="jim@firm.com", direction="received")
    hits = archive.search("jim@firm.com", "indemnity")
    assert len(hits) == 1
    assert hits[0].subject == "Acme NDA"


def test_a_message_can_be_found_by_its_attachment_text(db):
    archive.store(mail(attachments=[
        Attachment(filename="NDA.docx", content_type=DOCX,
                   size_bytes=10, content=docx_bytes())
    ]), owner="jim@firm.com", direction="received",
        attachment_text={"NDA.docx": "a clause about escrow arrangements"})
    assert archive.search("jim@firm.com", "escrow")


def test_storing_the_same_message_twice_does_not_duplicate_it(db):
    archive.store(mail(), owner="jim@firm.com", direction="received")
    archive.store(mail(), owner="jim@firm.com", direction="received")
    assert len(archive.search("jim@firm.com", "indemnity")) == 1


def test_search_returns_nothing_for_an_unmatched_term(db):
    archive.store(mail(), owner="jim@firm.com", direction="received")
    assert archive.search("jim@firm.com", "bankruptcy") == []


# --- the wall between lawyers --------------------------------------------

def test_one_lawyer_cannot_find_another_lawyers_mail(db):
    archive.store(mail(), owner="jim@firm.com", direction="received")
    assert archive.search("someone.else@firm.com", "indemnity") == []


def test_matter_listing_is_scoped_to_the_owner(db):
    archive.store(mail(), owner="jim@firm.com", direction="received", matter_id="M1")
    assert archive.by_matter("jim@firm.com", "M1")
    assert archive.by_matter("someone.else@firm.com", "M1") == []


def test_a_thread_is_scoped_to_the_owner(db):
    archive.store(mail(), owner="jim@firm.com", direction="received")
    assert archive.thread("jim@firm.com", "t1")
    assert archive.thread("someone.else@firm.com", "t1") == []


# --- punctuation must not become query syntax ----------------------------

@pytest.mark.parametrize("query", [
    'indemnity"', "indemnity OR *", "cap (2026)", "-indemnity", "^cap", '"""'])
def test_a_lawyers_punctuation_cannot_break_the_search(db, query):
    archive.store(mail(), owner="jim@firm.com", direction="received")
    archive.search("jim@firm.com", query)  # must not raise


# --- previous versions, which is what makes diff review possible ----------

def test_the_last_version_of_a_document_can_be_retrieved(db):
    archive.store(mail(mid="m1", attachments=[
        Attachment(filename="Acme NDA.docx", content_type=DOCX,
                   size_bytes=10, content=docx_bytes("Version one."))
    ]), owner="jim@firm.com", direction="received")

    found = archive.latest_version("jim@firm.com", "Acme NDA.docx")
    assert found is not None
    assert b"PK" == found[0][:2]


def test_a_redline_matches_its_original_when_looking_for_prior_versions(db):
    """"Acme NDA (redline).docx" and "Acme NDA.docx" are the same document."""
    archive.store(mail(mid="m1", attachments=[
        Attachment(filename="Acme NDA.docx", content_type=DOCX,
                   size_bytes=10, content=docx_bytes())
    ]), owner="jim@firm.com", direction="received")
    assert archive.latest_version("jim@firm.com", "Acme NDA (redline).docx") is not None


def test_an_unknown_document_has_no_previous_version(db):
    assert archive.latest_version("jim@firm.com", "Never Seen.docx") is None


# --- counterparties -------------------------------------------------------

def test_counterparties_are_derived_from_outside_recipients(db):
    archive.store(mail(mid="m1"), owner="jim@firm.com", direction="sent",
                  external=["gc@acme.com"])
    names = [c[0] for c in archive.counterparties("jim@firm.com")]
    assert "acme.com" in names


def test_personal_mail_domains_are_not_treated_as_counterparties(db):
    archive.store(mail(mid="m2"), owner="jim@firm.com", direction="sent",
                  external=["someone@gmail.com"])
    assert archive.counterparties("jim@firm.com") == []


# --- deletion, which has to actually work ---------------------------------

def test_a_lawyers_archive_can_be_deleted_entirely(db):
    archive.store(mail(), owner="jim@firm.com", direction="received")
    assert archive.purge(owner="jim@firm.com") == 1
    assert archive.search("jim@firm.com", "indemnity") == []


def test_deleting_one_lawyers_archive_leaves_another_alone(db):
    archive.store(mail(mid="m1"), owner="jim@firm.com", direction="received")
    archive.store(mail(mid="m2"), owner="kate@firm.com", direction="received")
    archive.purge(owner="jim@firm.com")
    assert archive.search("kate@firm.com", "indemnity")


# --- the audit trail ------------------------------------------------------

def test_every_read_is_logged(db):
    archive.store(mail(), owner="jim@firm.com", direction="received")
    archive.search("jim@firm.com", "indemnity")
    from lra.store import connect

    with connect() as c:
        rows = c.execute(
            "SELECT action, detail FROM access_log WHERE owner = ?", ("jim@firm.com",)
        ).fetchall()
    assert any(r[0] == "search" and "indemnity" in (r[1] or "") for r in rows)


# Every public read, not just the one that was easy to remember. counterparties()
# was the function missing an access_log entry, and it went unnoticed because
# the test above exercises search() alone.
READS = [
    ("search", lambda o: archive.search(o, "indemnity")),
    ("thread", lambda o: archive.thread(o, "t1")),
    ("by_matter", lambda o: archive.by_matter(o, "ACME-1")),
    ("latest_version", lambda o: archive.latest_version(o, "nda.docx")),
    ("counterparties", lambda o: archive.counterparties(o)),
]


@pytest.mark.parametrize("action,call", READS, ids=[n for n, _ in READS])
def test_each_public_read_leaves_an_access_log_entry(db, action, call):
    archive.store(mail(), owner="jim@firm.com", direction="received")
    from lra.store import connect

    call("jim@firm.com")
    with connect() as c:
        actions = [
            r[0] for r in c.execute(
                "SELECT action FROM access_log WHERE owner = ?", ("jim@firm.com",)
            ).fetchall()
        ]
    assert action in actions


def test_the_audit_trail_test_covers_every_public_function_in_the_module():
    """A tripwire, so a new read cannot be added without a decision about it.

    The module docstring promises that every read is logged and
    docs/archive.md tells a general counsel the same thing. That promise is
    only as good as the list above staying complete.
    """
    import inspect

    public = {
        name for name, obj in vars(archive).items()
        if inspect.isfunction(obj)
        and not name.startswith("_")
        and obj.__module__ == "lra.archive"
    }
    writes_and_admin = {"store", "purge", "log_access", "init"}
    assert public - writes_and_admin - {name for name, _ in READS} == set()


# --- retention ------------------------------------------------------------

def test_an_expired_message_is_swept_when_the_next_one_is_archived(monkeypatch, tmp_path):
    """A retention policy is only real if something applies it.

    docs/archive.md tells a general counsel retention expiry is implemented,
    but purge() had no caller except the command line, so a firm that set
    ARCHIVE_RETENTION_DAYS kept privileged correspondence forever.
    """
    from datetime import timedelta

    from lra.config import settings
    from lra.store import connect

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/r.sqlite3")
    monkeypatch.setenv("ARCHIVE_ENABLED", "true")
    monkeypatch.setenv("ARCHIVE_RETENTION_DAYS", "30")
    settings.cache_clear()
    try:
        archive.store(mail(mid="old"), owner="jim@firm.com", direction="received")
        long_ago = (datetime.now(UTC) - timedelta(days=90)).isoformat()
        with connect() as c:
            c.execute("UPDATE messages SET sent_at = ?", (long_ago,))

        archive.store(mail(mid="new"), owner="jim@firm.com", direction="received")

        with connect() as c:
            kept = [r[0] for r in c.execute("SELECT message_id FROM messages").fetchall()]
        assert kept == ["new"]
    finally:
        settings.cache_clear()
