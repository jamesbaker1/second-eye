# Build plan

> **Superseded.** This is the original build plan, kept for its reasoning.
> Parts of it no longer hold: the mail provider is Cloudflare Email Service,
> not AgentMail, and the review runs as an Anthropic Managed Agents session,
> not a tool-runner loop with an iteration cap. For where things stand read
> `STATUS.md`; for the order of work, `ROADMAP.md`; for what is settled,
> `DECISIONS.md`.

## The product in one paragraph

A lawyer is about to send a document to a client. Before sending, they forward
it to one address the whole firm shares. A minute later a reply arrives: a
verdict in the first line, the blockers, and the same document back with real
Word tracked changes. The agent checked it against the firm's precedents and the
other drafts on that matter, because it searched the document system using that
lawyer's own credentials. The interface is the address book.

## Why this can work

Four things have to be true. Two already are.

1. **The interface is free.** Email is the one tool every lawyer already has
   open, and attaching a document is a motion they perform dozens of times a
   day. We are not asking for a behavior change. We are inserting one recipient
   into a flow that already exists.

2. **The review is good enough.** Formatting, cross-references, defined terms,
   placeholders, internal inconsistency. Real, common, embarrassing, and
   mechanically checkable. Tractable today.

3. **The redline opens in Word.** Built, not yet proven. Phase 1 below is
   done and every output is round-tripped through an independent parser, but
   nobody has opened one of these files in real Microsoft Word. A findings memo
   is a commodity. A document that comes back with tracked changes a partner
   can accept one by one is the product.

4. **The agent can see the firm's own work.** Also not yet true. It is what
   separates "a language model read your contract" from "someone who knows how
   this firm drafts read your contract." Delegated OAuth is how it gets there
   without building a second permission model.

## Phases

### Phase 0 - Scaffolding (done)
Repo, domain models, mail provider abstraction, identity and entry-point
detection, the agent loop with five tools, three-layer memory, per-lawyer OAuth
with an email-delivered consent flow, an iManage adapter, prompts, and the
research written down.

Also done: the **deterministic check layer**. Placeholders, defined terms,
cross-references, numbering, amounts, dates, party names, and document metadata
leftovers, all in exact code rather than model calls. This is Litera Check
parity on the mechanics, and it runs before the agent so the agent is told what
was already caught. See `docs/litera-parity.md`.

The pipeline runs end to end locally via `second-eye replay samples/example.eml`.

### Phase 1 - Prove the redline (done)
The writer is built and tested. `pipeline/ooxml.py` emits native revision markup
at the lxml level, handles run splitting so a phrase spanning three runs with a
bold word in the middle survives with its formatting, allocates revision ids
that never collide with another author's, and refuses to act on an anchor that
is missing, ambiguous, or spans paragraphs. Nothing is attached without passing
structural verification.

27 writer tests cover split runs, table cells, headers, another author's
existing revisions, and every refusal path.

**Outstanding**: opening the output in Word, Google Docs and LibreOffice on real
firm documents. Structural verification is thorough but it is not the same as a
human opening the file, and that check has not been done.

The original plan for this phase, kept because the sample corpus is still worth
assembling. The strategy bullet is struck through because it was not the route
taken: `redline.apply` writes revision markup directly and never diffs, for the
reasons in `docs/redlining.md`.

- Collect ten real `.docx` files spanning the target corpus: an NDA, an SPA
  excerpt, an engagement letter, a memo, something with tables, something with a
  numbered schedule, something with existing tracked changes, something from a
  heavily styled Word template.
- ~~Implement `redline.apply`, starting with the clean-edit-then-diff
  strategy.~~ Done, by direct markup instead.
- Assert on every sample: the output opens in Word, Google Docs and LibreOffice;
  changes appear as `w:ins`/`w:del` attributed to the agent; headers, footers,
  numbering, styles and embedded objects survive; pre-existing tracked changes
  and comments from other authors are untouched.
- Make `verify_opens` gate every send. Degrade to memo, never send a broken file.

**Exit criterion**: ten of ten samples produce a redline that opens clean and
that a lawyer would accept changes from rather than re-key.

### Phase 2 - Connect the firm's documents
This is what makes the agent worth more than a generic reviewer.

- Confirm which DMS the firm runs and point the adapter at a real instance.
  Register the OAuth application and confirm the scopes.
- Prove the consent loop end to end: first document, standalone review with the
  offer, reply `connect`, click, later reviews see matters. Then `revoke`.
