# Roadmap

Read `PRODUCT.md` first. The organizing thesis is code review for legal
documents: the thing you run on everything you produce, always, without
deciding to. That thesis sets the order below. Anything that does not make the
review more universal, faster, or less wrong waits.

## What changed

Until now this was one capability: send a document, get a redline. The scope is
now sixteen capabilities plus a passive archive of everything the agent is
copied on. That is not the same product with more features. It is a different
product, and the architecture has to change to carry it.

Three structural additions make everything else possible.

**An intent router.** Every email used to mean "review this." Now an email might
mean compare, scrub, redact, file, benchmark, check citations, search my
archive, or show me the tracker. Something has to decide which, without asking
the lawyer to learn a command syntax. That is `pipeline/router.py`, which
exists and handles the unambiguous replies; everything else still defers to
the agent.

**An archive.** Every message the agent is copied on is stored and indexed. This
is what turns a document reviewer into something that knows what is going on,
and it is what the tracker is derived from. That is `archive.py`.

**A tracker.** Matters, counterparties, documents in flight, who owes whom what,
and what is overdue, all derived from the archive rather than maintained by
anyone. That will be `tracker.py`. It does not exist yet -- unlike the two
above, which do.

Everything else is a skill that plugs into the router.

## The thing to be careful about

The archive holds privileged client communications. That is a discovery target,
a breach target, and depending on jurisdiction and structure it can raise
questions about whether privilege is preserved when a third-party system holds
the correspondence.

This is not a reason not to build it. It is a reason to build it with the
constraints designed in rather than bolted on, and to get the firm's general
counsel to sign a written policy before it touches a live matter. The concrete
requirements are in `docs/archive.md` and they are launch blockers, not
paperwork.

DECISIONS 13 scopes when they bite. The first deployment is the owner, on
his own documents, where there is no third party whose privilege is at stake, so the
archive can be switched on for it. Everything in that table is still a blocker
before a second user or a live client matter, and nothing about a single-user
start makes the encryption or the signed policy less necessary later.

## Phase ordering

Two rules govern the order.

The tracked-changes writer comes before everything, because it is our equivalent
of committing a suggestion from a review comment, and a redline that has to be
re-keyed is worth nothing.

After that, the order is whatever makes the review run on more documents, run
faster, or be wrong less often. Depth waits for coverage.

### Phase A - The spine

1. **Intent router.** **Done.** `pipeline/router.py`. Deterministic routing for
   whole-message replies, quote and signature stripping, and auto-mail
   detection so the agent never answers a bounce, an out-of-office or a list.
   Free-form instructions are carried out by `pipeline/instruct.py`.
2. **Archive with full-text search.** **Done.** `archive.py`, off by default.
   Per-lawyer scoping and an access log are enforced in code. Its production
   requirements in `docs/archive.md` remain outstanding.
3. **Thread state and a change ledger.** **Done, and more important than the
   tracker.** `thread.py`. The ledger is the source of truth and the redline is
   regenerated from the original every round, which is what makes undo safe.
4. **Tracker.** Not started. Derived state, never hand-maintained.
5. **Skill interface.** Not started. Deferred until there are enough skills to
   justify the abstraction.

### Phase B - The skills that reuse what exists
These are close to free once the spine is in, because the redline engine, the
extractor and the check layer already exist.

**Done ahead of this phase**, because an audit or a direct question made them
urgent:

- **The associate loop, complete.** A reply resolves to its conversation and
  acts on it: a one-word answer becomes a tracked change, "undo the X change"
  reverses that one, "undo everything" returns the original, and a free-form
  instruction is carried out. "Add a force majeure clause after 7" inserts a
  tracked paragraph; "take the cap off the indemnity" is done, with the concern
  stated once first. `followup.py` and `pipeline/instruct.py`.
- **PDF review.** Text extraction with pypdfium2, plus the file handed to the
  model directly so it sees layout, tables and signature pages. A scan with no
  text layer is now reviewable rather than refused.
- **Findings that fix themselves.** Party-name drift and mixed date formats are
  written as tracked changes; everything certain-but-ambiguous carries a
  one-word question instead of a lecture. Went from 5% of findings becoming an
  edit to 33% edited and 48% answerable.

