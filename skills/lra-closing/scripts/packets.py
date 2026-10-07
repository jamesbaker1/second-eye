"""Build execution copies and one signature packet per signatory from a plan,
and a draft of the new closing state. See SKILL.md."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from _bootstrap import fail, finish, load_json


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True)
    parser.add_argument("--workspace", default="/workspace")
    parser.add_argument("--out", default="/mnt/session/outputs")
    parser.add_argument("--state", help="the current closing-state.json, if any")
    parser.add_argument("--draft", required=True, help="where to write the draft state")
    args = parser.parse_args()

    from lra.pipeline import closing_docs, closing_state

    plan = load_json(args.plan)
    try:
        built = closing_docs.packets(plan, Path(args.workspace), Path(args.out))
    except closing_docs.ClosingError as e:
        fail(str(e))
        return
    except Exception as e:  # noqa: BLE001
        fail(f"could not build the packets: {e}")
        return

    state = closing_state.empty(str(plan.get("closing") or ""))
    if args.state and Path(args.state).is_file():
        state = closing_state.normalise(load_json(args.state))
    state["name"] = str(plan.get("closing") or state.get("name") or "")
    state["return_to"] = str(plan.get("return") or state.get("return_to") or "")
    documents = {d["id"]: d for d in state["documents"]}
    for d in built["documents"]:
        documents[d["id"]] = d
    state["documents"] = list(documents.values())
    signatories = {s["id"]: s for s in state["signatories"]}
    for s in plan.get("signatories") or []:
        parties = [i.get("party") for i in s.get("sign") or []]
        signatories[s["id"]] = {"id": s["id"], "name": s["name"],
                                "party": s.get("party") or (parties[0] if parties else ""),
                                "person": s.get("person", ""),
                                "capacity": s.get("capacity", ""),
                                "email": s.get("email", "")}
    state["signatories"] = list(signatories.values())
    # A page already received on the same version is kept as it is; a page on
    # a document that is now a new version starts again.
    pages = {p["id"]: p for p in state["pages"]}
    for p in built["pages"]:
        old = pages.get(p["id"])
        if old and old.get("status") == "received" and old.get("ref") == p["ref"]:
            continue
        pages[p["id"]] = p
    state["pages"] = list(pages.values())
    state["executed"] = None
    Path(args.draft).write_text(json.dumps(state, indent=2, ensure_ascii=False))
    finish({"draft": args.draft, "method": built["method"],
            "packets": built["packets"], "documents": built["documents"],
            "pages": [{k: p[k] for k in ("id", "document", "signatory", "party", "page", "ref")}
                      for p in built["pages"]],
            "tally": closing_state.tally(state)})


if __name__ == "__main__":
    main()
