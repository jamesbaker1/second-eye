# Status

Where the project stands, what has been done, and what comes next. This is the
page to read first when coming back to the repo after a break. `ROADMAP.md` is
the long-term order; `DECISIONS.md` is what is settled; `docs/migration.md` is
the architecture change just finished; this page is the current position.

Last reviewed: 2026-10-07.

## In one paragraph

Redline Desk is a legal review agent that lives at an email address; one
production deployment runs it today, for one allowlisted sender. A lawyer
forwards a draft and gets back a one-line verdict, the findings clause by clause, and the
same document with real Word tracked changes; replies in the thread answer
its questions, undo its changes, or ask for a clean copy, renumbering,
signature pages or a deadlines calendar. Everything that works without a
model has been run on real email in production and passes. Everything that
needs the model (the substantive review, their-paper issues lists, the
negotiation check, turning comments, closings, blacklines, the playbook,
triage) is built and tested against a scripted platform, and has not yet
run live on this code. That one gap outranks everything else on this page.

## Scorecard

| | |
| --- | --- |
| Tests | 2,294 Python, green in both storage modes; 80 Worker; 20 MCP server |
| API contract | Every fake Anthropic client checked against the SDK's own types (anthropic 1.8.0) on every call |
| Deterministic checks | 27/27 planted defects caught, 0 false positives across 15 clean documents, including three realistic agreements; outbound messages 29/29 caught, 0 false positives across 28 clean ones |
| Reply parsing | 220 mail-client format x command cases pass (Gmail, Outlook, Apple Mail, mobile, three languages, signatures and disclaimers) |
| Production | Every email a Cloudflare Workflow (DECISIONS 32); canaries pass for every mechanical command |
| Model-backed | Built, not yet run live on this code |

## Architecture now

Mail reaches a Cloudflare Worker, which admits only allowlisted,
authenticated senders (strangers and forgeries are dropped without a word)
and starts one Workflow per message (`cloudflare/src/review-flow.ts`). The
Workflow calls a Python container for each step (`flow.py`): prepare
(intake, the deterministic checks, the thread), start a Managed Agents
session with the document mounted and no client attached, wait for
Anthropic's webhook or poll, finish (read the agent's files, verify every
returned document), send once through an outbox, record. The model decides
under DECISIONS 30; the deterministic code is the evidence it decides from.
Firm policy the model cannot override (DECISIONS 31): replies go only to the
sender, no-AI clients get no model call, memory is walled by client.

## What works, and how we know