5. **Compare two versions.** **Done.** `pipeline/compare.py` and `versions.py`.
   Tracked changes in the earlier version, proved by re-reading the output:
   accept-all must give the later version and reject-all the earlier, or
   nothing is attached. `compare.explain` says which changes carry risk.
   The original note: Two attachments, or one attachment and "compare to
   the last one you sent me," which needs no second attachment because the
   archive has it. Highest value per unit of work on the list.
6. **Clean copy.** **Done.** `pipeline/clean.py`. Original note: Strip tracked changes, comments, hidden text and author
   metadata, return a client-ready file alongside the redline, with a list of
   exactly what was removed.
7. **Execution readiness.** **Done**, deterministically. `checks.signature_blocks`:
   every party in the parties clause has a signature block, nobody signs who is
   not a party, and a block half filled in where the others are complete. All
   three are questions, not edits. `email_checks.missing_annexes`: a schedule or
   exhibit the document refers to that is in neither the file, the other
   attachments nor the covering note, on execution versions only. Blanks and
   date consistency were already covered. Original note: Signature blocks
   present and correct, every exhibit and schedule attached and
   cross-referenced, no blanks, dates consistent, right entities.
8. **What changed since last time.** **Done.** On a second review of a document
   the agent has seen, the reply leads with "Since the version I reviewed on
   <date>": the count of changes, how many the risk assessment marks worth
   attention, up to five named with before and after, and the comparison
   attached as `<stem> (changes since last time).docx`, proved by accept-all
   and reject-all like every comparison. The earlier version comes from the
   lawyer's own conversations (`reconcile.conversation_for`), not the archive,
   so it works on a default deployment; a by-name match is held to the text
   similarity bar so two clients' "NDA.docx" are never compared. The review
   agent is told what moved in its own fenced block and still reviews the
   whole document: the original note said "instead of re-reviewing
   everything", and that was wrong, because a clause that did not change can
   still be wrong and a narrowed review that missed one would be reported as
   a full one. A resend, a document never seen, and any failure inside the
   comparison leave the review exactly as it was. `handler._since_last_time`.

### Phase C - The skills that need something new
Each of these needs one external thing we do not have yet.

9. **Citation checking.** Two jobs: build the table of authorities, and verify
   every case cited exists and says what it is cited for. The second is worth
   more than the first, because courts are sanctioning lawyers over fabricated
   citations right now. Needs a case-law lookup source.
10. **Redaction.** Personally identifying information, privileged passages,
    commercially sensitive terms. Returns a redacted copy where the redaction is
    real removal, never a black rectangle over live text.
11. **Formatting repair.** **Done for fonts, styles and spacing**, in the
    code-execution container with a local gate (`pipeline/repair.py`,
    `docs/sandbox.md`). Numbering is deliberately not rebuilt: it is the one
    thing the gate cannot check. Original note: Rebuild numbering, normalize styles, fix legacy
    formatting. We detect these already; repairing is strictly more useful and
    every change is tracked so it stays safe.
12. **Clause benchmarking.** "How does this term compare?" answered against the
    firm's own corpus, which is better than market average because it is what
    this firm actually accepts.

### Phase D - The skills that write somewhere else
All of these are a one-word reply, and each needs a write scope or an
integration we deliberately deferred.

13. **File it.** Write the redline back to the matter. One OAuth scope away.
14. **Capture the time.** Every email to the agent is evidence of billable work.
    Draft the entry; never submit it.
15. **Send for signature.** The agent has the document and knows the parties.
16. **Check conflicts.** Screen a counterparty before a document goes to them.
    High value, high risk, needs the firm's conflicts database.

### Phase E - The things only the archive makes possible
This is where the product stops being a tool and starts being a colleague.

17. **Search your own email.** "What did I send Acme about the indemnity cap?"
    Answered from the archive, scoped to that lawyer, in a reply.
18. **The tracker.** "Where are my deals?" One email, one table: every matter,
    what is in flight, who owes what, what is overdue.
