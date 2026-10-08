# The IT pack

For a law firm's IT, security and risk teams deciding whether to let Redline
Desk handle the firm's documents. Everything here is written against the code
as it is on the date below, not against a roadmap. Where something is not
built, not verified, or not true yet, it says so in the same sentence. Where a
claim rests on a vendor's documentation, it links the page and the date it was
read.

Last checked against the code: 2026-10-04.

| Page | What it answers |
| --- | --- |
| This page | What it is, the data flow, where data lives, who can reach it, what the firm controls, and what does not exist yet |
| [architecture.md](architecture.md) | Every hop, protocol, store, key and trust boundary, as a diagram and as a list; how one firm is isolated from another |
| [subprocessors.md](subprocessors.md) | Cloudflare, Anthropic and GitHub: what each receives, where, their certifications (linked), and a change-notice commitment to sign |
| [questionnaire.md](questionnaire.md) | Pre-filled answers to the SIG Lite and CSA CAIQ v4 / AI-CAIQ questions a strict firm sends, by domain, with every "no" said plainly |
| [mail-flow.md](mail-flow.md) | Exact setup for Exchange Online (BCC rule scoped to a pilot group), Purview, Mimecast, Proofpoint and Google Workspace, and what not to do |
| [incident-response.md](incident-response.md) | Detection, the kill switch, key withdrawal, purge, breach notice, and offboarding |
| [ai-policy-crosswalk.md](ai-policy-crosswalk.md) | ABA Formal Opinion 512, the SRA's AI guidance, and bank and insurer outside-counsel guidelines, mapped to the controls that meet them |
| `lra selftest` | A command IT runs against the deployment for a dated PASS/FAIL report: see "Check it yourself" below |

The general counsel's note is `docs/onboarding/general-counsel.md`; the
lawyer-facing trust page is `docs/trust.md`. This pack is the technical
counterpart to both.

## One page for the CISO

**What it is.** A legal review agent at an email address
(`review@legal.<firm>.com`). A lawyer emails or BCCs it a draft; it replies
to that lawyer alone with findings and a Word file with tracked changes. No
software is installed at the firm, there is no plug-in, no inbound
connection into the firm's network, and no web console. One deployment
serves one firm.

**The data flow, in five lines.**

1. The lawyer's mail server sends the message (SMTP) to Cloudflare Email
   Routing for the agent's subdomain.
2. A Cloudflare Worker drops anything not from an allowlisted sender, or
   marked DMARC-fail, then seals the raw message with the firm's key
   (AES-256-GCM) into R2 and starts one Cloudflare Workflow for it.
3. The Workflow calls a Python container (Cloudflare Containers), which runs
   the deterministic checks and opens one Anthropic Managed Agents session
   with the document mounted (HTTPS to `api.anthropic.com`).
4. The container reads the session's findings and file back, verifies the
   file, and stores the conversation (rows in D1, the document sealed in R2).
5. The Worker sends one reply through Cloudflare's Email Service, to the
   sender only, enforced twice.

