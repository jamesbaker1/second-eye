"""A closing's state, and the rules an update to it must keep.

A closing is several documents, the people who sign them, one signature page
per signatory per document, and a checklist. The host keeps it (closing.py,
one row per thread); the closing agent is given it as a file, reads the
email's documents and scans against it, and writes the whole state back as
`closing.json` in its outputs. This module is the contract between the two,
and the same code runs on both sides: `scripts/validate_state.py` in the
sandbox refuses to write an update that breaks it, so the agent fixes its own
mistakes, and the host checks again before storing anything.

What is checked is bookkeeping, never judgment (DECISIONS 30). Whether a scan
is signed, which document and party it belongs to, and whether it is the
current version's page are the model's calls, made by reading the page. What
the rules hold it to is saying so consistently: a page recorded as received
must be marked signed, must carry the current version's reference, and must
come with the file; a page already received cannot quietly go back to
waiting unless its document changed; the executed set cannot exist while a
page is outstanding. A page the agent will not accept goes in `rejected`,
with a reason, so the lawyer is told.

No imports beyond the standard library: this is bundled into the skill.
"""

from __future__ import annotations

import copy
import hashlib
import re

STATE_SCHEMA = "lra-closing/1"
UPDATE_SCHEMA = "lra-closing-update/1"

PAGE_STATUSES = ("awaiting", "received")
CHECKLIST_KINDS = ("condition", "deliverable", "signing", "other")
CHECKLIST_STATUSES = ("open", "done", "waived", "not applicable")
MAX_MESSAGE_LINES = 12
MAX_LINE = 400


def empty(name: str = "") -> dict:
    return {"schema": STATE_SCHEMA, "name": name, "return_to": "", "documents": [],
            "signatories": [], "pages": [], "rejected": [], "checklist": [],
            "executed": None}


def text_ref(text: str) -> str:
    """A version reference from a document's words: the same words, the same
    reference, whatever the file's packaging. Printed on every page we issue,
    so a scan says which version it was signed on."""
    norm = re.sub(r"\s+", " ", text or "").strip().lower()
    return hashlib.sha256(norm.encode()).hexdigest()[:6].upper()


def page_id(document: str, signatory: str) -> str:
    return f"{document}--{signatory}"


# --------------------------------------------------------------------------
# Derived fields: set from the pages, never trusted from the update
# --------------------------------------------------------------------------


def normalise(state: dict) -> dict:
    """The state with every field that follows from the pages set from them:
    a signing item on the checklist is done exactly when its pages are in."""
    state = copy.deepcopy(state)
    for key, default in empty().items():
        state.setdefault(key, copy.deepcopy(default))
    received = {p.get("id") for p in state["pages"] if p.get("status") == "received"}
    for item in state["checklist"]:
        pages = item.get("pages") or []
        if item.get("kind") == "signing" and pages:
            item["status"] = "done" if all(p in received for p in pages) else "open"
    return state


# --------------------------------------------------------------------------
# The rules
# --------------------------------------------------------------------------


def _str(value) -> bool:
    return isinstance(value, str)


def _need(obj: dict, where: str, fields: dict[str, type | tuple], errors: list[str]) -> bool:
    ok = True
    for name, kind in fields.items():
        if name not in obj:
            errors.append(f"{where}: {name} is missing")
            ok = False
        elif not isinstance(obj[name], kind):
            errors.append(f"{where}: {name} has the wrong type")
            ok = False
    return ok


