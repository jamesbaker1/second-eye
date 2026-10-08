"""The model's side of the story, scripted, for `rehearse --stub`.

Everything the handler does is real: intake, routing, the deterministic
checks, the thread and the ledger, the negotiation evidence, the issues
list, the writer, the clean copy, the signature pack, the closing state and
its validator, the calendar. What is scripted is exactly what a model would
decide, step by step, tuned to show what each beat is meant to show:

- review.review: the findings a review session would report, and on v2 the
  negotiation.json the lra-negotiation skill would record, built from the
  evidence the host actually handed the session and checked by the host
  exactly as a live one is;
- playbook.read_positions: the positions the playbook agent would record,
  every quote word for word from the memo and the precedents, so the host's
  quote check passes on its merits;
- comments.turn: the edits, replies and resolutions the comments agent would
  make, written with our own writer and wordcomments, and examined by
  turning.examine as a live session's file is;
- managed.run_session for the closing agent: the plan and page reads a model
  would make, with the lra-closing tools (pipeline/closing_docs.py) doing the
  work and closing_state validating the update, as in the sandbox.

Nothing reaches the network: the Anthropic client is replaced by one that
raises, so an unscripted model call fails loudly instead of spending money.
"""

from __future__ import annotations

import json
import re
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from demos.falcon import cast, documents, signing

AGENT_IDS = {
    "MANAGED_REVIEW_AGENT_ID": "agent_stub_review",
    "MANAGED_ASSOCIATE_AGENT_ID": "agent_stub_associate",
    "MANAGED_ENVIRONMENT_ID": "env_stub",
    "MANAGED_PLAYBOOK_AGENT_ID": "agent_stub_playbook",
    "MANAGED_COMMENTS_AGENT_ID": "agent_stub_comments",
    "MANAGED_CLOSING_AGENT_ID": "agent_stub_closing",
    "SANDBOX_SKILL_ID": "skill_stub_tools",
    "SANDBOX_PLAYBOOK_SKILL_ID": "skill_stub_playbook",
    "SANDBOX_COMMENTS_SKILL_ID": "skill_stub_comments",
    "SANDBOX_CLOSING_SKILL_ID": "skill_stub_closing",
    "SANDBOX_NEGOTIATION_SKILL_ID": "skill_stub_negotiation",
}


class NoNetwork(RuntimeError):
    pass


def _no_client(*_a, **_k):
    raise NoNetwork("rehearse --stub: a model call was not scripted and was refused")


# --------------------------------------------------------------------------
# What the step is
# --------------------------------------------------------------------------


class Script:
    """Which step is running; set by the rehearsal before each email."""

    def __init__(self) -> None:
        self.step = ""
        self.calls: list[dict] = []       # what each scripted session was given


# --------------------------------------------------------------------------
# Reviews
# --------------------------------------------------------------------------

_MEMO = {p["clause"]: p for p in cast.POSITIONS}

# Our seven points on their v1: (clause, topic, what it is in their draft,
# their words, the memo position, our response).
POINTS = [
    ("3.2", "Deposit", "non-refundable in all circumstances",
     "which shall be non-refundable in all circumstances", "Deposit",
     ("Refundable in full if Completion does not occur, save where the Buyer is in "
      "material breach.")),
    ("3.3", "Price adjustment", "one way only",
     "and shall not be increased in any circumstances",
     "Purchase price and completion accounts",
     "The Net Working Capital adjustment works pound for pound in both directions."),
    ("7.3", "Escrow", "released after three months",
     "released to the Seller three months after Completion", "Escrow and retention",
     "Hold the Escrow Amount for six months after Completion."),
    ("9.3", "Liability cap", "15 per cent. of the price",
     "shall not exceed 15 per cent. of the Purchase Price", "Limitation of liability",
     ("Cap warranty claims at 50 per cent. of the Purchase Price, and title and capacity "
      "at 100 per cent.")),
    ("10.1", "Tax covenant", "none given",
     "The Seller gives no covenant in respect of Tax", "Tax covenant",
     "A full covenant for pre-Completion Tax, pound for pound."),
    ("11.1", "Non-compete", "12 months",
     "for a period of 12 months after", "Restrictive covenants",
     "24 months, with customers and senior employees covered for the same period."),
    ("12.1", "Warranty claims period", "12 months",
     "before the date falling 12 months after", "Warranty claims periods",
     "24 months for general Warranty Claims, and seven years for Tax."),
]
ACCEPTED_IN_V2 = {"3.2", "3.3", "7.3", "10.1", "11.1"}
REJECTED_IN_V2 = {"9.3": ("the cap", True), "12.1": ("the claims period", False)}


