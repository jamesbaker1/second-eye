# Onboarding a design-partner firm

The checklist for taking the first firm from "interested" to "sending live
documents". It is in the order the work has to happen. Each item says who it
waits on: **the firm**, **Jim**, or **done in code**. Where something is a
launch blocker that is not built, it says so.

Last checked against the code: 2026-10-04.

The other pages in this kit:

- `general-counsel.md`: the note for the firm's general counsel or risk
  partner. Send it first.
- `playbook-template.md` (and `playbook-template.docx`): how the firm writes
  down its negotiating positions so the playbook agent can read them.
- `first-week.md`: what the firm's lawyers should try in week one, with the
  email to send for each.
- `docs/it/README.md`: the IT pack for the firm's IT and security team (architecture, sub-processors, questionnaire, mail flow, incident response, AI policy crosswalk, `lra selftest`).

## Before anything else

The model-backed half of the product has never run on the current code
(`STATUS.md`, "What has not been run"). Nothing in this kit should reach a firm
until Jim has done the first live runs and fixed what they find. The
mechanical half (checks, clean copy, renumber, sig pages, deadlines) works in
production today.

## 1. What the firm must sign or decide

Each of these is a decision for someone at the firm with authority to make
it, normally the general counsel or risk partner. `general-counsel.md`
explains each one in plain English.

| # | Decision | Options and default | Why it matters |
| --- | --- | --- | --- |
| 1.1 | **Whose accounts it runs in** | The firm's own Cloudflare account and Anthropic organisation, or ours on the firm's behalf (DECISIONS 27) | Decides who holds `DATA_KEY`, who can read the Anthropic Console traces, and who is billed. One deployment serves one firm: `FIRM_DOMAINS`, the firm memory store and the firm playbook are all per deployment |
| 1.2 | **Accept Anthropic's processing, including 30-day retention** | Claude Fable 5.1 with 30-day retention (default); or `ZERO_RETENTION=true`, which needs the firm's Anthropic organisation on zero data retention and moves everything to Claude Opus 5 | The review runs in an Anthropic Managed Agents sandbox. The container, the mounted document and the files the agent writes are kept by Anthropic for up to 30 days **whatever the model**. `ZERO_RETENTION` does not change that (`docs/sandbox.md`). DECISIONS 25 and 29 leave "production security for a firm's documents" as an open decision to be made before any firm's matter goes through this path. This row is that decision |
| 1.3 | **A written retention policy, signed by the general counsel** | Our defaults are below; the firm may change any of them | Launch blocker in `docs/archive.md` and DECISIONS 13. **Not started: needs the firm.** We can supply the table of what is kept and for how long (`docs/trust.md`, "What we keep"); the firm writes and signs the policy |
| 1.4 | **The archive: on or off** | Off (`ARCHIVE_ENABLED=false`) | The archive stores every message the agent is copied on. It is also the one place extracted document text sits searchable in D1, outside the firm's key. Recommendation: off for the pilot. If on, it keeps mail 7 days unless the firm sets `ARCHIVE_RETENTION_DAYS` longer (0 keeps it for ever) |
| 1.5 | **Accept that database rows are not under the firm's key** | Accept, or ask for them to be sealed | Documents are sealed with the firm's key. The rows in D1 are not: sender, filenames, findings and the text of each tracked change, remembered preferences (DECISIONS 28, ROADMAP "Known findings"). Sealing them is **not built**; it is the next step if a firm asks |
| 1.6 | **Who replies go to** | `REPLY_POLICY=sender_only` (default): the sender and nobody else | `model` lets the model choose recipients (DECISIONS 30). Recommendation: keep `sender_only`. It is enforced twice, in `policy.py` and in the Worker (`cloudflare/src/outbox.ts`) |
| 1.7 | **Telling clients** | The firm's call | ABA Formal Opinion 512 treats a tool that learns from what it is given as self-learning, and asks for informed client consent before client information goes into one. The agent learns, walled by client (`docs/memory.md`). Whether and how to tell clients is the firm's decision |
| 1.8 | **Clients whose guidelines exclude AI** | The firm lists them (section 2) | Their documents get no model call at all |
| 1.9 | **Where data sits** | R2 bucket in the default location; `INFERENCE_GEO` unset | A firm that must keep client material in the EU needs the R2 bucket created with `--jurisdiction eu`. `INFERENCE_GEO` takes `us` or `global` only; there is no EU inference setting |
| 1.10 | **Web search** | Off (`WEB_SEARCH_ENABLED=false`) | A search query written from a privileged document discloses part of it. Keep it off |
| 1.11 | **The privilege legend** | "Privileged & Confidential — Attorney Work Product" | `PRIVILEGE_NOTICE`: the last line of every reply and an `X-Privileged` header. The firm may supply its own wording, or none |
| 1.12 | **Connect the document system or not** | Not connected | Optional. If yes, see 2.6. The iManage path is written from documentation and has never run against a real instance |
| 1.13 | **The pilot terms between the firm and us** | None drafted | **Needs Jim.** There is no pilot agreement or data processing addendum in this repo. The firm will ask for one |

