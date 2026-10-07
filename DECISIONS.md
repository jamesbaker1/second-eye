# Decisions

## Settled

| # | Question | Decision | Consequence |
| --- | --- | --- | --- |
| 1 | Who is it for on day one | A whole firm | Multi-user from the start. Identity resolution, per-user memory, audit log, and a real confidentiality posture are v1 concerns, not phase-5 concerns. |
| 2 | Addressing | One address for everyone | `review@<domain>`. User and matter are inferred, never typed. Keeps the mail provider choice open. Puts weight on identity and matter resolution. |
| 3 | Redline posture | Conservative | Only unambiguous fixes are written as tracked changes. Anything with legal meaning is raised in the email for the lawyer to decide. |
| 4 | Entry points | Support all three | Forward, CC-on-draft, and BCC-on-send all work. The agent detects which happened and adapts its reply. |
| 5 | Human in the loop | None | The agent replies directly to the requesting lawyer. Defensible because nothing reaches a client without that lawyer sending it. |
| 6 | Retention | Keep everything | Full history. This is what makes memory possible. It is also the largest confidentiality exposure in the system and it drives decision 8. |
| 7 | House style | Learned, not written | From the DMS, past documents, and observed behavior. Personal per lawyer, with a shared firm layer. See `docs/memory.md`. |
| 8 | Storage | Firm-controlled cloud | Our own storage, encrypted at rest, per-matter access control, in a tenant the firm controls. |
| 9 | Agentic or single-call | Agentic | The review is a tool-use loop, not one call. The agent decides what to look up. See `docs/agentic.md`. |
| 10 | Which agent surface | ~~Anthropic SDK Tool Runner~~ **Managed Agents, since 2026-09-26 (see 29)** | The original reasoning: not the Claude Agent SDK, which is the Claude Code harness, and not Managed Agents, which conflicted with decision 8. Superseded by 29 for the proof of concept. |
| 11 | DMS access model | Per-lawyer delegated OAuth | The agent acts as the requesting lawyer, never as a firm-wide service account. The ethical wall is structural. See `docs/oauth.md`. |
| 12 | DMS scopes | Read-only | The agent reads the firm's documents. Filing anything back is a separate scope, requested separately, later. |

## Settled in the design review (2026-09-20)

These supersede earlier rows where they conflict. The product framing that drove
them is the owner's: **a super-smart, helpful junior associate**. Not a checker that
reports problems, but a colleague who makes the changes, asks when it is unsure,
takes instructions in plain English, and can undo what it did.

