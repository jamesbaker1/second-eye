"""Make the bundled `lra` package importable, wherever this skill is unpacked.

`lib/` is filled in by `lra skills sync` from the product's own source, so the
code here is never a fork of it. The container is not guaranteed to have
pydantic, and the modules that matter use it only to declare plain records, so
a minimal stand-in is used when the real one is missing.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

try:
    if os.environ.get("LRA_FORCE_SHIM"):
        raise ImportError("shim forced")
    import pydantic  # noqa: F401
except ImportError:
    sys.path.insert(0, str(ROOT / "shims"))


def finish(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    raise SystemExit(0)


def fail(message: str) -> None:
    print(json.dumps({"error": message}, ensure_ascii=False))
    raise SystemExit(1)


def read(path: str) -> bytes:
    try:
        return Path(path).read_bytes()
    except OSError as e:
        fail(f"could not read {path}: {e}")
        raise


def write(path: str, content: bytes) -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return str(target)
