"""Encryption of what we store, under a key the firm holds.

Cloudflare encrypts D1 and R2 at rest, and that protects against someone
walking off with a disk. It does not protect against the things a firm's
general counsel actually asks about: a misconfigured bucket, a leaked API
token, a subpoena served on the host rather than on the firm. For those the
data has to be unreadable to anyone holding the storage but not the key, which
means encrypting before it leaves this process.

So documents and OAuth tokens are sealed here with AES-256-GCM, and the key
arrives as a secret the firm generates and can rotate or withdraw. Withdrawing
it is a real off switch: everything stored becomes noise.

What is NOT sealed, and why, so nobody assumes otherwise: the text the archive
indexes for search. Full-text search needs plaintext in the index, and an index
over ciphertext finds nothing. The archive is off by default for that reason
among others (docs/archive.md).

With no DATA_KEY set, nothing is encrypted and everything still works. That is
the right behaviour on a laptop and the wrong one on a server, so
`require_key()` is called at startup wherever DATABASE_URL points at D1.

With a key set, everything read back must be sealed. Plaintext used to pass
through unchanged "so data written before a key existed stays readable", which
also meant anyone able to write to the bucket or the table could plant a
document or an OAuth token and have it read as ours. There is no plaintext to
migrate: D1 and R2 have required the key since the first deploy (the service
refuses to start on D1 without it, and the Worker seals under the same key),
so the only rows written without one are on laptops that ran keyless, where
the fix is to go on running keyless or re-send and re-consent.

Associated data is not bound: the envelope is shared with the Worker's and
every row already sealed, so binding a row's identity into the tag is a new
envelope version and a re-seal of what is stored, not a change here.
"""

from __future__ import annotations

import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from secondeye.config import settings

PREFIX = b"enc:v1:"
_TEXT_PREFIX = "enc:v1:"


class KeyMissing(RuntimeError):
    """Sealed data, and no key that opens it."""


class NotSealed(KeyMissing):
    """A key is configured and the stored value is plaintext. Refused, because
    whoever can write the storage but does not hold the key could have put it
    there. A KeyMissing, so every caller that already treats a key problem as
    fatal treats this as fatal too."""


def _decode(value: str) -> bytes | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        key = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as e:
        raise KeyMissing("DATA_KEY is not valid base64") from e
    if len(key) != 32:
        raise KeyMissing("DATA_KEY must decode to exactly 32 bytes (AES-256)")
    return key


def _keys() -> list[bytes]:
    """The current key first, then any it replaced. Only the first one seals."""
    cfg = settings()
    return [k for k in (_decode(cfg.data_key), _decode(cfg.data_key_previous)) if k]


def enabled() -> bool:
    return bool(_keys())


def require_key() -> None:
    if not enabled():
        raise KeyMissing(
            "DATA_KEY is not set. Refusing to store client documents on a hosted "
            "database without it. Generate one with `second-eye keygen`."
        )


def generate() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def seal(data: bytes) -> bytes:
    """Encrypt, if there is a key. Plain bytes pass through unchanged otherwise."""
    keys = _keys()
    if not keys:
        return data
    nonce = os.urandom(12)
    return PREFIX + nonce + AESGCM(keys[0]).encrypt(nonce, data, None)


def unseal(stored: bytes) -> bytes:
    """Decrypt what `seal` produced. With no key configured, plaintext is
    returned as it is (a laptop). With a key, anything unsealed is refused."""
    if not stored:
        return stored
    if not bytes(stored).startswith(PREFIX):
        if enabled():
            raise NotSealed("a key is configured and the stored value is not sealed")
        return stored
    body = bytes(stored)[len(PREFIX):]
    nonce, ciphertext = body[:12], body[12:]
    for key in _keys():
        try:
            return AESGCM(key).decrypt(nonce, ciphertext, None)
        except InvalidTag:
            pass        # sealed under the other key; try that one
    raise KeyMissing("stored data is encrypted and no configured key opens it")


def seal_text(value: str) -> str:
    """The same, for a value that lives in a TEXT column."""
    if not value or not enabled():
        return value
    return _TEXT_PREFIX + base64.urlsafe_b64encode(
        seal(value.encode("utf-8"))[len(PREFIX):]).decode()


def unseal_text(stored: str) -> str:
    if not stored:
        return stored
    if not stored.startswith(_TEXT_PREFIX):
        if enabled():
            raise NotSealed("a key is configured and the stored value is not sealed")
        return stored
    raw = base64.urlsafe_b64decode(stored[len(_TEXT_PREFIX):])
    return unseal(PREFIX + raw).decode("utf-8")