| # | Question | Decision | Consequence |
| --- | --- | --- | --- |
| 13 | First deployment | The owner, on his own documents | No third-party privileged material on day one. The archive can be switched on for this deployment: what `docs/archive.md` lists as launch blockers protects *other people's* privileged material, and on the owner's own documents there is none. Encryption at rest with firm-held keys and a signed retention policy still stand before a second user or a live client matter. |
| 14 | Clean documents | Always reply | A short "nothing to flag" confirms it ran. Silence is indistinguishable from the service being down. |
| 15 | Primary trigger | Email | The ambient check-in hook is deferred. Everything optimizes for the send moment and for needing nothing installed. |
| 16 | Latency | Up to five minutes, quality over speed | No instant acknowledgement. This is the decision that unlocks the most: a self-verification pass, deeper document-system lookups, chunked review of long documents, and a higher effort setting. **Enforced** since 2026-09-26: `AGENT_TIME_BUDGET_SECONDS` (300) is checked between model turns; past it the model reports what it has, and the reply says "Partly reviewed" rather than pretending otherwise. |
| 17 | Redline aggression | Write what it can articulate; ask on the rest | **Partly built**: certain-and-unambiguous findings are written, and a finding the agent cannot resolve becomes a Word comment asking the question beside the clause. Reframed from "easy vs hard" to **conspicuousness**: the danger is not a wrong change, which costs one click to reject, but a wrong change accepted without anyone noticing. So write it, and make the email a complete honest manifest naming every judgment call. |
| 18 | Non-text repairs | Metadata strip, numbering rebuild, formatting normalization | Returned alongside the redline, with a clean client-ready copy. These are the fixes that save the most time and they are not tracked changes at all. **Two of three built**: the clean copy (`pipeline/clean.py`) and formatting repair for fonts, styles and spacing (`pipeline/repair.py`). Numbering is detected, not rebuilt: it is the one thing the repair gate cannot verify, so it is deliberately left alone. |
| 19 | Ambiguous fixes | Depends on the finding type | **Partly built**: party-name drift and mixed date formats are guessed and written; the rest is flagged. Guess where a wrong guess is obvious and harmless. Flag, and offer options in the email, where a wrong guess could be accepted silently. |
| 20 | Reply scope | Anything you would ask a junior associate | Respond to flags, make specific edits, draft new clauses, restructure, rename defined terms throughout. This is the biggest new capability and it needs full thread and document state. |
| 21 | Which version a reply operates on | The attachment if there is one, otherwise ask | Handles the real pattern: accept some changes, edit a little, send it back. The agent must detect which version it is looking at rather than assume. |
| 22 | Pushback | Flag, then comply | "That leaves you with an uncapped indemnity. I have made the change as asked." It says what it thinks once, then does what it is told. |
| 23 | Undo | Plain English, any granularity | "Undo the change to clause 7", "revert what you did to the indemnity", "start over". Requires a durable record of every change made in the thread. |
| 24 | Evaluation corpus | Synthetic, with planted defects | Fully controlled and measurable. Its limitation is that it only tests defects we thought to plant, so the completeness of the corpus is itself a thing to keep working on. |
| 25 | Code-execution retention | Accepted, and widened 2026-09-26 | The tool is not ZDR-eligible and keeps uploaded files for up to thirty days. The owner's call, on his own documents. **Revised:** the earlier rule ("produce derived files only, never edit") is superseded for the end-to-end proof of concept: the review runs as a Managed Agent in Anthropic's container, the document is mounted there, and our own tools (checks, compare, clean, and the tracked-changes writer) run inside it as a skill. Every file that comes back is re-verified locally (`redline.verify`) before it is attached; that proof is the one step that stays on our side by design. Production security for a firm's documents is an open decision, to be made before any firm's matter goes through this path. |
| 26 | How far the associate framing goes | Everything a junior does, by email; code review is the way in | The destination is a junior associate you can email anything. The order is unchanged: `PRODUCT.md` still governs, because a junior earns bigger work by never being wrong on small work. Two additions to `ROADMAP.md`: first drafts from the firm's form (phase E), and legal research, previously excluded, as a **stretch goal** under its own rules: verified citations, labeled research not advice, and decision 5 reopened for that skill only. |
| 27 | Hosting for a design-partner firm | Cloudflare end to end: Email Service, Queues, a container, D1, R2 | One vendor and one account a firm can be given or can own. The application stays an ordinary container, so this is reversible: `Dockerfile` runs anywhere and `store.connect()` still opens a local file. D1 is reached through a sqlite3-shaped adapter rather than by rewriting five storage modules, which costs transactions across a `with` block and nothing else (`docs/deploy-cloudflare.md`). |
| 28 | Encryption at rest | Documents and tokens sealed with AES-256-GCM under a key the firm generates; the service will not start on hosted storage without one | Host-side encryption answers a stolen disk. A firm's general counsel asks about a leaked token, a misconfigured bucket and a request served on the host, and only a key the host does not hold answers those. Rows are not sealed, and the deployment doc says exactly which ones quote the document. |
| 29 | Agent loop and container | Anthropic Managed Agents, cloud sandbox, cut over completely | The owner's call, 2026-09-26, for a full end-to-end proof of concept: "do it all through Anthropic as much as possible". The review is a Managed Agent session; the document is mounted in Anthropic's container; our checks, compare, clean and tracked-changes writer run there as a custom skill alongside Anthropic's `docx`/`pdf`/`xlsx`/`pptx` skills; the SDK tool-runner loop is deleted, not kept as a fallback. What stays on our side: receiving and sending email (the Cloudflare edge), thread state and the undo ledger, memory, and re-verifying every returned file with `redline.verify` before it is attached. This deliberately conflicts with decision 8 (firm-controlled storage) for the duration of the POC; production security for a firm's documents is to be decided before any firm's matter goes through it. |
| 30 | Who decides | The model decides everything: what an email asks for, which file is the document, whose paper it is, who the reply goes to, what a reply means, which mechanical findings are real, whether a returned file is sound, and the words of the reply | The owner's call, 2026-09-28: "everything LLM driven, to be the most performant possible", chosen over keeping hard-coded guards on recipients, undo and file verification. The deterministic code stays, as evidence and tools: checks run first and free, `redline.verify` runs on every file, addresses carry internal and external flags. Their output is given to the model and the model's call stands. The allowlist and DMARC drop at the edge remain as intake filters, because they run before any model call. This supersedes the rule-based routing and the "verify refuses" gate as they are replaced, each only after the scorecard shows the model matching or beating it (`docs/migration.md`, phase 3). It knowingly accepts that a model error can reach a client or a counterparty. |
| 31 | Reply policy | The owner approved a firm-controlled sender_only policy for the pilot (2026-09-28); under DECISIONS 30 the model decides recipients only when a firm sets REPLY_POLICY=model. | An amendment to 30, as a firm policy rather than a model guard: the firm sets it, the firm's general counsel can read it, and no model output changes it. Enforced where mail leaves, twice: `policy.py` readdresses every send and every reply stored for the Worker to the inbound sender, and the Worker refuses anything not addressed to exactly one allowlisted sender. `REPLY_POLICY=model` restores 30 as written. |
| 32 | The Workflow and detached sessions are the only paths (2026-09-29) | The owner asked for phase 5 before a live model run; rollback is a git revert, not a flag. | Every admitted email is carried by a Cloudflare Workflow and every review is a Managed Agents session with no client attached; the queue, its dead-letter queue, the job lease, the slow-notice timer thread, the streaming reviewer and the custom DMS tools are deleted (`docs/migration.md`, phase 5). Kept: the rule-based routing, as triage's fallback and the baseline the live model must beat (DECISIONS 30); the stream driver, for the associate, playbook and closing agents, which still answer custom tools; `oauth.py`, for consent and the capability binding. Two flags that chose between old and new paths doubled what had to be tested and explained, for a rollback nobody had needed. |