| Capability | Proof | Where |
| --- | --- | --- |
| Review with a verdict, clause-labelled findings, tracked changes, margin reasons | Production (mechanical half); stubs (model half) | `pipeline/review.py`, `pipeline/reply.py`, `pipeline/redline.py` |
| Answers, undo by number, "B is fine", several requests in one reply | Production canaries 2026-09-30 | `followup.py`, `pipeline/router.py` |
| Clean copy, Word and PDF | Production (Word); tests (PDF) | `pipeline/clean.py`, `pipeline/clean_pdf.py` |
| Compare, cumulative blacklines, PDF blackline with a change summary | Tests; blackline needs the model for significance | `pipeline/compare.py`, `blackline.py` |
| Renumber with cross-references repaired | Production | `pipeline/renumber.py` |
| Sig pages; signature packets, returned pages, executed set | Production (sig pages); stubs (closing) | `pipeline/sigpack.py`, `closing.py` |
| Deadlines table and calendar file | Production | `pipeline/deadlines.py`, `pipeline/ics.py` |
| Deal arithmetic, clause timing, party-name drift | Tests and evals | `pipeline/dealmath.py`, `pipeline/timeline.py` |
| What leaves by email, no model needed: every attachment's leftovers (properties naming another client, embedded and linked files, speaker notes, hidden sheets, PDF mark-up), DRAFT on a version sent as final, wrong version, a promised attachment missing | Evals; demo built (`python -m demos.outbound`), not yet sent to production | `pipeline/outbound.py`, `pipeline/email_checks.py`, `demos/outbound/README.md` |
| Their paper and the issues list | Stubs | `pipeline/their_paper.py`, `pipeline/issues_list.py` |
| Negotiation ledger and the cover-note check | Stubs | `negotiation.py` |
| Turning comments and the other side's markup | Stubs; Word itself not yet checked | `comments.py` |
| Playbook sent by email, approved before use | Stubs | `playbook.py` |
| Triage by the model, in shadow beside the rules (rules score 51/59) | Stubs; `TRIAGE=rules` in production until the first live run | `triage.py`, `evals/score_model.py` |
| Document system through our MCP server and Anthropic vaults | Stubs; no iManage instance | `dms_mcp.py`, `cloudflare/dms-mcp/` |
| Security pack: sender-only replies, no-AI clients, privilege notice, zero-retention option, kill switch, client memory walls, audit export, trust page | Tests | `policy.py`, `clients.py`, `audit.py`, `docs/trust.md` |
| Nothing kept after 7 days, nothing learned unless asked (2026-10-04): a daily sweep from the cron deletes conversations, closings, unconfirmed notes and leftover Anthropic sessions 7 days after the last activity; sessions are deleted, not archived, when a review ends; `LEARN_FROM_OUTCOMES` off; security page | Tests; not yet deployed (the first sweep deletes production threads idle over 7 days) | `retention.py`, `site/public/security.html` |
| `lra purge --client/--matter/--lawyer`: every kind we hold plus Anthropic's sessions, files and memory stores, dry run by default, resumable, audit rows blanked and the purge recorded | Tests (strict SDK fakes, both storage modes) | `purge.py`, `docs/trust.md` |
| Self-hosted option (2026-10-04; hosting by us stays the default): the firm runs it in its own Cloudflare account and Anthropic organisation, skipped by `deploy.yml`, deployed from a draft release built on a `v*` tag; `lra pause`/`resume` flip a D1 kill switch with no deploy; scoped, expiring support tokens; `lra tenant offboard` with a signed deletion certificate | Tests (fakes; the Worker switch in workerd); never run against a real account, no release tagged yet | `release.py`, `killswitch.py`, `offboard.py`, `cloudflare/src/pause.ts`, `docs/it/self-hosted.md` |
| The Project Falcon demo, 16 steps | Stub rehearsal passes | `demos/falcon/RUNSHEET.md` |

## What has not been run, and why it matters

1. **No model-backed session, ever, on the current code.** Every agent,
   skill script, grader rubric, webhook and memory store has run only against
   a scripted fake. The last live run (2026-09-21, Haiku, the old
   architecture) found six bugs no test had; the canaries on 2026-09-30 found
   five more in paths that did not need the model. Expect the same again.
   Since 2026-10-04 every fake is held to the SDK's types and documented
   limits (`tests/api_contract.py`), which found two real bugs (an agent
   update that dropped its MCP server list; archiving a session before it
   had stopped). What the types cannot settle is still open: how a Fable
   refusal appears inside a session, the server-side fallback to Opus,
   session metadata limits (8 or 16 keys), and the timing of the status lag.
2. **Nothing opened in Microsoft Word.** LibreOffice round-trips every file
   in CI. Comment threads and "Resolved" (turning comments) are the likeliest
   to differ in Word. `python -m evals.word_pack` writes the check files.
3. **No real document system.** The MCP server and vault flow were written
   from documentation.

## What to do next

**First, the live run.** An Anthropic organisation with credit and 30-day
data retention (Claude Fable 5.1 requires it), then `lra live-check`: it
applies the agents and skills, runs the eval and the triage scorecard, and
rehearses the Project Falcon demo, stopping at the first failure.