19. **Negotiation ledger.** Read the quoted thread. If opposing counsel asked
    for three changes and the document makes two, say so.
20. **Precedent on request.** "I need an NDA for an Ohio manufacturing client"
    with no attachment, answered with the three closest matches from the firm's
    own work.
21. **First draft from the firm's form.** "Draft me an NDA for the Acme deal."
    The natural next step after precedent on request, and the largest piece of
    junior work not otherwise on this page. Always starts from the firm's own
    form or closest precedent, never from a blank page, and every departure from
    that form comes back as a tracked change with the reason stated. That keeps
    it the same shape as everything else here: the lawyer reviews a redline, the
    engine is the one already built, and nothing about liability moves.
22. **Weekly digest.** What the agent caught, what is overdue, what went quiet.
23. **Blunter after hours.** Documents sent late get a more careful read and a
    less diplomatic verdict.

### Phase F - Acting on your behalf (long term, deliberately not yet)

Everything above stops at the document. This phase is where the associate stops
being someone who hands work back and becomes someone who gets it done.

The architecture for it already exists. Per-lawyer delegated OAuth was built for
the document system in `src/secondeye/oauth.py`, and the agent's tools are constructed
per request from that lawyer's own tokens. Adding a system means adding a scope,
an adapter, and a tool. Nothing structural changes.

**The systems worth connecting, in the order they earn their keep:**

| System | What it unlocks | Scope shape |
| --- | --- | --- |
| Document system, write | File the redline to the matter. Close the loop that already half exists | Write, on top of the read scope already there |
| Calendar, read | Cross-check every date and notice period in a document against the real deadlines on the matter | Read only |
| Mail, read | Answer "what did I agree with Acme about the cap?" from the lawyer's own correspondence rather than a separate archive | Read only |
| E-signature | "Send it for signature" as a reply. The agent has the document and knows the parties | Create envelope, never send without confirmation |
| Mail, send | Draft the covering email to the client. Draft, never send | Compose and draft only |
| Practice management | Draft the time entry for work the agent can see happened | Draft only |
| Entity registries | Verify a party's legal name is real and correctly spelled. Public data, no delegation needed | None |

**The rules this phase has to be built under.** These are not optional and they
are the reason it is not being built yet.

1. **One consent per system, never a bundle.** A lawyer who granted document
   search did not thereby grant mailbox access. Each scope is asked for when it
   is first useful, in a reply to something they sent, with a plain sentence
   saying what it enables.
2. **Read before write, always.** Every system is connected read-only first.
   Write access is a second, separate grant, asked for only once the read side
   has been useful for a while.
3. **Nothing irreversible without a confirmation in the thread.** Filing to a
   matter is reversible. Sending an envelope for signature is not. The line is
   whether an outside party sees it, and anything that crosses that line gets an
   explicit yes first, every time, with no remembered preference to skip it.
4. **Drafts, not sends.** For mail in particular the agent composes and leaves it
   in drafts. The lawyer presses send. This is the single most important line in
   this phase, because it keeps a human between the agent and every counterparty.
5. **Revocation stays one word.** "Revoke" already disconnects everything. That
   must remain true however many systems are attached.

**The reason this waits.** An agent holding a lawyer's send-mail and e-signature
scopes, whose prompt includes the text of documents sent to it by outsiders, is a
prompt-injection target with real consequences. A document that arrives from
opposing counsel can attempt to instruct the agent. Today the worst case is a
misleading review. With write scopes the worst case is a document filed to the
wrong matter or an envelope sent to the wrong party.

So this phase does not begin until three things are true: the review itself is
trusted and in daily use, there is an audit log of every action the agent takes
attributable to the lawyer it acted as, and there is a tested boundary between
document content and instructions. The last one is the hard part and it belongs
on the list before any of these scopes are requested.

### Stretch goal - Legal research

The destination is an associate you can email anything you would ask a junior
(DECISIONS 20 and 26), and a junior does research. So this is no longer
excluded. It is a stretch goal: last, after everything above, and not allowed to
pull work forward from anything above.