## The associate loop

Decisions 20 to 23 together describe a conversation about a document that
accumulates state, rather than a single request and response. Most of it is now
built. What it required, and where each piece stands:

- **Thread state.** Which document, which version, what changes have been made,
  and which questions are outstanding. Keyed by mail thread. **Built**, in
  `thread.py`.
- **A change ledger.** Every tracked change the agent has written, addressable in
  plain English, so "undo what you did to clause 7" can resolve to a specific
  revision id. **Built.** The ledger is the source of truth and the redline is
  regenerated from the original each round, which is what makes undo safe.
- **Intent routing on replies.** A reply may be an instruction, an answer to a
  question the agent asked, an undo, a new version of the document, or a
  complaint. These are not distinguishable by keyword. **Built**:
  `pipeline/router.py` handles the unambiguous whole-message replies
  deterministically, `followup.py` resolves answers and undos against the
  ledger, and `pipeline/instruct.py` carries out free-form instructions.
- **Version detection.** Given an attachment, work out whether it is the original,
  the agent's own redline, or something the lawyer has since edited. **Not
  built.** A reply carrying an attachment starts a fresh review.

Version detection is the remaining piece. `STATUS.md` has the current order.

## Consequences worth stating plainly

**Decision 4 has a sharp edge.** BCC-on-send means the document has already gone
to the client by the time the agent finds a blocker. That is not a review, it is
an incident report. It should still be supported, but the reply for a BCC has to
read differently: "this has already gone out, here is what is wrong with it,"
not "here is your redline." Treated as a separate reply template.

**Decision 6 needs a written policy, not just a config value.** Keeping every
privileged document a firm sends creates a discovery target and a breach target.
Before this is used on live matters it needs, at minimum: encryption at rest
with firm-held keys, per-matter access control so a lawyer cannot retrieve a
document from a matter they are not on, an audit log of every access, a deletion
path for when a client demands it, and a stated retention period even if that
period is long. This is tracked in phase 4 and is a launch blocker.

**Decision 5 plus decision 1 is a combination worth revisiting.** Firm-wide with
no approval queue means an associate can rely on the agent's output with nobody
checking it. That is fine when the agent is conservative and the lawyer is the
one who sends the document. It stops being fine if the agent ever starts sounding
confident about substantive legal conclusions. The system prompt already forbids
advising on the transaction. Keep it that way. Decision 26 makes legal research
a stretch goal, and this paragraph is why that skill reopens decision 5 rather
than inheriting it.

## Still open

Numbered `O13`+ rather than `13`+. These questions were numbered continuously
with the settled list above when that list stopped at 12, and the design review
then extended it to 24, so for a while `16` meant both "Latency" and "Long
documents" in one file. Renumbering these rows instead would silently break the
reference `config.py` already makes to "open question 18", and a cross-reference
that resolves to the wrong question is worse than one that does not resolve.
The `O` is what makes it resolve to exactly one row.

| # | Question | Why it is not urgent yet |
| --- | --- | --- |
| O13 | Which DMS: iManage, NetDocuments, SharePoint, something else | The adapter is written against iManage. The interface is one file, so switching is cheap, but the firm's answer decides what gets tested |
| O14 | Mail provider and domain | One address for everyone keeps every provider viable. Recommendation is in `docs/providers.md` |
| O15 | Does the agent see the final sent version | The single best learning signal in the system, and now the main open question. Requires a BCC-on-send habit or a DMS hook. See `docs/memory.md`. Since 2026-09-26 the habit is safe to adopt: BCC'd with nothing attached is answered with silence, and every reply goes to the sender alone. What is still not built is reading the sent version back against our redline |
| O16 | Long documents | A 100-page credit agreement will not fit one context comfortably. The agent can now read the document in ranges, which helps, but a chunking strategy is still needed |
| O17 | Documents the agent should not touch | Opposing counsel's draft is now detected and reviewed as their paper: no cosmetic tracked changes, an issues list against the playbook (docs/playbook.md). A court filing and an executed agreement are still open: detect and refuse, or review with a caveat |
| O18 | Model routing and cost | One model for everything, or triage on a cheaper model and escalate. The agent loop makes this a real cost question rather than a rounding error |
| O19 | Who approves the memory | Learned firm conventions are written to a store someone should be able to read and disagree with. Who owns that review. The firm's playbook has its own answer since 2026-09-28: it is changed only by a playbook admin (`PLAYBOOK_ADMINS`), by email, and takes effect when that admin replies "approve playbook" (docs/playbook.md). Learned conventions are still open |