**For the design-partner firm:** `docs/onboarding/README.md` is the checklist, with what the firm signs, sends and what we configure, and each launch blocker's state.
For the firm's IT and security review: `docs/it/README.md`, the IT pack (questionnaire, architecture, mail flow, incident response) and `lra selftest`.
The firm's own deployment is built (`lra tenant new`, then `lra tenant provision <firm> --apply`, `lra live-check --tenant <firm>`; `docs/deploy-cloudflare.md`, "One deployment per firm"): run it once the firm's address and domains are known.

**Then, in code:** whatever the live runs find. After that, from
`docs/migration.md`: switch `TRIAGE` to shadow and then model once the
scorecard shows it beating the rules; long documents across several agents;
Sonnet with an Opus advisor if cost matters.

## History

What was built and found, newest first.

- *Production canaries through the Workflow* (2026-09-29/30). Every mechanical
  command was sent from Gmail to production and checked in the inbox: clean
  copy, review (mechanical-only, no credit), an answer ("30"), "undo 1", "6.1;
  clean copy" in one reply, renumber, sig pages, and the deadlines calendar.
  Found and fixed on the way: a degraded review did not record its
  conversation; Gmail's wrapped "On ... wrote:" line made "30" a twelve-word
  instruction (then 130 of 220 reply-format x command cases across real mail
  clients, now all passing); undoing an answer left its question closed; with
  no model, an instruction replied "something went wrong"; the deadlines
  extractor missed plain dated obligations. Test replies must be sent with
  Gmail's `replyToMessageId`; `replyThreadId` sends no reply headers.

- *A whole-product review and what it found* (2026-09-27). Five reviewers
  (reply voice, UX, features, robustness, check noise) and then fixes, all
  deployed:
  - Nobody outside the firm hears from the agent: strangers, usually opposing
    counsel on reply-all, are dropped silently at the edge and in the app.
    An empty allowlist means the firm's own domains in the app; at the
    Worker, which sees the mail first, it drops everything. Forged mail
    (DMARC fail, including a forged "pass" below the gateway's "fail") is
    dropped for every message, before the daily cap counts it.
  - Undo acts only on a clear reference: every tracked change is numbered
    from the ledger, "undo 2" works, and anything ambiguous gets the list
    back with nothing reversed.
  - The reply reads like an associate's note (verdict in the lawyer's words,
    blockers first with their question, "Your call", before → after edits,
    one footer reply that works); prompts and rubric say the same.
  - Deterministic checks: 0 false positives on three realistic clean
    agreements (were 5, 4, 14), Word auto-numbering read, every finding
    located by clause, new checks for [●]/NTD placeholders, words-vs-figures,
    weekday/date, duplicate definitions and more.
  - Security: agent-written memory held until the lawyer says "remember
    that"; DMS tools scoped to the matter; zip-bomb guard; associate files
    vetted; sessions always stopped and cleaned; dead-letter queue answers
    the sender; keyed store refuses plaintext.
  - Trust: redline author is REDLINE_AUTHOR ("Reviewer"), not the tool's
    name; a thread clean copy says which copy it is; replies come from
    "Redline Desk" with a contact card; the reason for each substantive
    change sits in the Word margin.
  - New: dates and deadlines table; "renumber" with cross-reference repair,
    proved by the checks; "B is fine" dismissal with learned suppression;
    their-paper mode with an issues list (needs the live model to judge);
    a question in a reply is answered; a slow review sends one "back in
    about N minutes" line; one-tap answer links (built, but ONE_TAP_LINKS is
    false in production: they need plus addresses, so the subdomain's Email
    Routing needs subaddressing or a catch-all to the Worker before they are
    switched on).

- *Time to a verdict and replies to a sendable copy* (2026-09-27). The
  local pipeline on a 100-page agreement went from 3.4 s to 0.56 s, most of
  it the search for an earlier version among the lawyer's conversations; a
  clean document no longer waits 3 s for a redline the session never wrote;
  the SDK loads only when a session runs, and the image ships dependencies
  in their own layer. A reply may carry several requests, "30; 2; clean
  copy", done in one pass and answered once, and the footer offers exactly
  that when answering settles every blocker, so draft to clean copy is one
  reply. A clean copy names any question still open in its first line. Not
  done, on purpose: an early mechanical-only email (two verdicts on one
  document), cutting the outcome grader's iterations (needs the live run),
  and "fix everything" (the unapplied items are judgment calls).