It was excluded for a good reason and the reason has not gone away. Research
reintroduces the liability that was removed when the agent was scoped to review
documents rather than advise on transactions, and that line is what makes
running without a supervising-attorney queue defensible (DECISIONS 5). So it
ships under its own rules or not at all:

1. **Every proposition carries a citation, and every citation is verified** to
   exist and to say what it is cited for, by the same case-law lookup that
   citation checking (item 9) needs anyway. A proposition the agent cannot
   source is reported as unsourced, never stated.
2. **Labeled as research, never advice.** It reports what the authorities say.
   It does not say what the client should do.
3. **DECISIONS 5 is reopened for this skill only.** A real junior's research
   memo is read by someone senior before anyone relies on it. Whether the
   agent's needs the same is decided when this is built, not assumed from the
   review path.

The preconditions are the same shape as phase F's: the review is trusted and in
daily use, and citation checking has been running long enough to know its
false-verification rate. An agent that fabricates one authority ends the
product, not the feature.

## What is deliberately excluded

Contract lifecycle management, due diligence at volume, and eDiscovery. All
three are the wrong trigger at the wrong moment, and email is inherently one
document at a time. "Summarize these twelve leases into a table" fits in an
email and is fair game; a data room does not and is not.

Legal research used to be on this list. It is now the stretch goal above.

## Where this actually stands

Built and tested: the tracked-changes writer, the deterministic check layer,
the agent loop, per-lawyer delegated OAuth, the mail abstraction, file
identification, the archive, thread state with undo, the associate loop, PDF
review, and an OOXML validator.

Measured: 100% catch and 0.00 false positives per clean document on a
seventeen-document synthetic corpus at the time; `STATUS.md` has today's
scorecard and test counts.

Audited: 58 agents across eight dimensions with adversarial verification raised
125 findings, 113 of which survived refutation. The document-damaging and
security findings are fixed; the remainder are tracked below.

Also built since: document comparison with risk assessment, clean copies,
formatting repair, a custom Agent Skill that puts the comparison, clean and
check code into Anthropic's container (`skillsync.py`), house style reaching the
prompts, and the conversational half of the learning loop. `STATUS.md` has
the current test count and corpus.
`docs/litera-parity.md` has the comparison against Litera Check, Compare and
Clean that these were built to close.

Built for a design-partner deployment: Cloudflare end to end (`cloudflare/`,
`Dockerfile`, `docs/deploy-cloudflare.md`). Email in and out through the Email
Service, a queue between them, the application in a container, rows in D1
through a sqlite3-shaped adapter (`d1.py`), documents in R2 sealed with a key
the firm holds (`blobs.py`, `crypto.py`), retention that finally reaches
conversations and their documents, and `second-eye purge <address>`. The whole suite
runs in both storage modes, and the storage layer was run against the real
Worker under Wrangler's D1 and R2 emulators.

Deployed on 2026-09-21 at the owner's own review address, for one
allowlisted sender. The first real message, a clean-copy request, went email to Worker to
queue to a cold container to D1 and R2 to a reply in the inbox in fourteen
seconds. That proves the image build, the container reaching its Worker, and
the Email Service in both directions. It does not prove a model-backed review
in production, because the Anthropic account had no credit on the day.

Found by that first message, and fixed: the reply arrived as a new conversation
in Gmail rather than under the email it answered. Gmail threads on the subject
as well as the References header, and we appended the verdict to the subject.
Every provider had this; nobody had looked at a real inbox. The subject is now
the lawyer's own with "Re:", and every reply opens with its verdict in the
body, including instruction replies, whose verdict had lived only in the
subject.

Not built: the tracker, numbering rebuild, citation checking, redaction, and
everything in phases D, E and F. (Execution readiness is built; see STATUS.)

Never run: the output has not been opened in real Microsoft Word. Structural
validation is thorough and is not the same thing. `python -m evals.word_pack`
now writes one file of each kind we produce and a checklist of what to try in
each, so closing this is twenty minutes of somebody's time rather than a
project.

