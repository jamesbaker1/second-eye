"""Where the documents themselves live.

A database row is the wrong place for a 20 MB share purchase agreement, and on
D1 it is not a possible one: a row tops out at 2 MB. So the bytes of a document
go to object storage (R2, through the Worker) and the row keeps a reference.

The two columns that hold documents, `threads.original` and
`attachments.blob`, keep their type. What goes in them is whatever `stash`
returns: locally, the bytes themselves, exactly as before, so a laptop needs no
object store and no test changed; on Cloudflare, a short marker naming the R2
object. `fetch` reads either. That is less pure than a new column and a
migration, and it means one code path serves both, which for the component
"undo" is rebuilt from is worth more.

Everything is sealed (crypto.py) before it leaves the process, whichever
backend it is going to.
"""

from __future__ import annotations

import logging
import uuid

from secondeye import crypto, edge

log = logging.getLogger(__name__)

_REF = b"blobref:v1:"


def remote() -> bool:
    from secondeye import store

    return store.is_d1()


def stash(data: bytes | None, kind: str = "doc") -> bytes | None:
    """Store a document; return what belongs in the database column."""
    if data is None:
        return None
    sealed = crypto.seal(bytes(data))
    if not remote():
        return sealed
    key = f"{kind}/{uuid.uuid4().hex}"
    edge.call(f"/internal/blob/{key}", content=sealed, method="PUT")
    return _REF + key.encode()


def fetch(stored: bytes | None) -> bytes:
    """The document a column refers to, or holds."""
    if not stored:
        return b""
    stored = bytes(stored)
    if stored.startswith(_REF):
        key = stored[len(_REF):].decode()
        response = edge.call(f"/internal/blob/{key}", method="GET")
        if response.status_code == 404:
            raise LookupError(f"stored document {key} is missing from object storage")
        stored = response.content
    return crypto.unseal(stored)


def delete(stored: bytes | None) -> None:
    """Delete the object a column refers to, and raise if it will not go.

    `discard`, for a caller that records its progress and is run again
    (purge.py): it deletes the row only once the object is gone, so a failure
    leaves the row to find next time instead of an orphan nobody can. An
    object already gone is deleted."""
    if not stored or not bytes(stored).startswith(_REF):
        return
    key = bytes(stored)[len(_REF):].decode()
    edge.call(f"/internal/blob/{key}", method="DELETE")


def discard(stored: bytes | None) -> None:
    """Delete the object a column refers to. The row is the caller's to delete.

    Never raises: this runs inside retention sweeps and purges, and an object
    that will not delete must not stop the rows being removed. It is logged,
    because an orphaned object is still a client document somewhere.
    """
    if not stored or not bytes(stored).startswith(_REF):
        return
    key = bytes(stored)[len(_REF):].decode()
    try:
        edge.call(f"/internal/blob/{key}", method="DELETE")
    except Exception:
        log.exception("could not delete stored document %s", key)