def _finding(**kw):
    from secondeye.models import Finding, Severity

    kw.setdefault("severity", Severity.SUBSTANTIVE)
    kw.setdefault("category", "playbook")
    kw.setdefault("confidence", 0.9)
    return Finding(**kw)


def _points_v1() -> list:
    out = []
    for clause, topic, short, anchor, memo, response in POINTS:
        position = _MEMO[memo]["position"]
        out.append(_finding(
            title=f"Off playbook: {topic} - {short}", where=f"clause {clause}",
            explanation=f"The firm's playbook: {position}", anchor=anchor,
            our_position=position, response=response))
    return out


def _typos() -> list:
    """The spelling slips a reader finds; the repeated words are the
    mechanical checks' and are not reported twice."""
    from secondeye.models import Severity

    right = {"accordence": "accordance", "recieve": "receive", "reasonabl": "reasonable",
             "occured": "occurred", "seperate": "separate", "immediatly": "immediately",
             "liabilty": "liability", "amout": "amount", "garantees": "guarantees",
             "anouncement": "announcement", "responsable": "responsible",
             "goverened": "governed"}
    return [_finding(severity=Severity.STYLE, category="typo",
                     title=f"Typo in clause {clause}: “{wrong}”",
                     explanation=f"Should read “{right[wrong]}”.", anchor=wrong,
                     suggested_text=right[wrong], where=f"clause {clause}")
            for clause, wrong in documents.TYPOS if wrong in right]


def _clause_of(text: str) -> str:
    """"3.2" from "Clause 3.2: Deposit" or "3.2"; "Schedule 2" from "under
    SCHEDULE 2"."""
    m = re.search(r"schedule\s+(\d+)", text or "", re.IGNORECASE)
    if m:
        return f"Schedule {m.group(1)}"
    m = re.search(r"(\d+(?:\.\d+)*)", text or "")
    return m.group(1) if m else ""


def negotiation_for(evidence: bytes) -> dict:
    """What the negotiation agent records for v2, decided per clause as the
    story has it and cited from the evidence the host really built."""
    ev = json.loads(evidence)
    changes = ev["changes"]

    def at(clause: str) -> list[int]:
        return [c["index"] for c in changes if _clause_of(c["where"]) == clause]

    points, cited = [], set()
    rejected_ids: list[str] = []
    for p in ev["points"]:
        clause = _clause_of(p["clause"])
        if clause in ACCEPTED_IN_V2:
            shown = at(clause) + (at("Schedule 2") if clause == "3.3" else [])
            cited.update(shown)
            points.append({"id": p["id"], "status": "accepted" if shown else "rejected",
                           "label": p["clause"].split(": ")[-1].lower(),
                           "mentioned": False, "changes": shown,
                           "evidence": (f"Clause {clause} now reads as we asked." if shown
                                        else f"Clause {clause} is unchanged.")})
        else:
            label, mentioned = REJECTED_IN_V2.get(clause, (p["clause"].lower(), False))
            rejected_ids.append(p["id"])
            points.append({"id": p["id"], "status": "rejected", "label": label,
                           "mentioned": mentioned, "changes": [],
                           "evidence": f"Clause {clause} is unchanged from v1."})
    summaries = {"9.4": ("deletes the fraud carve-out in 9.4", "high"),
                 "4.4": ("moves the long-stop date from 31 March to 31 January", "high")}
    unrequested, grouped = [], {}
    for c in changes:
        if c["index"] not in cited:
            grouped.setdefault(_clause_of(c["where"]) or c["where"], []).append(c["index"])
    for clause, indices in grouped.items():
        summary, risk = summaries.get(clause, (f"changes {clause}", "low"))
        unrequested.append({"clause": clause, "summary": summary, "risk": risk,
                            "mentioned": False, "changes": indices})
    silent = [pid for pid in rejected_ids
              if not next(p for p in points if p["id"] == pid)["mentioned"]]
    cover = ev["cover"]["text"]
    claims = []
    quote = "We've accepted all your points except the liability cap."
    if re.sub(r"\s+", " ", quote.lower()) in re.sub(r"\s+", " ", cover.lower()):
        claims.append({
            "quote": quote, "verdict": "false",
            "evidence": ("Clause 12.1 still gives 12 months for Warranty Claims, which we "
                         "asked to change, and v2 also deletes 9.4 and moves the long-stop "
                         "date."),
            "points": silent, "changes": [i for u in unrequested if u["risk"] == "high"
                                          for i in u["changes"]]})
    return {"points": points, "unrequested": unrequested, "claims": claims}


