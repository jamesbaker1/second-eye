"""A stand-in for the Cloudflare Worker, faithful to its wire protocol.

`cloudflare/src/index.ts` exposes three endpoints to the application. This
answers the same three, the same way, with SQLite where the Worker has D1, a
dict where it has R2, and a list where it has the email service. Everything
crosses a JSON boundary exactly as it would over HTTP, so an encoding mistake
fails here rather than in production.

D1 is SQLite, so backing it with SQLite is not a mock of the database: the SQL
runs for real. What this cannot show is whether the TypeScript agrees with it,
which is what the contract test in test_cloudflare.py is for.
"""

from __future__ import annotations

import base64
import json
import re
import sqlite3
from types import SimpleNamespace


class FakeEdge:
    def __init__(self, path: str) -> None:
        self.path = path
        self.blobs: dict[str, bytes] = {}
        self.sent: list[dict] = []
        self.db_requests = 0
        # outbox key -> message id, as the Worker's outbox table (outbox.ts).
        self.outbox: dict[str, str] = {}

    # -- the three endpoints ------------------------------------------------

    def call(self, path: str, *, json: dict | None = None, content: bytes | None = None,
             method: str = "POST"):
        if path == "/internal/db":
            return self._respond(self._db(_roundtrip(json)))
        if path == "/internal/send":
            body = _roundtrip(json)
            key = body.pop("outbox", None)
            if key and key in self.outbox:
                return self._respond({"messageId": self.outbox[key], "duplicate": True})
            self.sent.append(body)
            message_id = f"cf-{len(self.sent)}"
            if key:
                self.outbox[key] = message_id
            return self._respond({"messageId": message_id})
        if path.startswith("/internal/blob/"):
            return self._blob(path[len("/internal/blob/"):], method, content)
        raise AssertionError(f"the Worker has no endpoint {path}")

    def _db(self, body: dict) -> dict:
        self.db_requests += 1
        conn = sqlite3.connect(self.path)
        results = []
        try:
            # One request is one D1 batch, and a batch is one transaction.
            for statement in body["statements"]:
                params = [_decode(p) for p in statement["params"]]
                cur = conn.execute(statement["sql"], params)
                columns = [d[0] for d in cur.description] if cur.description else []
                rows = [[_encode(v) for v in row] for row in cur.fetchall()] if columns else []
                results.append({
                    "columns": columns if rows else [],
                    "rows": rows,
                    "changes": max(cur.rowcount, 0),
                    "last_row_id": cur.lastrowid,
                })
            conn.commit()
        finally:
            conn.close()
        return {"results": results}

    def _blob(self, key: str, method: str, content: bytes | None):
        assert re.fullmatch(r"(thread|archive|doc|job)/[0-9a-f]{32}", key), key
        if method == "PUT":
            self.blobs[key] = bytes(content or b"")
            return self._respond({"stored": key})
        if method == "GET":
            if key not in self.blobs:
                return SimpleNamespace(status_code=404, content=b"", json=dict)
            return SimpleNamespace(status_code=200, content=self.blobs[key], json=dict)
        if method == "DELETE":
            self.blobs.pop(key, None)
            return self._respond({"deleted": key})
        raise AssertionError(method)

    @staticmethod
    def _respond(payload: dict):
        return SimpleNamespace(status_code=200, content=b"", json=lambda: payload)


def _roundtrip(payload):
    return json.loads(json.dumps(payload))


def _decode(value):
    if isinstance(value, dict) and "$bytes" in value:
        return base64.b64decode(value["$bytes"])
    return value


def _encode(value):
    if isinstance(value, bytes):
        return {"$bytes": base64.b64encode(value).decode()}
    return value
