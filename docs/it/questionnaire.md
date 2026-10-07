# Security questionnaire, pre-filled

Answers to the questions a strict firm sends, grouped by domain. The
questions are paraphrased from the topics of the Shared Assessments **SIG
Lite** and the CSA **CAIQ v4** (CCM v4 domain codes in brackets, for
example `[IAM]`) and the CSA **AI Controls Matrix / AI-CAIQ**
([CSA AICM](https://cloudsecurityalliance.org/artifacts/ai-controls-matrix-v1-1)).
They are not the licensed question text; map them onto your own form.

Every answer is true of the code and the operation on 2026-10-04. Each row
is one of: **Yes**; **Partly** (what is and is not done); **No** (with the
plan); **N/A** (with why); or **[Founder decision]** (needs a commercial,
contractual or spending decision, not code). "We" is Redline Desk, which is
one founder. "Customer-hosted" means the firm runs the deployment in its own
Cloudflare account and Anthropic organisation.

Run `lra selftest --tenant <firm>` for live evidence of the configuration
answers (`README.md`, "Check it yourself").

## 1. Governance, risk and compliance `[GRC]` `[A&A]`

| Question | Answer | Status |
| --- | --- | --- |
| Do you hold SOC 2 Type II, ISO 27001 or Cyber Essentials (Plus)? | None, for Redline Desk itself. Cloudflare and Anthropic hold theirs for their platforms (`subprocessors.md`); those do not cover our code. Plan: readiness through a compliance platform toward SOC 2 Type I, and Cyber Essentials Plus first for a UK firm. | **No** · **[Founder decision]** (cost and order) |
| Has an independent penetration test been performed in the last 12 months? | No. The known findings a tester would report first (internet-reachable `/internal/*`, the DMARC rule, open container egress) are being fixed first (`architecture.md`, "Known exposures"), then a third-party test. | **No** · **[Founder decision]** |
| Do you have a written information security policy, and who owns it? | No standalone policy document. The security rules that bind the product are written into the code and its docs (`docs/trust.md`, DECISIONS 28 and 31, this pack) and pinned by tests. Owner: the founder. | **No** (policy set to be written) |
| Do you publish a list of sub-processors and notify changes? | Yes: `subprocessors.md`. The notice commitment is a template clause awaiting agreement. | **Partly** · **[Founder decision]** (notice period) |
| Is there a DPA with SCCs / UK IDTA? | Not drafted. Anthropic's DPA (SCCs Modules 2 and 3, UK Addendum) covers the Anthropic leg; ours is not written. Customer-hosted, our role reduces to a software supplier with optional, time-boxed support access. | **No** · **[Founder decision]** |
| Is there cyber and technology E&O insurance? | Not confirmed in this repository. | **[Founder decision]** |
| Do you assess your own vendors? | Cloudflare and Anthropic were chosen and are documented with their trust pages and retention terms (`subprocessors.md`, `docs/sandbox.md`). No formal vendor-risk programme. | **Partly** |
| Is a risk register maintained? | Design risks are recorded as decisions (`DECISIONS.md`) and the open security items are listed in `README.md` and this page. No formal register. | **Partly** |

## 2. Asset management and data inventory `[DSP]` `[DCS]`

| Question | Answer | Status |
| --- | --- | --- |
| Is there an inventory of systems that process customer data? | Yes: per firm, one Worker, one container application, one D1 database, one R2 bucket, one Workflow, the Anthropic agents, environment and memory stores. Named in `deployments/<firm>/tenant.jsonc` and checked by `lra tenant check`. | **Yes** |
| Is there a data inventory and flow diagram? | Yes: `architecture.md`, every hop and store. | **Yes** |
| Is customer data classified and labelled? | All of it is treated as privileged. Replies carry the firm's privilege legend and an `X-Privileged` header for the firm's own DLP and journaling rules (`PRIVILEGE_NOTICE`). | **Yes** |
| Is customer data segregated from other customers? | Yes: one deployment per firm, sharing nothing but code; enforced by `lra tenant check` and reported by `lra selftest` (`architecture.md`, "Per-firm isolation"). | **Yes** |
| Do you hold data on end-user devices? | Customer documents: no. The operator's machine holds `deployments/<firm>/.env` (the firm's `DATA_KEY`, `EDGE_SECRET`, Anthropic key) after provisioning, and is told to move the key to the firm's vault (`src/lra/tenant.py`, BACKUP). Today the only copy of the founder's own production key is on his laptop, and backing it up is on his to-do list. | **Partly** |

## 3. Identity and access control `[IAM]`

| Question | Answer | Status |
| --- | --- | --- |
| How do end users authenticate? | By sending from an allowlisted address (`ALLOWED_SENDERS`, exact address or exact domain) whose domain does not fail DMARC. No passwords, no accounts, no web login. | **Yes** (see DMARC below) |
| Is spoofing of an allowed sender prevented? | Partly. A message whose `Authentication-Results` shows `dmarc=fail` or `compauth=fail` is dropped. A domain with no DMARC record, or a `none` result, is not dropped. Requiring a DMARC pass for allowlisted domains is **in progress**. A firm whose domains publish `p=reject` is covered by its own policy today. | **Partly** |
| SSO / SAML / SCIM? | N/A: there is no user interface. Joiners and leavers are the allowlist; with a domain entry, the firm's directory already decides who can send from that domain. Scoping to a pilot group is done in the firm's transport rule (`mail-flow.md`). | **N/A** |
| Is MFA enforced for administrative access? | Administrative access is to Cloudflare, Anthropic and GitHub accounts. MFA on the founder's accounts is not evidenced in this repository. Customer-hosted, the firm's own policies apply to its accounts. | **[Founder decision]** (attest and evidence) |
| Who at the vendor can access customer data? | The founder, who holds the deployment's Cloudflare and Anthropic access and, after provisioning, a copy of `DATA_KEY`. No other staff. Customer-hosted, standing access can be removed: the firm issues a scoped, time-boxed Cloudflare token when support is needed. The support-access procedure itself is not written. | **Partly** · **[Founder decision]** |
| Least privilege between components? | The container holds no Cloudflare API token; it reaches D1, R2 and mail only through the Worker with one bearer secret. That secret opens every internal endpoint, including arbitrary SQL; narrowing it is part of the in-progress move off the internet. | **Partly** |
| Are privileged actions logged? | Deploys: every change is a commit on `main` and a GitHub Actions run. Purges: each leaves a content-free record (`lra purge --history`). Configuration: a diff to `tenant.jsonc`. Console actions in Cloudflare or Anthropic: their own audit logs, not collected by us. | **Partly** |
| Leaver process? | Remove the address from `ALLOWED_SENDERS` (a commit and deploy), then `lra purge --lawyer <address>`. Not automated from the firm's directory. | **Partly** |
| Who may change firm-wide behaviour by email? | Only `PLAYBOOK_ADMINS` (or, with none named, the allowlist contact, or failing that any firm address): playbook changes and no-AI entries. The audit report by email goes only to a named admin or the allowlist contact (`audit.may_request`). `lra selftest` warns when `PLAYBOOK_ADMINS` is empty. | **Yes** |

## 4. Encryption and key management `[CEK]`

| Question | Answer | Status |
| --- | --- | --- |
| Is data encrypted in transit? | HTTPS between the container, the Worker and Anthropic. SMTP into Cloudflare and out from Cloudflare's Email Service uses TLS as negotiated with the other server; whether Cloudflare refuses a sender with no TLS is **not verified**. The firm can force TLS on its own outbound connector (`mail-flow.md`). | **Partly** |
| Is data encrypted at rest? | Documents, messages in transit through the Workflow, held mail, archive attachments and DMS tokens: AES-256-GCM under the firm's `DATA_KEY`, sealed before Cloudflare stores them, on top of Cloudflare's at-rest encryption. D1 rows (filenames, findings, the text of each tracked change, memory notes, the audit trail): Cloudflare's at-rest encryption only. Anthropic: Anthropic's at-rest encryption. | **Partly** |
| Who generates and holds the keys? | The firm's `DATA_KEY` is 32 random bytes generated by `lra keygen` or `lra tenant provision`, put on the Worker as a secret, and given to the firm to hold. Not in a KMS or HSM. | **Partly** |
| Can the customer hold the key / revoke it? | Yes: deleting the Worker secret and every copy makes everything sealed unreadable. At Anthropic, customer-managed keys (CMEK) exist for Managed Agents sessions and memory stores ([CMEK](https://platform.claude.com/docs/en/manage-claude/cmek)) but are not set up; activation is through Anthropic's account team. | **Partly** · **[Founder decision]** (CMEK) |
| Key rotation? | Partly: `DATA_KEY_PREVIOUS` lets a new key read what the old one sealed. There is no re-seal command, so the old key cannot yet be retired. Plan: an `lra rekey` that re-seals R2 objects and tokens. | **Partly** |
| Is the service refused without encryption? | Yes: the application refuses to start on hosted storage without `DATA_KEY`, and with a key set it refuses to read any stored value that is not sealed (`src/lra/crypto.py`). `lra selftest` fails a known test key. | **Yes** |

## 5. Logging and monitoring `[LOG]`

| Question | Answer | Status |
| --- | --- | --- |
| Is there an audit trail of user activity? | Yes: one row per message with no contents: received at, sender, matter, document names, what ran, model and session ids, inference geography, where stored and until when, reply recipients, sent at, outcome (`src/lra/audit.py`). Export: `lra audit --since ... --format csv|json`, or an admin emails "audit report for September". | **Yes** |
| Is the audit trail tamper-evident and how long is it kept? | Kept until the firm deletes it; a purge blanks its scope's names and ids and records itself. Not hash-chained or write-once. | **Partly** |
| Can logs be sent to our SIEM? | Not built. Today: the CSV or JSON export, and Cloudflare Workers Observability logs in the Cloudflare account. Plan: push audit rows and security events (drops, cap, pause, purge) to an HTTP collector. | **No** |
| Are security events monitored and alerted? | No alerting. Drops are logged without the sender's address in the Worker; the Workflow shows failed instances. | **No** |
| Do logs contain customer content? | The audit trail is designed to hold names, ids and times, never contents, and tests check that (`tests/test_audit.py`). Operational logs carry step names and errors; we have not audited every Python log line for document text. Workflow step state holds filenames and a one-line notice. | **Partly** |

## 6. Vulnerability and change management `[TVM]` `[CCC]` `[AIS]`

| Question | Answer | Status |
| --- | --- | --- |
| Secure development lifecycle? | Every change is a commit to a private GitHub repository. CI runs lint (`ruff`), about 2,100 Python tests twice (local SQLite and the Cloudflare D1 path), the evaluation corpus, and the Worker's own tests in workerd; security behaviour (allowlist, sender-only replies, no-AI, memory walls, encryption, purge) is pinned by tests. | **Yes** |
| Is code reviewed by a second person before release? | No: one developer. Branch protection is not available on the repository's current plan for a private repository. | **No** · **[Founder decision]** (plan upgrade, a second reviewer) |
| How does code reach production? | Only from `main` after CI passes, by GitHub Actions (`.github/workflows/deploy.yml`); the founder's own deployment first, as a canary, then each firm with its own Cloudflare token. Every push to `main` deploys every firm. Plan for customer-hosted: versioned releases the firm chooses to deploy. | **Partly** · **[Founder decision]** (release channel) |
| Dependency and vulnerability scanning? | No automated scanning (no Dependabot or equivalent configured). Dependencies are declared with minimum versions in `pyproject.toml` and `package.json`. | **No** (plan: enable scanning) |
| Is there an SBOM? | No. | **No** |
| Patch management for the runtime? | The container image is rebuilt from `Dockerfile` on every deploy; the Worker runtime is Cloudflare's. No scheduled rebuild when nothing changes. | **Partly** |
| Vulnerability disclosure? | No `security.txt` or published contact yet. | **No** · **[Founder decision]** (contact address) |
| Attack surface on the internet? | SMTP (allowlist and DMARC drop), `/health`, the signed webhook, `/oauth/callback` (closed with no DMS), and `/internal/*` behind one bearer secret, which is moving off the internet (`architecture.md`). | **Partly** |

## 7. Business continuity and disaster recovery `[BCR]`

| Question | Answer | Status |
| --- | --- | --- |
| What happens if a component fails mid-review? | Each message is a Cloudflare Workflow; each step is retried on its own with its state sealed in R2. A step that still fails sends the lawyer the one email they are owed ("I couldn't review ... Nothing was sent to anyone else") (`docs/deploy-cloudflare.md`). | **Yes** |
| What if the service is down? | Lawyers keep working: nothing in their mail client depends on it. A message sent while the Worker is down is retried by the sending server under normal SMTP rules. While paused, messages are held sealed and released later. | **Yes** |
| Backups and restore? | Not verified: what Cloudflare's D1 and R2 keep as backups, for how long and where, has not been checked (`docs/onboarding/README.md`, section 4). Sealed objects stay sealed in any backup. No restore has been tested. | **No** |
| RTO / RPO? | Not defined. | **No** · **[Founder decision]** |
| Key-person risk (one founder)? | Real. Mitigations: the firm can hold the code and run it in its own accounts (customer-hosted); every setting is in the repository; the firm holds `DATA_KEY`. No escrow or continuity plan is written. | **Partly** · **[Founder decision]** |
| Spending runaway? | Capped: allowlist before anything costs money, `MAX_EMAILS_PER_DAY`, one container instance, a per-session dollar cap enforced by Anthropic (`MANAGED_SESSION_BUDGET_CENTS`), a time budget, and prepaid credit without auto-reload. | **Yes** |

## 8. Incident response `[SEF]`

| Question | Answer | Status |
| --- | --- | --- |
| Is there an incident response plan? | Yes: `incident-response.md` (detection, containment with the kill switch, key withdrawal, purge, notification, offboarding). Not yet exercised. | **Partly** |
| Breach notification time? | Proposed: within **[24-72]** hours of becoming aware, as the firm's outside-counsel guidelines require. Anthropic commits to us within 48 hours (DPA G.1). Not yet in a signed agreement. | **[Founder decision]** |
| How fast can you stop processing? | `SERVICE_PAUSED=true` stops review and sending at once **once deployed**: a config change, a commit and a deploy, a few minutes plus the container rollout (`incident-response.md`). Faster levers owned by the firm: disable its transport rule; in its own accounts, delete the Email Routing rule or revoke the Anthropic key. A no-deploy switch is not built. | **Partly** |
| Do you have a status page or security contact? | Neither yet. | **No** · **[Founder decision]** |

## 9. Privacy and data protection `[DSP]` `[IPY]`

| Question | Answer | Status |
| --- | --- | --- |
| What personal data is processed? | Lawyers' names and addresses; whatever personal data the documents and emails contain (parties, signatories, counterparties). | — |
| Purpose limitation? | Used only to answer the lawyer who sent it. No analytics, no advertising, no model training. | **Yes** |
| Is customer data used to train models? | No. Anthropic: "Anthropic may not train models on Customer Content from Services" ([Commercial Terms, B](https://www.anthropic.com/legal/commercial-terms)). We train nothing. The product's memory is per lawyer, client and firm, inside the firm's deployment (`ai-policy-crosswalk.md`). | **Yes** |
| Retention schedule? | The product's defaults, the firm's to shorten, as in `docs/trust.md` ("What we keep, and for how long"): raw message until answered, 7 days if never; job row 24 h; the conversation, its document, versions, findings and tracked changes 7 days after the last activity (`THREAD_RETENTION_DAYS`); closings 7 days after the last email; unconfirmed notes 7 days; which changes were kept or undone not recorded (`LEARN_FROM_OUTCOMES` off); archive off, 7 days if on; audit trail and purge records until the firm deletes them; what a lawyer asked it to remember until removed. A daily sweep (`src/lra/retention.py`) deletes each item within a day of its window ending. At Anthropic: deleted when the review ends; see `docs/trust.md`. Uploads, outputs and the session itself are deleted, a failed delete is retried by the sweep, and Anthropic's own backend retention of up to 30 days sits under that. A signed retention policy is the firm's to write (launch blocker, `docs/onboarding/README.md` 1.3). | **Yes** |
| Deletion on request (client, matter, person)? | Yes: `lra purge --client N`, `--matter M` or `--lawyer x`: our rows and R2 objects, memory, and at Anthropic the sessions, files and memory stores for that scope; dry run by default, resumable, recorded. Limits in `docs/trust.md`. Run by whoever holds the deployment's credentials. No deletion certificate is produced yet. | **Yes** (certificate: **No**) |
| Data location and transfers? | Cloudflare D1 and R2 in the EU with `--jurisdiction eu`; the model in the US (no EU or UK option on Anthropic's first-party API). A UK or EU firm's transfer to Anthropic relies on Anthropic's SCCs and UK Addendum. Pin `INFERENCE_GEO=us` so the answer is one country. | **Partly** |
| DPIA support? | This pack and `docs/trust.md` are the inputs; we have not written a DPIA. | **Partly** |
| Return of data at exit? | The audit trail exports as CSV or JSON. Conversations and documents are not exported in bulk (the lawyer already holds every reply). Offboarding procedure: `incident-response.md`. | **Partly** |

## 10. AI-specific (AI-CAIQ / AI Controls Matrix topics)

| Question | Answer | Status |
| --- | --- | --- |
| Which model and provider? | Claude Fable 5.1 by default (`REVIEW_MODEL`, `agents/*.yaml`); Claude Opus 5 with `ZERO_RETENTION=true`; Anthropic's server-side fallback to Opus 5 when Fable declines a request. Through Anthropic's first-party API and Managed Agents. | — |
| Is our data used to train or fine-tune? | No (above). No fine-tuning of any kind. | **Yes** |
| Is zero data retention available? | Not for the review: Managed Agents is not ZDR-eligible ([API and data retention](https://platform.claude.com/docs/en/manage-claude/api-and-data-retention)). `ZERO_RETENTION=true` makes the model calls ZDR-compatible, not the sandbox. Clients that require ZDR go on `NO_AI_MATTERS` (no model at all). A ZDR review path would mean a non-Managed-Agents design. | **No** · **[Founder decision]** |
| Can specific clients or matters be excluded from AI? | Yes: `NO_AI_MATTERS` (matter numbers, client names, client domains), plus admin email "no AI for Acme". A match gets the deterministic checks only, and the model client refuses to be built while it holds, so a missed path fails closed (`src/lra/policy.py`, `tests/test_no_ai.py`). | **Yes** |
| Human oversight? | Every output is a suggestion to the lawyer who asked: tracked changes and comments in Word, never accepted, never sent to anyone else, never filed. Replies go to the sender only (`REPLY_POLICY=sender_only`). | **Yes** |
| Output verification? | Every returned Word file is re-verified by our code before it is attached (`redline.verify`); deterministic checks run first and their findings are evidence the model sees. Under DECISIONS 30 the model decides, and the firm policies of DECISIONS 31 hold regardless. | **Yes** |
| Prompt injection from documents? | Mitigated, not eliminated: the sandbox has no network except package registries; the session holds no email capability, no DMS token and no other client's memory; replies go to the sender only whatever the model writes; memory the agent proposes is held until the lawyer confirms it. A determined injection could put a few bytes in a package registry's log (`docs/sandbox.md`). Not independently tested. | **Partly** |
| Confidentiality walls inside the model's context? | Memory is walled by client; a lawyer's personal memory refuses any note naming a party, client, sum or proper name; the firm layer comes only from the approved playbook; an unidentified client gets no cross-matter memory (`docs/memory.md`, `tests/test_memory_walls.py`). Per-matter ethical screens between lawyers of the same firm: **not built**. | **Partly** |
| Web access by the model? | Off (`WEB_SEARCH_ENABLED=false`), and the sandbox has no allowed hosts. | **Yes** |
| Is AI use logged per job? | Yes: the audit row records "model review", the model, the session ids and the inference geography; each session can be read turn by turn in the Anthropic Console of the organisation that owns it. | **Yes** |
| Model evaluation and accuracy? | Deterministic checks: 27/27 planted defects caught, no false positives on 15 clean documents (`STATUS.md`). The model-backed review has an evaluation harness but **has not run live on the current code**. | **Partly** |
| Inference location? | `INFERENCE_GEO=us` pins US; unset follows the workspace default (global unless pinned). No EU or UK. | **Partly** |
| AI management system (ISO/IEC 42001)? | Ours: no. Anthropic's: certified ([Anthropic certifications](https://privacy.claude.com/en/articles/10015870-what-certifications-has-anthropic-obtained)). | **No** |

## The gaps, in one list

What a reviewer will mark against us today, and the plan:

1. No SOC 2, ISO 27001, Cyber Essentials or pen test for Redline Desk.
   **[Founder decision]**
2. No DPA, pilot agreement or MSA. **[Founder decision]**
3. `/internal/*` reachable from the internet behind one secret (change in
   progress); container egress open.
4. DMARC: only an explicit fail is dropped (change in progress).
5. D1 rows not under the firm's key.
6. No KMS/HSM for `DATA_KEY`; no re-seal command; CMEK at Anthropic not
   set up. **[Founder decision]** for CMEK.
7. Kill switch needs a deploy.
8. No SIEM feed or alerting.
9. No second reviewer, no branch protection, every push deploys every firm.
   **[Founder decision]**
10. No dependency scanning, no SBOM, no `security.txt`.
11. Backups and restore not verified; no RTO/RPO. **[Founder decision]**
12. No breach-notice commitment or status page in a contract.
   **[Founder decision]**
13. No ZDR for the review. **[Founder decision]**
14. No per-matter ethical screens between lawyers.
15. No deletion certificate; no bulk export at exit.
16. MFA and access policy for the founder's accounts not evidenced.
   **[Founder decision]**
17. Model-backed review not yet run live on the current code.