- Verify the wall. A lawyer not on a matter must get nothing from it, confirmed
  against a real instance with real permissions, not against our own logic.
- Turn on the deviation-from-standard-form finding. It is the single most
  reusable finding type in the product and it proves the DMS path is live.
- Add an audit log of every DMS call, attributable to the lawyer it was made as.

**Exit criterion**: a review that cites the firm's own precedent, and a
demonstrated failure to reach a matter the requesting lawyer is not on.

### Phase 3 - Make the review worth reading
- Build a labeled evaluation set: documents with planted defects. A placeholder
  left in, a broken cross-reference, a defined term used before definition, a
  date contradicting another date, a one-sided indemnity, a clause that deviates
  from the firm's form.
- Measure two numbers and only two: **catch rate** on planted defects and
  **false positive rate** on clean documents. False positives are the metric
  that kills this product, because every one has to be read and dismissed.
- Tune the system prompt, the tool descriptions, the `auto_apply` threshold, and
  the effort level against those numbers.

**Exit criterion**: catch rate above 90% on planted blockers, fewer than one
false positive per clean document.

### Phase 4 - Put it on an address
- Pick the mail provider. AgentMail is the default recommendation.
- Stand up the domain with SPF, DKIM and DMARC. Send from a subdomain so the
  main domain's reputation stays insulated.
- Deploy to firm-controlled cloud. Encrypt tokens and documents at rest with
  firm-held keys.
- Verify inbound signatures. Verify DMARC on senders, because a From header is
  forgeable and this agent acts on the sender's identity.
- Confirm the reply-to-sender-only rule holds under a CC'd client thread.
- Write the retention and confidentiality policy. This is a launch blocker, not
  paperwork.

**Exit criterion**: a lawyer forwards a real document from their phone and gets
a useful redline back without touching a terminal.

### Phase 5 - Close the learning loop
The reason full retention was worth its cost.

- Capture the version the lawyer actually sent, by BCC habit or DMS hook.
- Diff the agent's redline against it. Accepted changes reinforce, rejected ones
  suppress, per lawyer and firm-wide.
- Promote high-rejection categories into suppression memory automatically.
- Surface learned firm conventions somewhere a human can read and disagree.

**Exit criterion**: a measurable fall in false positives between month one and
month three, with no prompt changes.

### Phase 6 - Repair, not just report
Where Litera is genuinely ahead: fixing what it finds. Repair broken numbering
and legacy formatting rather than reporting it. Return a clean, metadata-free
client copy alongside the redline. Execution-readiness mode. Then counterparty
profiles and the firm-wide digest. See `docs/roadmap-ideas.md`.

## Sequencing rule

Phase 1 before everything. It is entirely possible to build a beautiful email
agent that produces redlines nobody will accept, and that product is worth zero.
Build the hard part first and find out.

Phase 2 before phase 3. Tuning the review before it can see the firm's documents
means tuning the wrong thing.

## Risks

| Risk | Why it matters | Mitigation |
| --- | --- | --- |
| Corrupted document | Catastrophic, it is a client file | `verify_opens` gates every send; degrade to memo |
| Leaking a document to the wrong recipient | Catastrophic and unrecoverable | Reply only to the sender, never reply-all, tested |
| Cross-matter leakage through the DMS | An ethics problem, not a bug | Delegated OAuth: the agent cannot fetch what the lawyer cannot fetch |
| False positives | Product gets ignored within a week | Phase 3 measures it as the primary metric; phase 5 drives it down |
| Hallucinated clause or precedent | Professional liability | Anchors quote the document verbatim; precedent claims cite a DMS document id |
| Sender spoofing | A From header is forgeable, and the agent acts as the sender | Verify DMARC on inbound; allowlist; never act on an unauthenticated sender |
| Privileged data retention | Client confidentiality, possible ethics issue | Firm-held keys, per-matter access control, audit log, deletion path |
| Agent runs away | Cost and latency | Iteration cap on the runner; a job that does not report fails loudly |
| BCC arrives too late | The document already reached the client | Separate reply template that reads as an incident report |

## What "done" looks like for v1

One address for the firm. Forward a `.docx`, get back a verdict email and a
redline that opens in Word with tracked changes attributed to the agent, citing
the firm's own precedent where relevant. Blockers caught. Fewer than one false
positive per document. Under two minutes end to end. No lawyer ever sees a
screen except the one consent page, once.