Run live for the first time on 2026-09-21, on Claude Haiku 4.5: a full review
with a redline, a comparison with its risk assessment, a three-message
conversation (review, instruction, undo), `second-eye skills sync`, and a formatting
repair in the container that passed the local gate. Six bugs that no offline
test could see, because every one of them lives in the conversation with the
real API, were found and fixed on the way:

- both report tools were declared strict with `list[dict]` arguments, which the
  API rejects, so no review or instruction had ever got past its first request;
- the instruction agent did not stream, and the SDK refuses to start a
  non-streamed request with that output budget;
- `sandbox.collect_files` looked one level into a response and the file id is
  two levels down, so every file the container wrote was reported as missing;
- replies sent later in a conversation were never registered against it, so the
  third message of any thread was lost without a provider thread id;
- thinking and effort were sent to Haiku, which accepts neither;
- an API key not scoped to a workspace needs a header nothing could supply.

Not yet run live: the whole Managed Agents path (`managed.py`, DECISIONS 29),
which replaced the tool-runner loop and the capability menu that were also
never run live. The account ran out of credit first. It is written against the
documented shapes and tested against a scripted fake of the platform only,
which is exactly the state the six bugs above were in.

Haiku is good enough to prove the plumbing and not good enough to ship: it
repeats findings the mechanical pass already made, and it called a deleted
notices clause "conforming". Judgment needs the model in `.env.example`.

### Known findings not yet fixed

From the audit, in rough order of how much they would hurt:

- On the webhook path (AgentMail, Postmark), FastAPI BackgroundTasks still
  loses a review on restart. The Cloudflare path does not: the message stays on
  a queue until the review has been answered for, and `store.claim()` now lets
  a redelivery take over a job whose worker died (`docs/deploy-cloudflare.md`).
  The lease is in `store.py` and works on any database; nothing redelivers to
  it on the webhook path yet.
- The rows in the database are not encrypted under the firm's key, only the
  documents and the document-system tokens are (`crypto.py`). Findings and the
  text of each tracked change quote the document, so they are client material
  sitting in D1 under Cloudflare's encryption alone. `docs/deploy-cloudflare.md`
  says so plainly; sealing those columns is the next step if a firm asks.

Found by replaying real `.eml` bytes end to end (`tests/test_replay_end_to_end.py`)
and fixed: a review that degraded to mechanical findings because the model was
unreachable never recorded its conversation, so the lawyer's reply to it was
answered with "I could not find a document to review".

Fixed since the audit, listed because the shape of each is worth remembering:
the sent version, when BCC'd, is read against our redline and every change is
scored (`reconcile.py`); a comparison leaves the later version behind as its thread's document, so a
reply to one is a review of that document with the reply as instructions;
the latency budget is enforced between model turns and a review cut short says
so rather than looking complete; a failed send no longer reports a failed review; `max_tokens` no longer shares
its budget with adaptive thinking; the document carries its own cache
breakpoint instead of being re-sent at full price each iteration; a second
`report_findings` call supersedes the first; ALLOWED_SENDERS domains are no
longer treated as inside the firm; the redline gate reads the bytes rather than
the filename; AgentMail computes attachment sizes itself and logs the signature
shape it could not match; and CI calls the venv directly rather than through
`uv run`, which was silently uninstalling ruff and pytest.

One audit finding was closed by declining it. The deterministic pass has no
check for a capitalized term that is used but never defined, and is not
getting one: every document is full of capitalized phrases that are correctly
undefined, so the rule would fire on all of them. It is the agent's job, and
`docs/litera-parity.md` now says so rather than claiming the check.

## How to know it is working

One number per phase, and only one.

| Phase | The number |
| --- | --- |
| A | Percentage of emails routed to the right skill with no clarifying reply |
| B | Percentage of redlines accepted rather than re-keyed |
| C | False positives per clean document |
| D | Actions completed by reply, without anyone opening a browser |
| E | Questions answered from the archive that the lawyer would otherwise have searched for themselves |

Above all of them sits the number from `PRODUCT.md`: **what fraction of the
documents this firm sends were reviewed at all.** A mediocre review on
everything beats an excellent review on the tenth of documents somebody
remembered to check. If coverage is not climbing, nothing else on this page
matters.