## 2. What the firm sends us

| # | What | Becomes | Notes |
| --- | --- | --- | --- |
| 2.1 | **Allowed senders**: every address or domain that may send to the agent | `ALLOWED_SENDERS` | Matched exactly, at the edge (`isAllowed` in `cloudflare/src/mail.ts`) and in the app (`intake.check_sender`). `firm.com` does not admit `london.firm.com`: list every domain the firm's lawyers send from. Anyone not on the list is dropped silently; they get no reply. Adding co-counsel's domain lets them use it but does not make them "inside the firm" for the wrong-recipient check |
| 2.2 | **The firm's own mail domains** | `FIRM_DOMAINS` | Required when the agent is on a subdomain; the service refuses to start without it (`config.py`). The first domain listed is the key the firm's playbook is stored under |
| 2.3 | **A named contact, and the playbook admins** | `ALLOWLIST_CONTACT`, `PLAYBOOK_ADMINS` | Admins may change the playbook, add or lift a no-AI client by email, and ask for the audit report by email. Name them as addresses, not a domain: with `PLAYBOOK_ADMINS` empty, every firm address may change the playbook (`playbook.is_admin`). The audit report by email goes only to an address named in `PLAYBOOK_ADMINS` or to `ALLOWLIST_CONTACT` (`audit.may_request`) |
| 2.4 | **No-AI clients and matters** | `NO_AI_MATTERS` | Matter numbers, client names, or the client's mail domains, comma separated. A domain entry is the strongest: it also covers scanned PDFs, whose parties only a model can read. Entries here cannot be lifted by email; entries an admin adds by email ("no AI for Acme") can |
| 2.5 | **The client register** | `lra clients add` | For each client: the client number the firm's matter numbers start with, the names it appears under in documents, and the domains its people write from. This is what memory is walled by. Also: how the firm writes matter numbers. The agent reads a matter number only when a lawyer writes it as "Matter 10234-0007" (or "File" or "Client") in the subject or body (`identity.resolve_matter`); a client it cannot identify gets no cross-matter memory at all |
| 2.6 | **iManage details, if connecting** | `DMS_PROVIDER`, `DMS_CLIENT_ID`, `DMS_CLIENT_SECRET`, `DMS_TOKEN_URL`, `DMS_AUTHORIZE_URL`, and `IMANAGE_BASE_URL` in `cloudflare/dms-mcp/wrangler.jsonc` | The firm's iManage Work host, an OAuth client registered for us with the read-only scopes in `docs/oauth.md`, and the redirect URL we give them (the edge URL plus `/oauth/callback`). Each lawyer then grants their own access by replying "connect" |
| 2.7 | **Their playbook** | The firm playbook | In the shape of `playbook-template.md`, sent by email by a playbook admin (3.4) |
| 2.8 | **Preferences** | `REDLINE_AUTHOR`, `PRIVILEGE_NOTICE`, `MAIL_AGENT_NAME` | The name on every tracked change (default "Reviewer"), the legend, the display name on replies (default "Second Eye") |
| 2.9 | **Expected volume** | `MAX_EMAILS_PER_DAY` | Every accepted message counts, including BCC copies answered with silence. Size it for the lawyers' whole outbox if they adopt the BCC habit. Today it is 30 |
| 2.10 | **Each pilot lawyer verifies their address** | `CLOUDFLARE_MAX_MESSAGE_MB=25` | Cloudflare sends 5 MiB per message, or 25 MiB to an address verified in the account. A marked-up agreement can pass 5 MiB. Each lawyer clicks one verification email |
| 2.11 | **A subdomain for the agent's address, if on their domain** | `MAIL_AGENT_ADDRESS` | e.g. `review@legal.firm.com`. A subdomain, so the agent's sending reputation is separate from the firm's. Needs the firm's DNS for that subdomain on Cloudflare |