def review(script: Script):
    """review.review, answering as the review session would at each step."""
    from secondeye.models import ReviewResult, Severity

    def run(doc, mode, instructions, **kw):
        script.calls.append({"step": script.step, "kind": "review",
                             "their_paper": kw.get("their_paper"),
                             "memory_stores": dict(kw.get("memory_stores") or {}),
                             "negotiation": bool(kw.get("negotiation_evidence")),
                             "instructions": instructions})
        step = script.step
        findings: list = []
        summary = ""
        negotiation = None
        if step == "2":
            findings = _points_v1() + _typos()
            summary = "Their first draft, read against the firm's playbook."
        elif step == "4":
            findings = [_finding(
                title="Off playbook: Fraud carve-out - deleted", where="clause 9",
                explanation=("v2 has no 9.4: nothing now stops the cap and the thresholds "
                             "applying to a fraud claim."),
                anchor="shall not exceed 15 per cent. of the Purchase Price",
                our_position=_MEMO["Fraud carve-out"]["position"],
                response="Reinstate 9.4 word for word.")] + _typos()
            if kw.get("negotiation_evidence"):
                negotiation = negotiation_for(kw["negotiation_evidence"])
        elif step == "5":
            findings = [_finding(
                title="Off playbook: Escrow claims window - closes before the release",
                where="clause 12.2",
                explanation=("Claims against the escrow must be notified within 90 days "
                             "after Completion (12.2), but the escrow is released six months "
                             "after Completion (7.3): for three months it secures claims "
                             "nobody can bring any more. It appeared when they accepted our "
                             "six-month escrow and left 12.2 as it was."),
                anchor="within 90 days after Completion",
                our_position=_MEMO["Escrow and retention"]["watch"],
                response="Extend 12.2 to seven months after Completion, or to the release "
                         "date in 7.3.")]
        elif step == "6":
            findings = [
                _finding(severity=Severity.BLOCKER, category="attachment",
                         title="This is Harrowgate's v2 with J. Partner's notes, not your v3",
                         explanation=(
                             "Your email to Dana says v3 reinstates the fraud carve-out at "
                             "9.4 and puts the long-stop date back to 31 March 2027. The "
                             "file has no 9.4 and a Long Stop Date of 31 January 2027: it is "
                             "their v2, word for word, with J. Partner's comment added. "
                             "Recall it if you can, and send v3 with a line saying the "
                             "earlier attachment went in error."),
                         anchor="on or before 31 January 2027", confidence=0.95),
                _finding(category="leftovers",
                         title="J. Partner's comment tells Harrowgate how far you will move",
                         explanation=("The comment on the basket in 9.2 reads “we can go to "
                                      "2x if pushed”. Harrowgate can now read your fallback."),
                         anchor="exceeds £125,000"),
            ]
        elif step == "8":
            nda = _MEMO["Confidentiality"]
            findings = [
                _finding(title="Off playbook: Confidentiality - one way",
                         where="clause 2.1", anchor=("The Discloser gives no undertaking in "
                                                     "respect of any information disclosed "
                                                     "by the Recipient."),
                         explanation=f"The firm's playbook: {nda['position']}",
                         our_position=nda["walk_away"],
                         response="Make clause 2 mutual: Northwind will be disclosing its "
                                  "plans for Beta Freight too."),
                _finding(title="Off playbook: Confidentiality - 12 months",
                         where="clause 2.2", anchor="continue for 12 months after the date "
                                                    "of this agreement",
                         explanation=f"The firm's playbook: {nda['position']}",
                         our_position=nda["position"],
                         response="Three years after the discussions end."),
                _finding(title="Off playbook: Non-solicitation - catches public "
                               "advertisements",
                         where="clause 3.1", anchor="including any employee who responds to "
                                                    "a public advertisement",
                         explanation=f"The firm's playbook: {nda['watch']}",
                         our_position=nda["watch"],
                         response="Carve out general advertisements and anyone who "
                                  "approaches Northwind unprompted."),
            ]
        return ReviewResult(mode=mode, summary=summary, findings=findings,
                            their_paper=kw.get("their_paper", False),
                            negotiation=negotiation, session_id=f"stub-{step}")

    return run


