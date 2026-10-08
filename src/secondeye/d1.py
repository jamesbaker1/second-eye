"""Cloudflare D1, wearing the shape of a sqlite3 connection.

Every module that stores anything does it the same way:

    with connect() as c:
        row = c.execute("SELECT ...", params).fetchone()

Forty-odd call sites, five files, and a small vocabulary: execute,
executescript, fetchone, fetchall, rowcount, lastrowid, and rows read by index
or by name. D1 is SQLite, so the SQL itself does not change, FTS5 included.
Rather than teach five modules a second storage API, this module speaks the
first one: `store.connect()` hands back one of these when DATABASE_URL is
`d1://`, and nothing above it knows.

What does change, and has to be said plainly:

**A `with` block is no longer one transaction.** Each statement reaches D1 on
its own and commits on its own. Locally, a crash half way through a block rolls
the whole block back; here it leaves the first half. The blocks that make
several writes were read with that in mind (the list is in
docs/deploy-cloudflare.md): each is either idempotent on retry or writes its
parent row last, so a partial write is invisible rather than wrong. The one
operation that must be atomic, claiming a job, is a single statement and always
was.

**Schema scripts run once per process.** `init()` is called at the top of
nearly every storage function, which is free against a local file and a network
round trip here. A script this process has already applied is skipped.
"""

from __future__ import annotations

import base64
import hashlib
import re
import sqlite3
from typing import Self

from secondeye import edge


class D1Error(sqlite3.OperationalError):
    """Subclasses the sqlite3 error so existing handlers keep working."""


class Row(tuple):
    """A result row readable as `row[0]` and as `row["name"]`, like sqlite3.Row."""

    _columns: tuple[str, ...] = ()

    def __new__(cls, values, columns):
        row = super().__new__(cls, values)
        row._columns = tuple(columns)
        return row

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                return super().__getitem__(self._columns.index(key))
            except ValueError:
                raise IndexError(f"no such column: {key}") from None
        return super().__getitem__(key)

    def keys(self) -> list[str]:
        return list(self._columns)


class Cursor:
    def __init__(self, result: dict) -> None:
        columns = result.get("columns") or []
        self._rows = [Row([_decode(v) for v in values], columns)
                      for values in result.get("rows") or []]
        self.rowcount: int = int(result.get("changes") or 0)
        self.lastrowid: int | None = result.get("last_row_id")
        self._next = 0

    def fetchone(self):
        if self._next >= len(self._rows):
            return None
        row = self._rows[self._next]
        self._next += 1
        return row

    def fetchall(self) -> list:
        rest = self._rows[self._next:]
        self._next = len(self._rows)
        return rest

    def __iter__(self):
        return iter(self.fetchall())


_applied_scripts: set[str] = set()


class Connection:
    # Accepted and ignored: rows are always readable both ways.
    row_factory = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def execute(self, sql: str, params=()) -> Cursor:
        return Cursor(_query([{"sql": sql, "params": [_encode(p) for p in params]}])[0])

    def executescript(self, script: str) -> None:
        digest = hashlib.sha256(script.encode()).hexdigest()
        if digest in _applied_scripts:
            return
        statements = _split(script)
        if statements:
            _query([{"sql": s, "params": []} for s in statements])
        _applied_scripts.add(digest)

    def commit(self) -> None:
        """Every statement has already committed."""

    def close(self) -> None:
        pass


def connect() -> Connection:
    return Connection()


def _query(statements: list[dict]) -> list[dict]:
    try:
        payload = edge.call("/internal/db", json={"statements": statements}).json()
    except edge.EdgeError as e:
        raise D1Error(str(e)) from e
    return payload["results"]


def _split(script: str) -> list[str]:
    """A schema script as separate statements. D1 prepares one at a time.

    Comments go first, because ours contain semicolons. Nothing here handles a
    semicolon inside a string literal or a trigger body; the schemas have
    neither, and a test asserts every script in the codebase splits into
    statements SQLite itself accepts.
    """
    without_comments = re.sub(r"--[^\n]*", "", script)
    return [s.strip() for s in without_comments.split(";") if s.strip()]


def _encode(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$bytes": base64.b64encode(bytes(value)).decode()}
    return value


def _decode(value):
    if isinstance(value, dict) and "$bytes" in value:
        return base64.b64decode(value["$bytes"])
    return value
