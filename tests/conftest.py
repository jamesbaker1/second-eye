"""Run the whole suite against Cloudflare D1's shape as well as local SQLite.

    LRA_TEST_BACKEND=d1 pytest

With that set, every test's storage goes through `d1.Connection`, the JSON wire
protocol and object storage, via the fake Worker in fake_edge.py. Nothing in
any test changes: the point is that five hundred tests written against sqlite3
are the specification the D1 adapter has to meet, and this is how it is held to
it. CI runs both.
"""

from __future__ import annotations

import os

import pytest

from tests.fake_edge import FakeEdge

BACKEND = os.environ.get("LRA_TEST_BACKEND", "sqlite")

# The demos' invented addresses, whatever an operator's demo.env says
# (demos/identity.py): the suite is the same on every machine.
os.environ["DEMO_IDENTITY"] = "example"


def documents(out) -> list:
    """What a reply attached, less the contact card that rides on a lawyer's
    first review: the documents are what these tests are about."""
    return [a for a in out.attachments if a.content_type != "text/vcard"]


@pytest.fixture(autouse=True)
def _no_model_transcription(monkeypatch):
    """A scan is transcribed by a model call. The suite runs offline, and a
    developer's .env may hold a real key, so no test reaches the API by
    accident: a test that wants a transcription stubs `extract._transcribe` and
    turns availability back on itself (tests/test_transcribe.py)."""
    from lra.pipeline import extract

    monkeypatch.setattr(extract, "transcription_available", lambda: False)


@pytest.fixture(autouse=True)
def _no_model_triage(monkeypatch):
    """TRIAGE defaults to shadow, which is a model call on every email. The
    suite runs offline and some tests set a dummy key, so none reaches the
    API: a test that wants triage stubs `triage.call_model` and turns
    availability back on itself (tests/test_triage.py)."""
    from lra import triage

    monkeypatch.setattr(triage, "available", lambda: False)


@pytest.fixture
def learning(monkeypatch):
    """LEARN_FROM_OUTCOMES on. Off by default, nothing is learned unless a
    lawyer asks; the tests of what undos, dismissals and the sent version
    teach turn it on with this."""
    from lra.config import settings

    monkeypatch.setenv("LEARN_FROM_OUTCOMES", "true")
    settings.cache_clear()
    yield
    settings.cache_clear()


@pytest.fixture(autouse=True)
def _storage_backend(monkeypatch, tmp_path_factory):
    if BACKEND != "d1":
        yield None
        return

    from lra import d1, edge, store, thread

    fake = FakeEdge(str(tmp_path_factory.mktemp("d1") / "d1.sqlite3"))
    monkeypatch.setattr(store, "is_d1", lambda: True)
    monkeypatch.setattr(edge, "call", fake.call)
    # Per-process caches that a fresh database per test would otherwise defeat.
    d1._applied_scripts.clear()
    thread._migrated.clear()
    yield fake
    d1._applied_scripts.clear()
    thread._migrated.clear()
