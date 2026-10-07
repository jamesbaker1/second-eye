"""Render the closing checklist in a state file as a PDF. See SKILL.md."""

from __future__ import annotations

import argparse

from _bootstrap import fail, finish, load_json, write


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    from lra.pipeline import closing_docs

    state = load_json(args.state)
    if not state.get("checklist"):
        fail("the state has no checklist items")
    try:
        pdf = closing_docs.checklist_pdf(state)
    except Exception as e:  # noqa: BLE001
        fail(f"could not render the checklist: {e}")
        return
    finish({"file": write(args.out, pdf), "items": len(state["checklist"])})


if __name__ == "__main__":
    main()