def validate_state(state: dict, previous: dict | None = None,
                   files: set[str] | None = None) -> list[str]:
    """Every way `state` breaks the rules, as sentences the agent can act on.
    `files` is what exists to be referred to: the session's outputs, plus
    whatever the host already holds for this closing."""
    errors: list[str] = []
    if not isinstance(state, dict):
        return ["the state is not a JSON object"]
    if state.get("schema") != STATE_SCHEMA:
        errors.append(f'schema must be "{STATE_SCHEMA}"')
    if not _need(state, "state", {"name": str, "documents": list, "signatories": list,
                                  "pages": list, "rejected": list, "checklist": list},
                 errors):
        return errors
    files = set(files or ())
    previous = previous or empty()

    docs: dict[str, dict] = {}
    for i, d in enumerate(state["documents"]):
        where = f"documents[{i}]"
        if not isinstance(d, dict) or not _need(d, where, {"id": str, "title": str,
                                                            "file": str, "ref": str}, errors):
            continue
        if d["id"] in docs:
            errors.append(f"{where}: id {d['id']!r} is used twice")
        docs[d["id"]] = d

    sigs: dict[str, dict] = {}
    for i, s in enumerate(state["signatories"]):
        where = f"signatories[{i}]"
        if not isinstance(s, dict) or not _need(s, where, {"id": str, "name": str,
                                                            "party": str}, errors):
            continue
        if s["id"] in sigs:
            errors.append(f"{where}: id {s['id']!r} is used twice")
        sigs[s["id"]] = s

    pages: dict[str, dict] = {}
    for i, p in enumerate(state["pages"]):
        where = f"pages[{i}]"
        if not isinstance(p, dict) or not _need(p, where, {"id": str, "document": str,
                                                            "signatory": str, "status": str,
                                                            "ref": str}, errors):
            continue
        where = f"page {p['id']}"
        if p["id"] in pages:
            errors.append(f"{where}: id is used twice")
        pages[p["id"]] = p
        doc = docs.get(p["document"])
        if doc is None:
            errors.append(f"{where}: document {p['document']!r} is not in documents")
        if p["signatory"] not in sigs:
            errors.append(f"{where}: signatory {p['signatory']!r} is not in signatories")
        if p["status"] not in PAGE_STATUSES:
            errors.append(f"{where}: status must be one of {', '.join(PAGE_STATUSES)}; "
                          "a page you will not accept goes in rejected")
        if doc is not None and p["ref"] != doc["ref"]:
            errors.append(f"{where}: ref {p['ref']} is not its document's current ref "
                          f"{doc['ref']}")
        if p.get("page") is not None and not (isinstance(p["page"], int) and p["page"] > 0):
            errors.append(f"{where}: page must be a positive page number or null")
        received = p.get("received")
        if p["status"] == "received":
            if not isinstance(received, dict):
                errors.append(f"{where}: received, so it needs a received record")
                continue
            if received.get("signed") is not True:
                errors.append(f"{where}: recorded as received but not marked signed; an "
                              "unsigned page goes in rejected")
            if doc is not None and received.get("version_ref") != doc["ref"]:
                errors.append(f"{where}: signed on version {received.get('version_ref')!r}, "
                              f"not the current {doc['ref']}; a stale page goes in rejected")
            if not _str(received.get("file")) or received.get("file") not in files:
                errors.append(f"{where}: its signed file {received.get('file')!r} was not "
                              "written (use scripts/receive.py)")
            elif not received["file"].lower().endswith(".pdf"):
                errors.append(f"{where}: the signed file must be a PDF")
            if not _str(received.get("source")) or not received.get("source"):
                errors.append(f"{where}: say which attachment and page it came from (source)")
        elif received not in (None, {}):
            errors.append(f"{where}: awaiting, so received must be null")

    for i, r in enumerate(state["rejected"]):
        if not isinstance(r, dict) or not r.get("source") or not r.get("reason"):
            errors.append(f"rejected[{i}]: needs a source and a reason")

    for i, item in enumerate(state["checklist"]):
        where = f"checklist[{i}]"
        if not isinstance(item, dict) or not _need(item, where, {"id": str, "item": str,
                                                                  "kind": str, "status": str},
                                                    errors):
            continue
        if item["kind"] not in CHECKLIST_KINDS:
            errors.append(f"{where}: kind must be one of {', '.join(CHECKLIST_KINDS)}")
        if item["status"] not in CHECKLIST_STATUSES:
            errors.append(f"{where}: status must be one of {', '.join(CHECKLIST_STATUSES)}")
        for pid in item.get("pages") or []:
            if pid not in pages:
                errors.append(f"{where}: page {pid!r} is not in pages")

    # Nothing received may be lost. A page goes back to waiting only because
    # its document is a new version.
    old_docs = {d.get("id"): d for d in previous.get("documents", []) if isinstance(d, dict)}
    for old in previous.get("pages", []):
        if not isinstance(old, dict) or old.get("status") != "received":
            continue
        new = pages.get(old.get("id"))
        doc_before = old_docs.get(old.get("document"), {})
        doc_now = docs.get(old.get("document"))
        new_version = doc_now is not None and doc_now.get("ref") != doc_before.get("ref")
        if new_version:
            continue
        if new is None:
            errors.append(f"page {old.get('id')}: was received and is now missing; a "
                          "received page stays unless its document is a new version")
        elif new.get("status") != "received":
            errors.append(f"page {old.get('id')}: was received and is now "
                          f"{new.get('status')}; it stays received unless its document "
                          "is a new version")

    executed = state.get("executed")
    if executed not in (None, {}):
        if not isinstance(executed, dict) or not isinstance(executed.get("files"), list):
            errors.append("executed: needs a list of files")
        else:
            waiting = [pid for pid, p in pages.items() if p.get("status") != "received"]
            if waiting:
                errors.append("executed: set while pages are outstanding: " + ", ".join(waiting))
            for name in executed["files"] + [executed.get("index") or ""]:
                if name and name not in files:
                    errors.append(f"executed: {name!r} was not written")
            if not executed.get("index"):
                errors.append("executed: the closing index is missing")
    return errors


