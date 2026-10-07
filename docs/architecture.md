# Architecture

```
  inbound email
       |
       v
  mail provider  ──  Cloudflare: Worker email() -> a Workflow per email,
  (Cloudflare /                   whose steps call flow.py in the container
   AgentMail /     ──  others: webhook POST /webhooks/inbound, 200 at once
   Postmark)                       |
       ^                     handler._route / flow.prepare
       |                           |   (handler.handle() runs every stage in
       |                           |    one process, for lra replay)
       |         ┌─────────────────┼──────────────────┐
       |         v                 v                  v
       |    intake +          store (sqlite)     router: revoke /
       |    identity        idempotency, jobs     connect / stop,
       |         |                                and automated mail
       |         v
       |   thread.resolve()       a reply with no attachment goes to
       |         |                followup: answer a question, or undo
       |         v
       |     extract              .docx / .pdf / .txt -> numbered blocks
       |         |
       |         ├──────────────> archive.store()   off by default
       |         v
       |   ┌─────────────────────────────────────────┐
       |   │  deterministic pass -- no model at all  │
       |   │                                         │
       |   │   checks.run_all      placeholders,     │
       |   │                       defined terms,    │
       |   │                       cross-refs,       │
       |   │                       numbering,        │
       |   │                       amounts, dates,   │
       |   │                       party names,      │
       |   │                       metadata          │
       |   │                                         │
       |   │   email_checks.run_all                  │
       |   │                       wrong recipient,  │
       |   │                       wrong attachment, │
       |   │                       email contradicts │
       |   │                       the document      │
       |   └─────────────────────────────────────────┘
       |         |
       |         | Finding[]  -- also rendered into the agent's prompt,
       |         |               so it does not re-report the mechanics
       |         v
       |   ┌─────────────────────────────────────────┐
       |   │  review agent (Managed Agents session,  │
       |   │  no client attached)                    │
       |   │                                         │
       |   │   dms MCP server ──> DMS, as this       │
       |   │     (one matter)      lawyer            │
       |   │   validate_findings.py ──> findings.json│
       |   │   notes.json ──> memory, pending        │
       |   │   redline.py  ──> the redline           │
       |   └─────────────────────────────────────────┘
       |         |                    ^
       |         v                    |
       |     redline             memory stores, mounted
       |   Finding[] -> w:ins/w:del   firm + personal + matter
       |   verify_opens gate
       |         |
       |         ├──────────────> thread.record_changes()
       |         |                the ledger every undo is rebuilt from
       |         v
       └───── reply              verdict-first email + attachment
                                 sender only, never reply-all
```

## Design rules

**Mechanics before judgment.** The deterministic pass runs first and the
agent is told what it caught. Two reasons, in this order: those findings are
exact and cannot be hallucinated, so they lead the reply regardless of what the
model says; and an agent that has been shown the mechanical list stops
re-reporting it in its own words, which is the fastest way to make a review feel
padded. `checks.party_names` and `email_checks.recipient_mismatch` both emit
blocker-severity findings with no model involved, so false-positive tuning for
those lives in `checks.py`, not in the prompt.

**The mail vendor is behind one interface.** `mail/base.py` is the only place
that knows a vendor exists. Everything above speaks `InboundEmail` and
`OutboundEmail`. Swapping AgentMail for Postmark is one file.

**Nothing holds a review open.** Agent loops plus document rewriting take
minutes. On Cloudflare each email is a Workflow instance named after the
message, so a second delivery cannot start a second review; its steps are
retried on their own, the session runs in Anthropic's sandbox with nobody
attached, and a webhook (or a 60-second poll) wakes the Workflow when it
stops. A vendor webhook (`/webhooks/inbound`) returns 200 at once and runs
`handler.handle()` in the background, with `store.claim()` guarding
duplicates.

**The agent's reach is the lawyer's reach.** The document system is our MCP
server, called with that lawyer's own token (from their Anthropic vault) and
bound by a signed URL to the one matter the email belongs to. The ethical wall
is enforced by the firm's existing access control, and narrowed to the matter
by ours. See `docs/oauth.md` and `src/lra/dms_mcp.py`.

**The model returns structure, not prose.** The session records its findings
as `findings.json`, written only by the skill's validator against a fixed
schema and checked again when it is read. The redliner and the email
writer both consume the same `Finding` objects, so what the email claims was
changed is what was changed.

**Memory is layered and explainable.** Firm conventions, then personal
preferences that override them, then matter-specific positions. When the agent
flags something because of memory rather than because it is objectively wrong,
the email says so.

**Rejections are sentences, not error codes.** Every `intake.Rejection` and
every tool failure carries text we are willing to email back verbatim. There is
no path that produces a stack trace or silence.

**Nothing leaves without verification.** `redline.verify_opens()` runs on every
generated document. Failure degrades to a memo-only reply rather than attaching
a file that might not open.

**Replies go to the sender only.** Never reply-all, never to a CC, never to
anyone found in a quoted thread. Enforced in `reply.compose` and tested.

**Degradation is always to something useful.** No DMS consent means a standalone
review plus an offer, not a refusal. A failed redline means a memo, not an
error. A missing matter means no matter memory, not a guess.
