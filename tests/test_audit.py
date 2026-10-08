"""The audit trail: one content-free row per job, and the two ways to read it."""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, date, datetime

import pytest

from secondeye import audit, handler
from secondeye.models import InboundEmail
from tests.test_handler import Captured, email_with_sample, stub_review


@pytest.fixture
def captured(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/audit.sqlite3")
    monkeypatch.setenv("PLAYBOOK_ADMINS", "gc@firm.com")
    monkeypatch.setenv("INFERENCE_GEO", "us")
    from secondeye.config import settings

    settings.cache_clear()
    provider = Captured()
    monkeypatch.setattr(handler, "get_provider", lambda: provider)
    yield provider
    settings.cache_clear()


def reviewed(monkeypatch, subject="NDA for Acme", matter="") -> InboundEmail:
    def review_with_a_session(doc, mode, instructions, **kw):
        # What managed.run_session does on creating a session: no job id
        # passed down, only the one this work is for.
        audit.session("sesn_0123")
        return stub_review()(doc, mode, instructions, **kw)

    monkeypatch.setattr(handler.review, "review", review_with_a_session)
    email = email_with_sample(subject)
    if matter:
        email.text_body += f"\nMatter no. {matter}"
    email.received_at = datetime(2026, 9, 14, 10, 30, tzinfo=UTC)
    handler.handle(email)
    return email


def test_a_review_leaves_a_row_that_answers_the_committee(captured, monkeypatch):
    reviewed(monkeypatch, matter="100-0001")
    [row] = audit.rows("2026-09-01")
    assert row["received_at"].startswith("2026-09-14T10:30")
    assert row["sender"] == "jim@firm.com"
    assert row["matter_id"] == "100-0001"
    assert row["documents"] == ["Acme NDA.docx"]
    assert {"checks", "model review"} <= set(row["ran"])
    assert row["session_ids"] == ["sesn_0123"]
    from secondeye.config import settings

    assert row["model"] == settings().review_model
    assert row["inference_geo"] == "us"
    assert ("SQLite" in row["stored_in"] or "D1" in row["stored_in"]) and row["retention_until"]
    assert row["reply_to"] == ["jim@firm.com"]
    assert row["reply_sent_at"]
    assert row["outcome"] == "replied"


def test_the_row_holds_names_and_never_contents(captured, monkeypatch):
    reviewed(monkeypatch)
    dumped = audit.as_json(audit.rows("2026-09-01"))
    assert "[COUNTERPARTY NAME]" not in dumped, "document text"
    assert "Quick look before I send this" not in dumped, "email body"
    assert "uncapped" not in dumped, "a finding"


def test_rows_filter_by_period_lawyer_and_matter(captured, monkeypatch):
    reviewed(monkeypatch, "one", matter="100-0001")
    reviewed(monkeypatch, "two", matter="200-0001")
    assert len(audit.rows("2026-09-01")) == 2
    assert audit.rows("2026-10-01") == []
    assert len(audit.rows("2026-09-01", "2026-09-15")) == 2
    assert [r["matter_id"] for r in audit.rows("2026-09-01", matter="200-0001")] == ["200-0001"]
    assert audit.rows("2026-09-01", lawyer="kate@firm.com") == []


def test_the_command_line_prints_csv_and_json(captured, monkeypatch, capsys):
    from secondeye import cli

    reviewed(monkeypatch)
    monkeypatch.setattr("sys.argv", ["second-eye", "audit", "--since", "2026-09-01"])
    assert cli.main() == 0
    table = list(csv.DictReader(io.StringIO(capsys.readouterr().out)))
    assert table[0]["documents"] == "Acme NDA.docx"
    assert list(table[0]) == list(audit.COLUMNS)

    monkeypatch.setattr("sys.argv", ["second-eye", "audit", "--since", "2026-09-01",
                                     "--lawyer", "jim@firm.com", "--format", "json"])
    assert cli.main() == 0
    assert json.loads(capsys.readouterr().out)[0]["session_ids"] == ["sesn_0123"]


# --- by email ------------------------------------------------------------------

def ask(sender: str, text: str = "audit report for September") -> InboundEmail:
    return InboundEmail(message_id=f"m-audit-{sender}-{text}", from_address=sender,
                        to=["review@firm.com"], subject="Audit", text_body=text,
                        received_at=datetime.now(UTC))


def test_an_admin_gets_the_months_report_as_a_csv(captured, monkeypatch):
    reviewed(monkeypatch)
    captured.sent.clear()
    handler.handle(ask("gc@firm.com"))
    [out] = captured.sent
    assert out.to == ["gc@firm.com"] and not out.cc
    [attached] = out.attachments
    assert attached.content_type == "text/csv"
    assert attached.filename.endswith("2026-09-30.csv")
    table = list(csv.DictReader(io.StringIO(attached.content.decode())))
    assert [r["documents"] for r in table if r["documents"]] == ["Acme NDA.docx"]


def test_anyone_else_is_refused_and_gets_nothing(captured, monkeypatch):
    reviewed(monkeypatch)
    captured.sent.clear()
    handler.handle(ask("jim@firm.com"))
    [out] = captured.sent
    assert out.attachments == []
    assert "Only the firm's playbook admins" in out.text_body


def test_the_period_is_read_from_the_words(monkeypatch):
    def period(text):
        return audit.request(ask("gc@firm.com", text))

    assert period("audit report for September 2026") == (date(2026, 9, 1), date(2026, 10, 1))
    assert period("Audit log since 2026-09-15")[0] == date(2026, 9, 15)
    assert period("please send the audit report for february 2025") == (
        date(2025, 2, 1), date(2025, 3, 1))
    assert period("check this against our playbook") is None