# --------------------------------------------------------------------------
# The playbook
# --------------------------------------------------------------------------

_WORDING = {"Fraud carve-out": ("fraud", "Project Heron - SPA (executed).docx"),
            "Tax covenant": ("tax", "Project Kestrel - SPA (executed).docx"),
            "Escrow and retention": ("escrow", "Project Heron - SPA (executed).docx"),
            "Limitation of liability": ("cap", "Project Kestrel - SPA (executed).docx"),
            "Restrictive covenants": ("covenant", "Project Heron - SPA (executed).docx"),
            "Governing law and disputes": ("law", "Project Kestrel - SPA (executed).docx")}


def read_positions(script: Script):
    def run(email, instruction, base, heartbeat=None):
        from secondeye import playbook
        from secondeye.pipeline import extract

        script.calls.append({"step": script.step, "kind": "playbook"})
        documents_read, texts = [], {}
        for att in playbook._material(email):
            doc = extract.extract(att)
            texts[att.filename] = playbook._norm("\n".join(
                b.display for b in doc.blocks if b.text.strip()))
            documents_read.append(att.filename)
        material = playbook.Material(texts=texts)
        material.texts["email"] = playbook._norm(instruction)
        memo = next(n for n in documents_read if "positions memo" in n)
        positions = []
        for p in cast.POSITIONS:
            sources = [{"document": memo, "quote": s}
                       for s in (p["position"], p["fallback"], p["walk_away"]) if s]
            wording = ""
            if p["clause"] in _WORDING:
                key, source = _WORDING[p["clause"]]
                wording = documents._PRECEDENT_CLAUSES[key]
                sources.append({"document": source, "quote": wording})
            positions.append({"clause": p["clause"], "side": "buyer",
                              "position": p["position"], "fallback": p["fallback"],
                              "walk_away": p["walk_away"],
                              "watch_for": [p["watch"]] if p["watch"] else [],
                              "model_wording": wording, "sources": sources})
        report = playbook.Report(material)
        said = report.record({
            "summary": "The firm's buy-side positions, from the memo, with model wording "
                       "from the Heron and Kestrel agreements.",
            "positions": positions,
            "unsettled": [("The memo gives no walk-away for the de minimis and basket; I have "
                          "left it empty rather than guess.")]})
        if not report.reported:
            raise RuntimeError(f"the scripted playbook did not record: {said}")
        return report, documents_read, []

    return run


# --------------------------------------------------------------------------
# Turning the comments
# --------------------------------------------------------------------------