- *The cut-over to Anthropic Managed Agents* (DECISIONS 29, 2026-09-27). The
  review and the instruction agent are persisted agent definitions
  (`agents/*.yaml`, applied by `lra agents apply`); every review is a session
  with the document mounted read-only in Anthropic's sandbox, opened with an
  outcome whose rubric (`agents/review_rubric.md`) a separate grader scores,
  capped in dollars by the platform and in time by us. `report_findings` and
  the DMS lookups are custom tools answered over the event stream, so the
  lawyer's OAuth token never leaves our side; the stream is reconnected with
  history dedupe so a pending tool call cannot deadlock the session. The
  tracked-changes writer runs in the container as `redline.py` in our skill,
  and the handler re-verifies the file locally, runs the local writer for the
  manifest, and falls back to it if the session's file is missing or fails.
  Memory is read from Anthropic memory stores, one per scope, projected from
  our table. Deleted: the tool-runner loop, the sandbox and capabilities modules,
  the container-upload plumbing, the advisor and iteration-cap settings.
  `lra review --live` is the first-run command. Nothing here has run live.
- *Scanned PDFs, read by the model* (2026-09-27). A PDF with no text layer is
  transcribed by the model in one call, page by page, and the transcription
  builds the Word copy for the redline and the comparison. No OCR engine: the
  owner's decision is that reading goes through the model. The deterministic
  checks do not run on a transcription, on purpose: they would report its
  slips as the document's. The reply says the copy is a transcription to
  check against the scan. A transcription that fails (no credit, a refusal)
  leaves the scan what it was: reviewed from the pages, nothing marked up.

- *A Word redline for every input, compare across formats, and "since the
  version I saw"* (ROADMAP item 8). A PDF, a legacy .doc, an RTF, an .odt or
  a text file now gets its tracked changes in a Word copy converted or built
  from the document (`pipeline/reflow.py`), and a PDF also comes back with a
  note per finding on the page and the blockers highlighted
  (`pipeline/annotate.py`, pypdf). The same Word copy makes Word against the
  PDF the other side returned a real tracked-changes comparison, and lets
  "compare this to the one I sent" work with the archive off. A second review
  of a document the agent has seen leads with what changed since, with the
  comparison attached and the agent told where to dwell.
- *The playbook skill* (`skills/lra-playbook`, `docs/playbook.md`): Lito's
  headline feature. Twelve starter position files, each saying it is a
  starter; the review reports off-playbook clauses as questions and proposes
  the fallback wording only where it fits verbatim. `lra skills sync
  lra-playbook` uploads it; `SANDBOX_PLAYBOOK_SKILL_ID` attaches it.
- *Two decisions, recorded in DECISIONS 25 and 29:* the sandbox is on in
  production, and the review loop is moving to Anthropic Managed Agents with
  a cloud sandbox, the SDK tool-runner path to be deleted. Documents will be
  mounted in Anthropic's container and our tools, including the writer, run
  there as a skill; every returned file is re-verified locally before it is
  attached. Production security for a firm's documents is an open decision.

- *The other half of the learning loop* (`reconcile.py`). The BCC'd sent
  version is matched to the conversation it came from and read against the
  redline we gave: kept, dropped or rewritten, per change. Dropped changes feed
  the same promotion to suppression as an undo. The reply says what was learned
  in one line.

- *Thread state for comparisons.* A comparison now leaves the later version
  behind as the thread's document (`origin = compare`). A reply to it is read
  as instructions for a review of that document, so "look harder at clause 4"
  gets a review of clause 4 rather than "attach a .docx"; "thanks" gets
  silence; and from the first review on it is an ordinary review thread.