def validate_update(update: dict, previous: dict | None, outputs: set[str],
                    held: set[str] | None = None) -> list[str]:
    """An update as the agent writes it: the new state, the reply's words and
    the files to attach. `outputs` is what this session wrote; `held` is what
    the host already has for this closing."""
    if not isinstance(update, dict):
        return ["closing.json is not a JSON object"]
    errors: list[str] = []
    if update.get("schema") != UPDATE_SCHEMA:
        errors.append(f'schema must be "{UPDATE_SCHEMA}"')
    message = update.get("message")
    if not isinstance(message, list) or not all(_str(m) for m in message):
        errors.append("message must be a list of lines")
    elif len(message) > MAX_MESSAGE_LINES:
        errors.append(f"message is over {MAX_MESSAGE_LINES} lines; keep it short")
    elif any(len(m) > MAX_LINE for m in message):
        errors.append(f"a message line is over {MAX_LINE} characters")
    attach = update.get("attach", [])
    if not isinstance(attach, list) or not all(_str(a) for a in attach):
        errors.append("attach must be a list of file names")
    else:
        for name in attach:
            if name not in outputs:
                errors.append(f"attach: {name!r} is not in the outputs")
    state = update.get("state")
    errors += validate_state(normalise(state) if isinstance(state, dict) else state,
                             previous, set(outputs) | set(held or ()))
    return errors


# --------------------------------------------------------------------------
# What the reply says first
# --------------------------------------------------------------------------


def _join(items: list[str]) -> str:
    return ", ".join(items)


def tally(state: dict) -> str:
    """"3 of 5 in. Waiting on: Beta Trading's director - SPA p.42, Disclosure
    Letter p.9." Counted from the state, so the numbers are never the
    model's."""
    pages = [p for p in state.get("pages", []) if isinstance(p, dict)]
    if not pages:
        return "No signature pages issued yet."
    docs = {d["id"]: d for d in state.get("documents", []) if isinstance(d, dict)}
    sigs = {s["id"]: s for s in state.get("signatories", []) if isinstance(s, dict)}
    done = sum(1 for p in pages if p.get("status") == "received")
    if done == len(pages):
        return f"All {len(pages)} in."
    waiting: dict[str, list[str]] = {}
    for p in pages:
        if p.get("status") == "received":
            continue
        doc = docs.get(p.get("document"), {})
        label = doc.get("short") or doc.get("title") or p.get("document", "?")
        if p.get("page"):
            label += f" p.{p['page']}"
        who = sigs.get(p.get("signatory"), {}).get("name") or p.get("signatory", "?")
        waiting.setdefault(who, []).append(label)
    parts = [f"{who} — {_join(labels)}" for who, labels in waiting.items()]
    return f"{done} of {len(pages)} in. Waiting on: " + "; ".join(parts) + "."


def checklist_line(state: dict) -> str:
    items = [i for i in state.get("checklist", []) if isinstance(i, dict)]
    if not items:
        return ""
    closed = sum(1 for i in items if i.get("status") in ("done", "waived", "not applicable"))
    return f"Checklist: {closed} of {len(items)} done."


__all__ = ["STATE_SCHEMA", "UPDATE_SCHEMA", "checklist_line", "empty", "normalise",
           "page_id", "tally", "text_ref", "validate_state", "validate_update"]