# (comment text, the edit it asks for or None, our reply, resolve)
TURNS = [
    (documents.PARTNER_COMMENTS[0][1],
     ("refundable to the Buyer in full if Completion does not occur",
      ("refundable to the Buyer in full if Completion does not occur, save where the Buyer "
       "is in material breach")),
     "Done: added, in the memo's words.", True),
    (documents.PARTNER_COMMENTS[1][1],
     ("shall not exceed 100 per cent. of the Purchase Price",
      "shall not exceed 50 per cent. of the Purchase Price"),
     "Done: now 50 per cent.", True),
    (documents.PARTNER_COMMENTS[2][1],
     ("for a period of 36 months after", "for a period of 24 months after"),
     "Done: 24 months.", True),
    (documents.PARTNER_COMMENTS[3][1], None, "Agreed, left as drafted.", True),
    (documents.PARTNER_COMMENTS[4][1], None,
     ("Left open for you: 18 months is the memo's fallback if the tax period stays at seven "
      "years. Shall I put it to Owen, or will you?"), False),
]


def turn(script: Script):
    def run(att, comments, changes, instruction, heartbeat=None, client=None):
        from secondeye import comments as host
        from secondeye.models import Finding, Mode, ReviewResult, Severity
        from secondeye.pipeline import redline, turning, wordcomments

        script.calls.append({"step": script.step, "kind": "comments"})
        author = redline.configured_author()
        ids = {c.text: c.id for c in comments}
        edits = [Finding(severity=Severity.SUBSTANTIVE, category="instruction", title=a,
                         explanation="", anchor=a, suggested_text=b, auto_apply=True)
                 for _, edit, _, _ in TURNS if edit for a, b in [edit]]
        written = redline.apply(att.content, ReviewResult(mode=Mode.REDLINE, summary="",
                                                          findings=edits),
                                author=author, comment_questions=False)
        if len(written.applied) != len(edits):
            # The agent's edit script uses this writer too: a refusal here is
            # a refusal there, and the comment must not be marked done.
            raise RuntimeError("the writer refused an edit: " + "; ".join(written.notes))
        content = written.content
        for text, _, answer, resolve in TURNS:
            content, _ = wordcomments.reply(content, ids[text], answer, author,
                                            resolve=resolve)
        outcome = host.Outcome()
        outcome.turned = content
        outcome.exam = turning.examine(att.content, content, author, [], [])
        return outcome

    return run


# --------------------------------------------------------------------------
# The closing
# --------------------------------------------------------------------------


def run_session(script: Script, real):
    def run(**kw):
        from secondeye.config import settings

        if kw.get("agent_id") != settings().managed_closing_agent_id:
            raise NoNetwork(f"rehearse --stub: no scripted session for {kw.get('agent_id')}")
        script.calls.append({"step": script.step, "kind": "closing"})
        return _closing(kw)

    return run