- *Punctuation and spacing* (`checks.punctuation`), the last "Partly" row in
  the parity table. Repeated words, a space on the wrong side of a mark and a
  missing space after a comma become tracked changes; double spaces and a
  stray straight quote among curly ones are counted, not written. With this
  the Litera Check scope is closed.

- *Usability, from a product review of the reply surface.* "Stop" is now
  remembered rather than promised. "Thanks" and "all accepted" get silence.
  "Stop flagging X" is confirmed and no longer also run as an edit; "flag X
  again" lifts it. The verdict names a heading that exists. Many tracked
  changes are counted by kind with the substantive ones named, and the footer
  offers "clean copy" as the one motion that accepts them all. `help` and
  `setup` are whole-message replies, the help text rides once on a lawyer's
  first review, and the "attach a .docx" refusal points at it. Follow-up
  replies carry an HTML body. The app's allowlist refusal names who to ask
  (`ALLOWLIST_CONTACT`); on Cloudflare it never fires, because the Worker
  drops an unlisted sender silently before the app sees it. And `MAIL_AGENT_ALIASES` keeps a forwarded address
  from reading as a BCC. Held back on purpose, until the live runs: dismissing
  a finding in one line, QUESTION mode, and framing the counterparty's draft
  as theirs (open question O17).
- *A code review of the day's earlier commits* found the time budget
  mislabelling a review that reported on the very turn that crossed the
  deadline (the SDK runs tools after the loop sees the turn), and seven shapes
  of real signature page that produced false findings: names with lowercase
  connectors, the English "for and on behalf of" under the signature, the
  American partnership By-chain, parties listed in a schedule, notice clauses,
  witness blocks, and plural or ranged schedule mentions in the covering note.
  All fixed, each pinned by a test built from the text that broke it.

- *The latency budget (DECISIONS 16) is enforced, and the redelivery question
  is answered.* A review could not run twice, but the redelivery of a review
  that outran the queue consumer's fifteen minutes was acknowledged as a
  duplicate, which left the still-running review with no retry if it then
  died. Three changes: a time budget checked between model turns, after which
  the model is told to report what it has and the reply says "Partly
  reviewed"; a heartbeat on the job row per turn, so `store.claim()` can tell
  a slow review from a dead one; and `retry_delay` on the queue consumer, so
  every redelivery waits out the lease, not only the ones the Worker code asks
  for.
- *Execution readiness* (ROADMAP item 7), the last Check-scope item. Signature
  page checks in `checks.signature_blocks` (a party with no block, a signatory
  who is not a party, one block half filled where the others are complete) and
  `email_checks.missing_annexes` (a schedule the document promises that is in
  neither the file, the other attachments nor the covering note, on execution
  versions only). All questions, never edits. Two clean signature pages, one
  American and one English, are in the corpus so the false-positive gate
  covers them; 16/16 catch, 0.00 false positives across 9 clean documents.
- *The BCC-on-send habit is safe to adopt* (open question O15). This is the
  coverage lever in `PRODUCT.md`: a mail rule copying the agent on every
  outgoing message makes the trigger not a decision. Audited: every reply in
  the codebase goes to the sender alone and nothing sets CC, now pinned by a
  test. Fixed: BCC'd on a message with nothing attached, including on a thread
  we once reviewed, is answered with silence rather than "could not find a
  document", and a plus-tagged copy of our own address (`review+M123@`) no
  longer reads as a BCC. Not yet done: the lawyer-facing setup note for the
  mail rule, and reading the sent version back (item 4).

**Not yet, on purpose:** sealing D1 rows (only when a firm asks), the tracker,
citation checking, redaction, and formatting comparison. The scope began as
Litera Check, Compare and Clean parity (2026-09-21) and was widened on
2026-09-26 to Litera and Kira features that fit `PRODUCT.md`; Kira at volume
and a Word add-in stay out.

## How to keep this page honest

Update it when something moves between "not run" and "run", when a row in the
table changes state, or when the next-up list changes. `tests/test_docs_references.py`
checks that every file this page names exists.
