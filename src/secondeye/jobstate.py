"""A review's state between two Workflow steps, sealed in object storage.

In handler.handle() a review lives in one process from start to finish, and
its state is local variables. On the Workflow path each stage is a
separate container call, and the container may have been replaced in between,
so what one stage leaves for the next is written down: sealed under the data
key (crypto.py), like every document, and stored in R2 through the Worker
under `job/<32 hex>`. The key is derived from the job id (the Workflow
instance id) and the part's name, so the Worker can be handed a key to read
and the container never has to remember one.

Parts: "state" (the review, see flow.py), and the replies the Worker sends,
"outbound", "fallback" and "failure", each already in the shape
`/internal/send` takes.

Locally (no D1) the parts live in this process's memory, which is enough for
tests and a laptop, and deliberately nothing more.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime
from enum import Enum

from secondeye import crypto, edge

PARTS = ("state", "outbound", "fallback", "failure")

_local: dict[str, bytes] = {}


def _remote() -> bool:
    from secondeye import store

    return store.is_d1()


def key(job_id: str, part: str = "state") -> str:
    """Where a part lives. The shape the Worker's blob endpoint accepts."""
    digest = hashlib.sha256(f"{job_id}:{part}".encode()).hexdigest()
    return f"job/{digest[:32]}"


def put(job_id: str, part: str, data: bytes) -> str:
    """Seal and store one part; returns its key."""
    where = key(job_id, part)
    sealed = crypto.seal(data)
    if _remote():
        edge.call(f"/internal/blob/{where}", content=sealed, method="PUT")
    else:
        _local[where] = sealed
    return where


def get(job_id: str, part: str) -> bytes | None:
    """One part, unsealed, or None when it was never stored or has gone."""
    where = key(job_id, part)
    if _remote():
        response = edge.call(f"/internal/blob/{where}", method="GET")
        if response.status_code == 404:
            return None
        stored = response.content
    else:
        stored = _local.get(where)
        if stored is None:
            return None
    return crypto.unseal(stored)


def save(job_id: str, state: dict) -> None:
    put(job_id, "state", dumps(state).encode())


def load(job_id: str) -> dict | None:
    data = get(job_id, "state")
    return loads(data.decode()) if data is not None else None


def discard(job_id: str) -> None:
    """Delete every part. The review is over; nothing of it is kept here."""
    for part in PARTS:
        where = key(job_id, part)
        if _remote():
            edge.call(f"/internal/blob/{where}", method="DELETE")
        else:
            _local.pop(where, None)


# -- JSON that carries bytes -------------------------------------------------


def _plain(value):
    if isinstance(value, bytes | bytearray | memoryview):
        return {"$b64": base64.b64encode(bytes(value)).decode()}
    if isinstance(value, Enum):
        return _plain(value.value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    return value


def _rich(value):
    if isinstance(value, dict):
        if set(value) == {"$b64"}:
            return base64.b64decode(value["$b64"])
        return {k: _rich(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_rich(v) for v in value]
    return value


def dumps(value) -> str:
    """JSON, with bytes as {"$b64": ...}, enums as their values and datetimes
    as ISO strings. Pydantic and the dataclasses read all three back."""
    return json.dumps(_plain(value), separators=(",", ":"))


def loads(text: str):
    return _rich(json.loads(text))