**Where data lives, and for how long.** The windows are the ones in
`docs/trust.md` ("What we keep, and for how long"), the product's defaults
since 2026-10-04; the firm can shorten any of them, and a daily sweep
(`src/lra/retention.py`, run by the Worker's cron) deletes each item within a
day of its window ending.

| Data | Where | Under the firm's key? | Kept |
| --- | --- | --- | --- |
| The incoming message | Cloudflare R2, the firm's bucket | Yes | Until answered; 7 days if never (R2 lifecycle rule) |
| The conversation, its document and earlier versions, findings, tracked changes, negotiation record | R2 (documents) and D1 (rows) | Documents yes; rows **no** (Cloudflare's own encryption at rest only) | 7 days after the last reply or new version (`THREAD_RETENTION_DAYS`) |
| A closing and its signed pages | R2 and D1 | Pages yes; rows no | 7 days after the last email on it |
| A note the agent proposed that the lawyer did not confirm | D1 | No | 7 days, never used |
| Which changes were kept, undone or dismissed | — | — | Not recorded (`LEARN_FROM_OUTCOMES` off) |
| The job row (sender, status) | D1 | No | 24 hours (`RETENTION_HOURS`) |
| The mail archive | R2 and D1 | Attachments yes; search index no | Off; 7 days if switched on |
| What a lawyer asked it to remember | D1 and Anthropic memory stores | No | Until the lawyer or a purge removes it |
| The audit trail (names, times, ids; no contents) and purge records | D1 | No | Until the firm deletes them; a purge blanks its scope |
| Workflow step state: filenames, the "reviewing X" notice, session ids | Cloudflare Workflows | No | With the Workflow instance (see `architecture.md`) |
| The document in review, the instructions, memory for this lawyer and client | Anthropic (US; there is no EU or UK option) | No (Anthropic's own encryption; CMEK exists but is not set up) | Deleted when the review ends; see below |

**Retention at Anthropic.** Deleted when the review ends; see `docs/trust.md`.
The files we upload and the files a session writes are deleted when the
session ends, and the session itself, with its turn-by-turn record, is
deleted when the review ends (`src/lra/managed.py`); a delete that failed is
retried by the daily sweep once the job is 7 days old. Anthropic's own
backend retention for inputs and outputs is up to 30 days, and its
documentation does not say whether anything of a deleted session stays
within that time, so up to 30 days is what can be promised (`docs/trust.md`,
"Anthropic's retention"). Memory stores (short notes, not documents) stay
until deleted. Managed Agents is not eligible for zero data retention, and
`ZERO_RETENTION=true` does not change that: it moves the model, not the
sandbox.

**Who can access it.**

- *Send to it:* only addresses or domains on `ALLOWED_SENDERS`, matched
  exactly. Everyone else is dropped silently, before any code that costs
  money runs.
- *Receive from it:* only the sender of the message being answered
  (`REPLY_POLICY=sender_only`, enforced in the container and again in the
  Worker).
- *Administer it:* whoever holds the Cloudflare account, the Anthropic
  organisation, the GitHub repository (a push to `main` deploys every
  firm), and the firm's `DATA_KEY`. Today that is the founder, unless the
  firm runs it in its own Cloudflare account and Anthropic organisation
  (`lra tenant new --account-id`). There is no web admin surface to put SSO
  in front of.
- *Read stored documents:* only someone holding `DATA_KEY` and the storage.
- *Machine-to-machine:* the container reaches the Worker's `/internal/*`
  endpoints with one bearer secret (`EDGE_SECRET`). Those endpoints are
  currently on the public `workers.dev` hostname; taking them off the
  internet is in progress (`architecture.md`, "Known exposures").

**What the firm controls** (each a setting in the firm's
`deployments/<firm>/tenant.jsonc`, so every change is a reviewable diff):
the allowlist; reply policy; the no-AI client list (`NO_AI_MATTERS`, plus
"no AI for Acme" by an admin's email); retention; the archive (off);
web search (off); inference geography (`us` or global); the privilege
legend; the daily message cap; the kill switch (`SERVICE_PAUSED`); who the
admins are; client memory walls (`lra clients`); the audit export
(`lra audit`); deletion by client, matter or lawyer (`lra purge`); and the
key itself. In the firm's own accounts, the firm can also cut it off with no
help from us: disable the transport rule, delete the Email Routing rule, or
revoke the Anthropic key.

**What we do not have yet.** Said here so nobody finds it later:

- No SOC 2, ISO 27001, Cyber Essentials or third-party penetration test of
  Second Eye itself. Cloudflare's and Anthropic's attestations cover their
  platforms, not our code (`subprocessors.md`).
- No signed DPA, pilot agreement or MSA in this repository.
- D1 rows are not sealed under the firm's key.
- The internal endpoints are on the internet behind one secret (fix in
  progress); the container's own egress is not restricted.
- The DMARC check drops only an explicit `dmarc=fail`; requiring a pass for
  allowlisted domains is in progress.
- The kill switch needs a config change and a deploy, not a button.
- No SIEM feed; audit export is a CSV or JSON on request.
- No key re-seal command: `DATA_KEY_PREVIOUS` reads old data, so rotation
  is possible but retiring the old key is not.
- No branch protection on `main` (the private repository's GitHub plan does
  not offer it), and a push to `main` deploys every firm.
- No EU or UK model processing: Anthropic's workspace geography is US only.
  D1 and R2 can be EU.
- The model-backed review has not yet run live on the current code
  (`STATUS.md`).

The full list, with plans, is in `questionnaire.md`; items marked
**[Founder decision]** there need a commercial or contractual decision, not
code.

## Check it yourself

```bash
lra selftest --tenant <firm> --env-file <the firm's secrets file>
```

It reads the firm's settings and prints a dated report, one line per check
(PASS, WARN, FAIL, INFO, SKIP) with the evidence: the allowlist is set and
holds no public mail domain; a stranger, a lookalike domain and a subdomain
are each dropped by the Worker's own rule; `REPLY_POLICY`; `DATA_KEY` is a
32-byte key and not one of this repository's test keys; the retention values;
the archive is off; the kill switch's state; web search is off; the inference
geography; the firm's deployment shares no Worker, database, bucket, agent or
memory store with any other; the Worker's `/health`; that `/internal/*`
refuses a request without the secret; that the OAuth callback is closed; that
every agent id is retrievable with the firm's own Anthropic key (so it is in
the firm's organisation) and the sandbox's egress is limited; and that the
audit export reads. `--wrangler` also lists the Worker's secret names.
`--offline` skips the network checks; `--json` for a machine. It exits 1 on
any FAIL.

It is read-only: it makes no model call (Anthropic objects are only
retrieved), sends no email (the stranger check runs the Worker's rule, which
a test holds to the Worker's source), and writes nothing.
