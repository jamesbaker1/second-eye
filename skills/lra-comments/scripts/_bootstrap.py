"""Make the bundled `secondeye` package importable, wherever this skill is unpacked.

The same arrangement as lra-document-tools: `lib/` and `shims/` are filled in
by `second-eye skills sync` from the product's own source, so nothing here is a fork
of it. Also the helpers every script shares: JSON out, the working file in
and out, the author, and the decisions journal.

The journal records which of the other side's tracked changes were accepted
and which rejected. It sits beside the working file (`WORK.docx.decisions.json`)
and follows it from script to script, so finish.py can check that every change
that disappeared was decided by accept_reject.py and nothing else.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

try:
    if os.environ.get("SECOND_EYE_FORCE_SHIM"):
        raise ImportError("shim forced")
    import pydantic  # noqa: F401
except ImportError:
    sys.path.insert(0, str(ROOT / "shims"))

DEFAULT_AUTHOR = "Reviewer"


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


def author(value: str | None) -> str:
    return (value or os.environ.get("SECOND_EYE_AUTHOR") or DEFAULT_AUTHOR).strip()


def journal_path(docx: str) -> Path:
    return Path(f"{docx}.decisions.json")


def journal(docx: str) -> dict:
    try:
        data = json.loads(journal_path(docx).read_text())
        return {"accepted": list(data.get("accepted", [])),
                "rejected": list(data.get("rejected", []))}
    except (OSError, ValueError):
        return {"accepted": [], "rejected": []}


def carry(source: str, target: str, add: dict | None = None) -> None:
    """The journal of `source`, plus `add`, becomes the journal of `target`."""
    decided = journal(source)
    for key, ids in (add or {}).items():
        decided[key] = decided.get(key, []) + [i for i in ids if i not in decided.get(key, [])]
    if Path(source).resolve() == Path(target).resolve() and not add:
        return
    if decided["accepted"] or decided["rejected"]:
        journal_path(target).write_text(json.dumps(decided))