## 3. What we configure

All settings below are `vars` in the firm's `deployments/<firm>/tenant.jsonc`
(not secret; `cloudflare/wrangler.jsonc` says what each does) or secrets on
the firm's Worker. They reach the
application only if they are in the `FORWARDED` list in
`cloudflare/src/index.ts` (see 3.4). The setup order is in
`docs/deploy-cloudflare.md`, "Setting it up"; this is what is different for a
firm.

### 3.1 The deployment

| Step | Command or setting | State |
| --- | --- | --- |
| **The firm's own deployment**: its Worker, container, D1 database, R2 bucket (with `--jurisdiction eu` if 1.9 says so), Workflow, address, vars and secrets, none shared with Jim's or another firm's | 1. `lra tenant new <firm> --address review@legal.firm.com --domains firm.com [--senders ...] [--contact ...] [--admins ...] [--jurisdiction eu] [--account-id ...]`, then read and fill the vars in `deployments/<firm>/tenant.jsonc` (2.1 to 2.9) and `lra tenant render <firm>`. 2. The firm's `ANTHROPIC_API_KEY` in `deployments/<firm>/.env`. 3. `lra tenant provision <firm>` (a dry run: every wrangler command printed), then `--apply` with a token for the firm's account: D1, R2 and its lifecycle rules, the Worker, `DATA_KEY`, `EDGE_SECRET`, `ANTHROPIC_API_KEY`. 4. Back up the `DATA_KEY` it prints the place of. 5. The steps it prints as by hand: email sending and routing, the webhook key, `lra live-check --tenant <firm>`, and the GitHub secrets `CLOUDFLARE_API_TOKEN_<FIRM>` and `CLOUDFLARE_ACCOUNT_ID_<FIRM>`. 6. Commit `deployments/<firm>/` and push: `deploy.yml` deploys it after Jim's. `docs/deploy-cloudflare.md`, "One deployment per firm" | **Done in code** (`src/lra/tenant.py`, `tests/test_tenant.py`; Jim's deployment is the tenant `jim`, its config unchanged). **Needs Jim** to run it for the firm, a Cloudflare account on Workers Paid, and the firm's DNS (2.11) |
| **Or, self-hosted**: the firm's IT runs it in the firm's own Cloudflare account and Anthropic organisation, deployed from a release, with no standing access for us | `lra tenant new <firm> --self-hosted --account-id <theirs> ...`; the firm follows `docs/it/self-hosted.md` (provision, `lra tenant deploy --release`, support tokens, offboarding) | Done in code, never run against a real account. An option, not the default |
| Email routing for the agent address, sending enabled on the subdomain, subaddressing on (for one-tap links) | `docs/deploy-cloudflare.md`, "Email" | **Needs Jim** (and the firm's DNS, 2.11). `ONE_TAP_LINKS` stays `false` until a test to a plus address gets a reply |
| `DATA_KEY`: generate, give to the firm to hold in their vault, put as a secret | `lra tenant provision <firm> --apply` generates and puts it, and says where it is kept | Done in code: documents, held mail, archive attachments and DMS tokens are sealed with AES-256-GCM, and the service refuses to start on D1 without a key (`crypto.py`, `blobs.py`). **Needs the firm** to hold the key. **Needs Jim**: today the only copy of the production key is on Jim's laptop, and backing it up is on his to-do list |
| `EDGE_SECRET`, `ANTHROPIC_API_KEY`, `ANTHROPIC_WORKSPACE_ID`, `ANTHROPIC_WEBHOOK_SIGNING_KEY` | `lra tenant provision <firm> --apply` puts the first three; the webhook key by hand (it prints the command) | Needs Jim |
| Anthropic: credit, a workspace monthly limit, 30-day retention on (Fable 5.1 requires it) or ZDR with `ZERO_RETENTION=true` | Console | Needs Jim, after 1.1 and 1.2 |
| The agents and skills | `lra live-check --tenant <firm>`: skills, agents, environment and memory store in the firm's organisation, the ids written into `deployments/<firm>/tenant.jsonc` | Done in code; needs Jim to run against the firm's Anthropic organisation |

### 3.2 Who may use it, and where replies go

| Setting | Value for the firm | Enforced in | State |
| --- | --- | --- | --- |
| `ALLOWED_SENDERS` | from 2.1 | `cloudflare/src/index.ts` (`email()`), `intake.check_sender` | Done in code |
| `FIRM_DOMAINS` | from 2.2 | `config.py` | Done in code |
| `ALLOWLIST_CONTACT`, `PLAYBOOK_ADMINS` | from 2.3 | `playbook.is_admin`, `audit.may_request` | Done in code |
| DMARC required | nothing to set, unless a firm domain has no DMARC record: then `DKIM_ONLY_DOMAINS` until it has one | `cloudflare/src/auth.ts`: only mail whose domain passed DMARC on Cloudflare's verdict is admitted; the rest is dropped before it is counted. Ask IT for each sending domain's DMARC record before go-live | Done in code |
| `REPLY_POLICY` | `sender_only` | `policy.apply`, and `policed()` in `cloudflare/src/outbox.ts` | Done in code |
| `MAX_EMAILS_PER_DAY` | from 2.9 | `email()` in the Worker | Done in code |
| `PRIVILEGE_NOTICE` | from 2.8 | `policy.privileged`, `cloudflare/src/mail.ts` | Done in code |

### 3.3 The firm's controls

| Control | Setting or command | State |
| --- | --- | --- |
| **No-AI list** | `NO_AI_MATTERS` from 2.4. Admins add more by emailing "no AI for Acme"; "AI ok for Acme" lifts an emailed entry | Done in code (`policy.py`, `tests/test_no_ai.py`). A match makes the model client refuse to build, so a missed path fails closed |
| **Zero-retention option** | `ZERO_RETENTION=true`, then `lra agents apply` again | Done in code. Covers the model only, not the sandbox |
| **Kill switch** | `lra pause <firm>` (a dry run; `--apply` writes the switch row in the firm's D1, or paste its SQL into the dashboard's D1 console), `lra resume <firm>` to undo. No deploy. One firm's switch stops that firm only. Mail is accepted and held sealed, nothing is reviewed or sent, a review under way waits before it sends; on resume the held mail is released by the daily cron or `POST /internal/release`. `SERVICE_PAUSED="true"` in `tenant.jsonc` and a deploy still works, and wins | Done in code (`cloudflare/src/pause.ts`, `killswitch.py`, `policy.paused`), never run against the live D1 |
| **Memory walls** | `lra clients add <number> <name> --alias ... --domain ...` for each client in 2.5; `lra clients link <matter> <number>` where a matter number does not start with the client number | Done in code (`clients.py`, `memory.py`, `tests/test_memory_walls.py`). Run from a machine with `DATABASE_URL=d1://`, `EDGE_URL`, `EDGE_SECRET` and `DATA_KEY` set to the firm's deployment |
| **Audit export** | `lra audit --since 2026-11-01 --format csv`, or an admin emails "audit report for November" | Done in code (`audit.py`). One row per job, no contents |
| **Deletion by client, matter or lawyer** | `lra purge --client 10234` (or `--matter`, `--lawyer`): a dry run listing counts and Anthropic ids; add `--apply` to delete. `lra purge --history` lists every purge | Done in code (`purge.py`, `tests/test_purge.py`). Run from a machine with the deployment's `DATABASE_URL`, `EDGE_*`, `DATA_KEY` and `ANTHROPIC_API_KEY` |
| **Retention** | `THREAD_RETENTION_DAYS=7` (nothing about a document kept 7 days after the last reply, swept daily), `RETENTION_HOURS=24`, `ARCHIVE_ENABLED=false`, `LEARN_FROM_OUTCOMES=false`, and the R2 lifecycle rules | Done in code. The values are whatever 1.3 says; the firm may shorten them |
| **Document system** | `DMS_*` from 2.6; `docs/deploy-cloudflare.md`, "The document-system MCP server" | Written, never run against a real iManage. Lawyers reply "revoke" to disconnect |

### 3.4 Every setting reaches the container

`cloudflare/src/index.ts` passes the container only the settings named in
`FORWARDED`. Until 2026-10-04 that list had fallen behind `config.py`, so
the playbook, comments, closing and blackline agent ids, four skill ids,
`REDLINE_AUTHOR` and others could be set and did nothing. **Fixed in code**:
the list now carries every setting the application reads, and
`tests/test_worker_forwarding.py` fails if a new one is added to `config.py`
without being forwarded or named as deliberately left out.

So the firm's playbook can be loaded by email (`playbook-template.md`) once
`lra agents apply` has printed `MANAGED_PLAYBOOK_AGENT_ID` and it is set in
the `vars` of the firm's deployment (`lra live-check --tenant <firm>` does it). Turning comments, signature packets and
blacklines likewise need only their agent and skill ids set.

## 4. Launch blockers

Everything that `docs/archive.md`, `ROADMAP.md` ("The thing to be careful
about") and `DECISIONS.md` say must be true before a second user or a live
client matter.

| Blocker | Source | State |
| --- | --- | --- |
| The firm's general counsel has signed a written retention policy | `docs/archive.md`; DECISIONS 13 | **Not started. Needs the firm** (1.3) |
| Encryption at rest with firm-held keys | `docs/archive.md`; DECISIONS 13, 28 | **Done in code** for documents, held mail, archive attachments and DMS tokens. **Not covered**: the D1 rows, and the archive's message rows and search index (1.4, 1.5). **Needs the firm** to hold the key, **needs Jim** to hand it over and back it up. |
| Per-lawyer scoping, enforced in code | `docs/archive.md` | **Done in code.** Each lawyer's conversations and archive are theirs alone. Per-matter ethical screens between lawyers in the same firm are **not built**: a client's or matter's memory is read on every review of that client's matters, whoever sends it |
| An audit log of every read | `docs/archive.md` | **Done in code**: `access_log` for the archive, `audit_log` for every job, Worker logs for each document-system call |
| A deletion path for when a client demands one | `docs/archive.md`; DECISIONS 6 | **Done in code.** `lra purge --client`, `--matter` or `--lawyer` deletes conversations, documents, versions, closings, archived mail, remembered notes and suppressions, and Anthropic's sessions, files and memory stores; it blanks the scope's audit rows and records the purge (`docs/trust.md`). Not reachable by client: conversations from before 2026-10-04 with no matter number (counted by the dry run; purge their lawyers) |
| Retention expiry | `docs/archive.md` | **Done in code.** Values per 1.3 |
| Tokens and blobs excluded from backups that leave the tenant | `docs/archive.md` | **Not started. Needs Jim**: check what Cloudflare's D1 and R2 backups cover and where they live |
| Production security for a firm's documents in Anthropic's sandbox | DECISIONS 25, 29; `docs/sandbox.md` | **Open. Needs the firm** (1.2) **and Jim** |
| A second deployment the firm's data lives in alone | `cloudflare/wrangler.jsonc` comment; DECISIONS 27 | **Done in code** (`lra tenant`, 3.1). **Needs Jim** to run the steps in 3.1 for the firm |
| Settings the firm needs actually reach production | `cloudflare/src/index.ts` | **Done in code** (3.4), pinned by `tests/test_worker_forwarding.py` |
| A live model run on the current code | `STATUS.md` | **Not done. Needs Jim**: credit, then the live evals |
| Output opened in Microsoft Word | `STATUS.md` | **Not done. Needs Jim** (or a firm lawyer): `python -m evals.word_pack` |
| Document system tested against a real instance | `STATUS.md`; `docs/oauth.md` | **Not done.** Needs the firm's iManage (only if 1.12 is yes). The OAuth callback is on our `workers.dev` host, not one the firm controls, which `docs/oauth.md` lists as a production requirement |
| Pilot agreement | | **Not drafted. Needs Jim** (1.13) |

## 5. Go-live, in order

1. Jim: the live model runs pass (`STATUS.md`, "What to do next").
2. Firm: decisions in section 1 made; retention policy signed.
3. Firm: section 2 sent.
4. Jim: the blockers marked "Needs Jim" in section 4 closed, or written down
   as accepted by the firm.
5. Jim: deployment configured (section 3). `MAX_EMAILS_PER_DAY` low for the
   first week.
6. Jim: one test of each `first-week.md` email from an allowed address, read
   in the inbox.
7. Firm: one lawyer sends `help`, then the week-one emails.
8. Jim: read `lra audit` at the end of the week with the firm's contact.