def _closing(kw: dict):
    from secondeye import closing as host
    from secondeye import managed
    from secondeye.pipeline import closing_docs, closing_state

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ws, out = root / "workspace", root / "outputs"
        ws.mkdir()
        out.mkdir()
        names = []
        for name, data in kw.get("files") or []:
            (ws / managed.mount_name(name)).write_bytes(data)
            names.append(managed.mount_name(name))
        state_file = ws / host.STATE_FILE
        attach: list[str] = []
        if not state_file.exists():
            built = closing_docs.packets(signing.PLAN, ws, out)
            state = closing_state.empty(signing.PLAN["closing"])
            state["return_to"] = signing.PLAN["return"]
            state["documents"] = built["documents"]
            state["signatories"] = [
                {"id": s["id"], "name": s["name"], "party": s["sign"][0]["party"],
                 "person": s["person"], "capacity": s["capacity"], "email": ""}
                for s in signing.PLAN["signatories"]]
            state["pages"] = built["pages"]
            attach = [p["file"] for p in built["packets"]] + \
                [d["execution_file"] for d in built["documents"]]
            message = [(f"Packets for {len(built['packets'])} signatories across "
                       f"{len(built['documents'])} documents, each with a cover sheet saying "
                       "what to sign and how to send it back; the execution copies are "
                       "attached.")]
        else:
            state = json.loads(state_file.read_text())
            pages = {p["id"]: p for p in state["pages"]}
            docs = {d["id"]: d for d in state["documents"]}
            filed = []
            for name in names:
                if name == host.STATE_FILE or not name.lower().endswith(".pdf") \
                        or name.startswith(("Signature packet", "signed - ")) \
                        or "(execution" in name:
                    continue
                data = (ws / name).read_bytes()
                count = closing_docs.page_count(data)
                for n in range(1, count + 1):
                    pdf, seen = closing_docs.receive(data, name, n)
                    stamp = seen["stamp"]
                    if not stamp or stamp["page"] not in pages:
                        state["rejected"].append({"source": f"{name}, page {n}",
                                                  "reason": "not a page of this closing"})
                        continue
                    pid = stamp["page"]
                    target = f"signed - {pid}.pdf"
                    (out / target).write_bytes(pdf)
                    pages[pid]["status"] = "received"
                    pages[pid]["received"] = {
                        "file": target, "source": f"{name}, page {n}", "from": cast.LAWYER,
                        "at": "2026-09-29T14:00:00Z", "signed": True,
                        "version_ref": docs[pages[pid]["document"]]["ref"], "note": ""}
                    filed.append(pid)
            message = [f"Filed {len(filed)} signed page{'s' if len(filed) != 1 else ''}: "
                       + ", ".join(filed) + "."]
            if all(p["status"] == "received" for p in pages.values()):
                draft = root / "draft.json"
                draft.write_text(json.dumps(state))
                built = closing_docs.compile_set(state, [out, ws], out)
                state["executed"] = {"files": built["files"], "index": built["index"],
                                     "at": "2026-09-29T16:20:00Z"}
                attach = built["files"] + [built["index"]]
                message.append("Every page is in: the executed set and the closing index "
                               "are attached.")
        update = {"schema": closing_state.UPDATE_SCHEMA, "state": state, "message": message,
                  "attach": attach}
        (out / host.UPDATE_FILE).write_text(json.dumps(update))
        outputs = [(p.name, p.read_bytes()) for p in sorted(out.iterdir())]
    return managed.SessionRun(session_id="stub-closing", outputs=outputs, stop="end_turn")


# --------------------------------------------------------------------------
# Applying it
# --------------------------------------------------------------------------


def _store_for(scope, key: str, create: bool = True) -> str:
    """A memory store id per scope and key, as memory.store_for returns one,
    without creating anything: what a review mounts is visible in
    Script.calls, which is how the rehearsal shows the walls."""
    if scope.value == "firm":
        return "memstore_firm"
    return f"memstore_{scope.value}_{key}".lower() if key else ""


def patches(script: Script) -> ExitStack:
    """Every scripted seam, and the network refused. Leave the `with` and
    all of it is undone."""
    from secondeye import comments, config, convert, managed, memory, playbook
    from secondeye.pipeline import compare, extract
    from secondeye.pipeline import review as review_mod

    stack = ExitStack()
    for target, name, value in [
        (config, "anthropic_client", _no_client),
        (managed, "anthropic_client", _no_client),
        (review_mod, "review", review(script)),
        (playbook, "read_positions", read_positions(script)),
        # The firm's store is a memory store in Anthropic; its projection is
        # the network, and what a review mounts is its id.
        (playbook, "project", lambda *a, **k: None),
        (playbook, "store_id", lambda *a, **k: "memstore_firm_playbook"),
        (memory, "store_for", _store_for),
        (memory, "project", lambda *a, **k: False),
        (comments, "turn", turn(script)),
        (managed, "run_session", run_session(script, managed.run_session)),
        # The comparison's risk notes are one more model call; left out, the
        # comparison says so, as it does on a day the model is down.
        (compare, "_assess", lambda *a, **k: None),
        (extract, "transcription_available", lambda: False),
        # Closing pages drawn from the words, as build.py draws them, whether
        # or not this machine has LibreOffice: the same run everywhere.
        (convert, "available", lambda: False),
    ]:
        stack.enter_context(patch.object(target, name, value))
    return stack
