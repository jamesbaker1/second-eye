# Deploying on Cloudflare

This is the deployment a design-partner firm runs on. Everything is in one
Cloudflare account: mail in, mail out, a Workflow per email, the database,
the document store, and the container the application runs in.

## What runs where

```
lawyer's mail client
      |  email to review@<firm subdomain>
      v
Cloudflare Email Service ---> Worker  email()   seals the raw message into R2,
                                 |              starts a ReviewFlow instance for it
                                 v
                            Workflow  -------> container /prepare, /session/start,
                                                   |   /session/status, /finish, /finished
                                                   v
                                             Container (Python, this repo)
                                                   |   review, redline, compare, clean
                                                   |
                 |   http://edge.internal/... (the container's outbound
                 |   handler, never on the internet)        api.anthropic.com
                 +---------------------------------+------------------------------+
                 v                                 v                              v
        /internal/db  -> D1              /internal/blob -> R2            /internal/send -> Email Service
        rows                             documents, sealed               the reply, to the sender only
```

The Worker is `cloudflare/src/index.ts`. It holds no product logic and reads no
document. It exists because a container cannot bind to D1, R2, Workflows or
the email service, and a Worker can. The application reaches all of them through
three internal endpoints with one secret, `EDGE_SECRET`, and holds no
Cloudflare API token. It calls them as `http://edge.internal`, a name the
container runtime hands to the Worker's outbound handler without it leaving
Cloudflare, so those endpoints are not on the Worker's public hostname at all
(below, "What is on the internet").

The only thing outside Cloudflare is Anthropic. The review runs as a Managed
Agents session (DECISIONS 29): the container opens a session on the agent
definitions `second-eye agents apply` created, mounts the document in Anthropic's
sandbox with nobody attached, reads the findings and the redline the session
leaves as files, and verifies what comes back before it is attached. The document is in Anthropic's
container for the life of the session and retained under the terms in
`docs/sandbox.md`. For a firm deployment that page is for their general counsel
to read before any of their matters go through.

Setting up the agents is a one-time step before the first deploy, from a
machine with `ANTHROPIC_API_KEY` set:

```bash
.venv/bin/second-eye skills sync                    # prints SANDBOX_SKILL_ID
.venv/bin/second-eye skills sync lra-playbook       # prints SANDBOX_PLAYBOOK_SKILL_ID
.venv/bin/second-eye agents apply                   # prints MANAGED_*_ID for .env / wrangler vars
```

Run `second-eye agents apply` again after changing `agents/*.yaml`, the prompts they
name, `AGENT_EFFORT`, `WEB_SEARCH_ENABLED`, `INFERENCE_GEO` or a skill id; it
updates the agents in place, which makes a new version, and running sessions
keep the old one. The ids are plain vars, not secrets. Every session's Console
trace is logged at INFO as
`https://platform.claude.com/workspaces/<ANTHROPIC_WORKSPACE_ID or default>/sessions/<id>`. It
is readable while the session runs: each session is deleted, with its
events, when the review ends (`managed._clean_up`).

## What a firm's general counsel will ask, and the answers

**Where are our documents?** In one R2 bucket in the firm's own Cloudflare
account, or ours on their behalf. Set `jurisdiction` on the bucket to keep them
in the EU. Rows that describe them are in D1.

**Who can read them?** Whoever holds `DATA_KEY`. Every document is encrypted
with AES-256-GCM before it leaves the application (`src/secondeye/crypto.py`), and so
is an incoming message while it waits for its Workflow (the Worker does the same,
in the same format). Cloudflare also encrypts at rest, but that protects
against a stolen disk. This protects against a misconfigured bucket, a leaked
token, and a request served on the host instead of the firm. The firm generates
the key (`second-eye keygen`), and withdrawing it turns everything stored into noise.
The service refuses to start on D1 without one.

**What is not encrypted under our key?** Be exact about this. The rows in D1:
who sent what and when, filenames, the agent's findings and the text of each
tracked change, the lawyer's remembered preferences. And, if the archive is
switched on, the extracted text it indexes for search, because an index over
ciphertext finds nothing. The archive is off by default. Document-system OAuth
tokens are encrypted.

**How long do you keep things?**

Nothing about a document is kept more than 7 days after the conversation's
last activity (Jim's call, 2026-10-04). The daily cron runs the sweep
(`retention.py`, through the container's `/retention/sweep`); `second-eye purge`
with no scope runs the same sweep by hand.

| What | Kept for | Setting |
| --- | --- | --- |
| The raw incoming message | Until reviewed, then deleted. 7 days if it never is | bucket lifecycle rule, below, and the daily cron |
| A review's state between Workflow steps (`job/` in R2, sealed) | Until the reply is sent, then deleted. 7 days at most | the lifecycle rule and the daily cron |
| The job row (sender, status) | 24 hours | `RETENTION_HOURS` |
| The conversation, its document, earlier versions and negotiation ledger | 7 days after the last reply or new version | `THREAD_RETENTION_DAYS` |
| A closing and its signed pages | 7 days after the last email on it | `THREAD_RETENTION_DAYS` |
| A note the agent proposed that nobody confirmed | 7 days | `THREAD_RETENTION_DAYS` |
| At Anthropic: each job's review sessions (with what they wrote) and uploads | Deleted when the session ends; the daily sweep retries a failed delete once the job is 7 days old | `THREAD_RETENTION_DAYS` |
| Archive, if enabled (off by default) | 7 days | `ARCHIVE_RETENTION_DAYS` |
| What was kept, undone or dismissed | Not recorded, unless learning is on | `LEARN_FROM_OUTCOMES` |
| Notes a lawyer asked to keep | Until the lawyer, or a purge, removes them | none |
| Mail held by the kill switch (`held/` in R2, sealed) | Until the switch is turned off and it is released | `SERVICE_PAUSED` |
| Outbox rows, webhook event ids, session-to-instance rows (D1; ids only) | 31 days | the daily cron |
| The audit trail and the purge records (no contents) | Until the firm deletes them | none |

Anthropic may keep its own copies of a session for up to 30 days under its
terms, whatever we delete (`docs/trust.md`). Production ran `THREAD_RETENTION_DAYS=30`
until 2026-10-04; the first sweep after the change deletes every conversation
and closing quiet for more than 7 days.

Until this deployment, conversations were never deleted at all: the row
recording that an email arrived was gone in a day and the privileged document
itself stayed forever. `thread.purge` fixes that and deletes the object in R2,
not just the row pointing at it. Until 2026-10-04 its sweep ran only when a
new conversation started, so a quiet week deleted nothing; the daily cron
runs it now.

**Can you delete everything for one person, client or matter?** Yes:
`second-eye purge --lawyer jane@firm.com`, `--client 10234` or `--matter
10234-0007`, a dry run until `--apply`. Conversations, their documents in R2
and earlier versions, closings and signed pages, archived mail and its
attachments, remembered notes and suppressions, and Anthropic's sessions,
files and memory stores for that scope; the scope's audit rows are kept and
blanked, and the purge is recorded. What it cannot reach is in
`docs/trust.md`.

**What if your server dies while reviewing our document?** The review is a
Cloudflare Workflow, one instance per email, and each stage is a step that is
retried on its own, with its state sealed in R2 between steps. The review
session itself runs in Anthropic's sandbox, not in our container, so a
container restart loses nothing: the next step picks up the session by its
id. A step that still fails after its retries takes the failure path, which
sends the lawyer the one email they are owed (the mechanical findings, or
"I couldn't review <subject>. Nothing was sent to anyone else. Please
resend it.") and keeps the conversation so their reply to it still works.

**What if a review just takes too long?** `AGENT_TIME_BUDGET_SECONDS` (five
minutes by default) is checked on every session event. Past it the session is
interrupted and the model told to report what it has, the verdict says "Partly
reviewed", and the email says the review was cut short. It is never labelled
complete. Money has its own cap:
`MANAGED_SESSION_BUDGET_CENTS` is enforced by Anthropic before every model
request in the session.

**Who can send to it?** `ALLOWED_SENDERS`: exact addresses, or exact domains
(`firm.com` does not admit `london.firm.com`, so list every domain the
firm's lawyers send from). Anyone else is dropped silently at the Worker,
with no bounce and no reply; an empty `ALLOWED_SENDERS` at the Worker drops
everything. A message is admitted only if its sender's domain passed DMARC,
on the verdict Cloudflare's own mail server wrote (`cloudflare/src/auth.ts`):
the topmost `Authentication-Results`, which must be `mx.cloudflare.net`'s and
say `dmarc=pass` for the From address's domain. Anything else is dropped at
the Worker, silently and before it counts against the daily cap: a DMARC
failure, a domain with no DMARC record, a DNS error, no verdict, a pass for
some other domain, two From headers. A forged partner address gets neither a
review nor a say about one, and a forged "pass" written lower in the headers
is never read. A firm domain with no DMARC record yet goes in
`DKIM_ONLY_DOMAINS` and is admitted on a DKIM signature that passed for
exactly that domain; a DMARC failure is never admitted. Publishing a record
is the fix. The application checks DMARC again for replies and commands
(`router.authentication_failed`), for the paths that do not come through the
Worker.

After deploying this rule, email the agent from each allowed address and
check `npx wrangler tail` shows `sender authenticated by dmarc ;
Cloudflare verdicts: 1`. A drop logs `dropped mail whose sender domain did
not authenticate:` and the reason, with no address. If a real sender is
dropped with `no Authentication-Results`, Cloudflare did not stamp the
message: revert the commit (there is deliberately no switch that loosens
the rule) and report it.

**Who does a reply go to?** With `REPLY_POLICY=sender_only`, the default, the
lawyer who sent the message and nobody else, whatever the model decided
(DECISIONS 31). It is enforced twice where mail leaves: in the application
(`policy.py`, wrapping every send and every reply stored for the Worker) and
in the Worker (`policed()` in `outbox.ts`, which refuses anything not
addressed to exactly one allowlisted sender, and drops any CC). A firm that
wants the model to choose recipients (DECISIONS 30) sets `REPLY_POLICY=model`.

**Are replies marked privileged?** Every message ends with `PRIVILEGE_NOTICE`
("Privileged & Confidential — Attorney Work Product" unless the firm sets its
own; empty for none), small and grey in HTML, and carries it in an
`X-Privileged` header for journaling and DLP rules. At the foot, not the top:
the first line is the verdict a phone previews. The documents we return are
not stamped: a redline or clean copy is usually sent on to the other side,
and a privilege legend in its properties would travel with it.

**A client's guidelines forbid AI. Can their work stay away from the model?**
Yes. List them in `NO_AI_MATTERS` (matter ids, client names, or the client's
mail domains), or have a playbook admin email "no AI for Acme" ("AI ok for
Acme" lifts it; a configured entry cannot be lifted by email). A message on
that matter, with the client's domain on To or CC, or with the client named as
a party in the document gets no model call at all: the deterministic checks,
compare, clean copy, renumber and signature pages, which are code, and a reply
that says "Mechanical checks only: Acme's guidelines exclude AI." While such a
message is handled, `managed.configured()` is false and `anthropic_client()`
raises, so a model call nobody routed around fails rather than being made
(`policy.py`, `tests/test_no_ai.py`). One limit: parties are read from the
document's text, so a scanned PDF whose only link to the client is its
parties would be transcribed by the model before they are known. List the
client's domain or matter to cover its scans too.

**How do we stop it at once?** Set `SERVICE_PAUSED` to `"true"` in the deployment's `tenant.jsonc`, `second-eye tenant render`, and deploy: every message is accepted and held sealed in R2 (never bounced), nothing is reviewed and nothing is sent; set it back and the held mail goes through, on the daily cron or at once with an operator's signed `POST /internal/release` (below, "Operator access").

**Can I just BCC it on everything?** Yes, and that is the intended habit: a
mail rule that BCCs the agent on every outgoing message. When the agent is on
no visible header it replies to the sender only, never to anyone the message
was addressed to, and a message with nothing attached gets no reply at all
(`identity.bcc_only`, tested in `tests/test_handler.py`). Two things to know.
Each accepted message counts against `MAX_EMAILS_PER_DAY`, including the ones
answered with silence, because the cap is applied before anything is parsed;
size it for the lawyer's whole outbox, not their document count. And a
document going to a client gets an "Already sent" report, not a redline; the
gate is still forwarding before you send.

## What it can cost, and what stops it

Cloudflare has no spending cap. It has budget alerts, which send an email the
day after a threshold is crossed and stop nothing. So the caps are ours, and
they sit in the Worker, in front of everything that costs money:

| Guard | Where | What it stops |
| --- | --- | --- |
| Allowlist at the edge | `email()` in the Worker, `ALLOWED_SENDERS` | A stranger's mail waking the container, running the model, or getting a reply. Dropped silently, with no bounce; nothing of ours starts |
| Daily cap | `MAX_EMAILS_PER_DAY`, counted atomically in D1 | A loop, a forwarding rule gone wrong, or a compromised mailbox. Past the cap, mail is refused until midnight UTC |
| One small container | `max_instances: 1`, `instance_type`, `sleepAfter: 10m` | Scale-out, and paying for idle. `basic` is about $6.50 a month if it never slept |
| Bounded step retries, then one failure email | `review-flow.ts` | A message that always fails being reviewed for ever |
| The OAuth callback is closed unless a document system is configured | `fetch()` in the Worker | The one public path to the container being used to keep it awake |
| Dollar cap on the session | `MANAGED_SESSION_BUDGET_CENTS`, enforced by Anthropic | One review running away. The session pauses at the cap; we report what it found |
| Time budget on the session | `AGENT_TIME_BUDGET_SECONDS` | One review holding its Workflow, and the lawyer, past the five minutes DECISIONS 16 allows |

`tests/test_cloudflare.py` pins these, so loosening one is a line in a diff.

Two caps are not ours to set and matter more than any of the above, because
the model is where the money goes:

- **Anthropic:** buy prepaid credit and leave auto-reload off. Then the balance
  is a hard ceiling. Also set a monthly limit on the workspace, in the Console
  under Settings, Limits.
- **Cloudflare:** set a budget alert at a number you would want to hear about
  (Billing, Budget alerts). It will not stop anything; the guards above do that.

## Setting it up

You need a Cloudflare account on the **Workers Paid plan**, the firm's domain
(or a subdomain of it) on Cloudflare DNS, and Node 22. The plan is not
optional and the failure is not obvious: on the free plan D1, R2 and
the Worker itself all deploy, and then Containers answers "you do not have
access" and Email Sending answers a bare "Unauthorized".

You do not need Docker. The image has to be built by something with a Docker
engine, and `.github/workflows/deploy.yml` does it on a GitHub runner after CI
passes on main. It needs two repository secrets, `CLOUDFLARE_ACCOUNT_ID` and
`CLOUDFLARE_API_TOKEN`; the token is the "Edit Cloudflare Workers" template
plus account permissions for Containers, D1, Queues (only to detach the
retired queue consumers, below), Workflows and Workers R2 Storage.
Without them the workflow skips and stays green.

```bash
cd cloudflare && npm ci

# 1. Storage. Note the database id it prints, for wrangler.jsonc.
npx wrangler d1 create legal-review-agent
npx wrangler r2 bucket create legal-review-agent-docs          # add --jurisdiction eu if required

# 2. A message that is never successfully reviewed must not sit in R2 for ever.
npx wrangler r2 bucket lifecycle add legal-review-agent-docs \
  --name expire-unreviewed-mail --prefix inbound/ --expire-days 7

# 3. Secrets. The data key is the firm's: generate it, store it in their vault,
#    and paste it here once.
../.venv/bin/second-eye keygen                # -> DATA_KEY
openssl rand -base64 48                       # -> EDGE_SECRET
npx wrangler secret put DATA_KEY
npx wrangler secret put EDGE_SECRET
npx wrangler secret put ANTHROPIC_API_KEY     # a key created inside a workspace
```

Do not edit `cloudflare/wrangler.jsonc`: its names, ids and vars are
placeholders shared by every deployment. Make the deployment a tenant
(`second-eye tenant new <name> --address ... --domains ...`, "One deployment per
firm" below), put the D1 `database_id` and the `vars` (`MAIL_AGENT_ADDRESS`,
`FIRM_DOMAINS`, `ALLOWED_SENDERS`, `EDGE_URL`) in
`deployments/<name>/tenant.jsonc`, add `"primary": true` if it is the one
deploy.yml's first job deploys (and change that job's `--config`), and run
`second-eye tenant render <name>`. Every wrangler command for it then takes
`--config ../deployments/<name>/wrangler.jsonc`, the secrets above included.
Then:

```bash
# The Worker alone, from a laptop with no Docker. This has to come before the
# secrets, because a secret can only be put on a Worker that exists.
npx wrangler deploy --containers-rollout=none --config ../deployments/<name>/wrangler.jsonc
```

It prints the Worker's URL. Put that in `EDGE_URL`, set the secrets from step 3,
and push to main: the deploy workflow builds `../Dockerfile` and rolls the
container out. At this point the database and document store can already be
checked from a laptop, through the operator's door ("Operator access",
below): `DATABASE_URL=d1://`, `EDGE_URL` at the live Worker and
`OPERATOR_SECRET` in the shell, then `store.claim` and `thread.start`.
Then run `second-eye agents apply` against the same settings: `MANAGED_REVIEW_AGENT_ID`
must be the clientless reviewer (`agents/review.agent.yaml`), or a review
stops waiting for a tool nobody answers and degrades to the mechanical
findings.
Against the first deployment that took about 50 ms a statement.

**Email.** Sending first: `npx wrangler email sending enable <subdomain>`, which
adds SPF and DKIM records under the subdomain only, then
`npx wrangler email sending dns get <subdomain>` to see them. Receiving: check
`dig MX <domain>` before anything else. If the domain already receives mail
somewhere other than Cloudflare, enabling Email Routing on it will take that
mail over, so route a subdomain instead. Then add the rule:

```bash
npx wrangler email routing rules create <domain> --name "legal review agent" \
  --match-type literal --match-field to --match-value review@<subdomain> \
  --action-type worker --action-value legal-review-agent
``` Use a subdomain, so that the agent's sending reputation and the firm's
own are separate things.

Turn on subaddressing in the zone's Email Routing settings, so that
`review+anything@<subdomain>` reaches the same rule. The one-tap links in a
review's HTML body write to `review+t<token>@`, which is how a reply started
from a mailto: link, with no In-Reply-To, finds its conversation; without
subaddressing Cloudflare drops those replies.

**The 5 MiB limit.** Cloudflare carries 5 MiB per outgoing message, or 25 MiB
to an address verified in the account. A marked-up share purchase agreement can
pass 5 MiB once encoded. When it does, the lawyer still gets the findings, with
a sentence saying the file would not send. For a pilot, verify each lawyer's
address in the dashboard and set `CLOUDFLARE_MAX_MESSAGE_MB=25`.

The steps above are how the first deployment was set up, by hand. A firm's
deployment is made with `second-eye tenant`, below, which runs the same commands
against its own names.

## One deployment per firm

Each firm runs in a deployment of its own: its own Worker, container, D1
database, R2 bucket, Workflow, address, allowlist, `DATA_KEY` and Anthropic
agents and memory store. Nothing is shared but the code. A deployment is one
file, `deployments/<firm>/tenant.jsonc`: the firm's resource names and ids
and its complete `vars`. `second-eye tenant render` turns it into
`deployments/<firm>/wrangler.jsonc`, the config `wrangler deploy --config`
takes, taking everything else (the Worker's code, compatibility date,
container class, cron) from `cloudflare/wrangler.jsonc`. The rendered file is
committed, so what a firm runs is in the diff, and `tests/test_tenant.py`
fails if it is out of date or if two deployments share a Worker, database,
bucket, Workflow, address, agent or memory store.

The operator's own deployment is a tenant like the others, marked
`"primary": true` in its tenant file (Jim's is `deployments/jim/`, which is
private and not in the public repository). deploy.yml deploys it first, as
the canary, with the unsuffixed `CLOUDFLARE_API_TOKEN` and
`CLOUDFLARE_ACCOUNT_ID`, from its rendered `deployments/<name>/wrangler.jsonc`;
provision and offboard leave it alone. `cloudflare/wrangler.jsonc` itself
names nobody's deployment: its names, ids and vars are placeholders
(`example.com`, a zero database id), and its comments say what each var does.
Deploy a tenant's rendered config, never that file.

A new firm starts from the shared config's behaviour settings
(`REVIEW_MODEL`, `TRIAGE`, `MAX_EMAILS_PER_DAY`, retention, log level),
copied once into its own file, with `REPLY_POLICY=sender_only`,
`SERVICE_PAUSED=false` and `ONE_TAP_LINKS=false` whatever they are. Every
other var, and any var added to `cloudflare/wrangler.jsonc` later, starts
empty: an id is never inherited.

Adding a firm, from the repository root with Node and `cloudflare/` set up
(`npm ci`):

```bash
# 1. The tenant: deployments/acme-llp/tenant.jsonc and its wrangler.jsonc.
#    --senders defaults to the firm's domains; --jurisdiction eu keeps the
#    database and the documents in the EU; --account-id if the firm's own
#    Cloudflare account; --instance-type defaults to standard-1.
.venv/bin/second-eye tenant new acme-llp --address review@legal.acme.com \
  --domains acme.com,acme.co.uk --contact gc@acme.com --admins partner@acme.com
#    Read the vars it wrote (NO_AI_MATTERS, PLAYBOOK_ADMINS,
#    MAX_EMAILS_PER_DAY...), then `second-eye tenant render acme-llp`.

# 2. The firm's Anthropic key, in a file git ignores:
echo "ANTHROPIC_API_KEY=sk-ant-..." >> deployments/acme-llp/.env

# 3. The Cloudflare resources. A dry run first: every wrangler command, in
#    order, and nothing created.
.venv/bin/second-eye tenant provision acme-llp
CLOUDFLARE_API_TOKEN=<a token for the firm's account> \
  .venv/bin/second-eye tenant provision acme-llp --apply
```

`--apply` creates the D1 database (and writes its id into `tenant.jsonc`),
the R2 bucket with both 7-day lifecycle rules, deploys the Worker without its
container (`--containers-rollout=none`, so a secret has somewhere to go, and
writes the Worker's URL into `EDGE_URL`), then puts `DATA_KEY` and
`EDGE_SECRET`, generated there, and `ANTHROPIC_API_KEY` from the firm's
`.env`. Secrets go to wrangler on stdin, never on a command line. It stops at
the first command that fails; every step before it is safe to run again. A
secret the Worker already holds is never replaced: a second `DATA_KEY` would
make every stored document unreadable, so a rerun reuses the one in
`deployments/<firm>/.env` or leaves the Worker's alone.

**Back up the `DATA_KEY` at once.** It is written to
`deployments/<firm>/.env` (mode 600) and onto the Worker, and nowhere else.
Put it in the firm's own secret store (their password manager or KMS: the
firm holds it, DECISIONS 13) and keep a second copy offline with ours. It
never goes into GitHub; the deploy does not need it.

What `--apply` does not do, because each changes the firm's mail or DNS, or
needs a person at a console. It prints each one with its exact command:

1. Email: check `dig MX <subdomain>`, then `npx wrangler email sending enable
   <subdomain>` and the routing rule from the address to the firm's Worker
   (the "Email" step above, with the firm's names). Subaddressing on before
   `ONE_TAP_LINKS=true`.
2. The webhook: the firm's Console, Manage -> Webhooks, then
   `npx wrangler secret put ANTHROPIC_WEBHOOK_SIGNING_KEY --config
   ../deployments/<firm>/wrangler.jsonc`.
3. The agents: `second-eye live-check --tenant <firm>` (`--dry-run` first). It reads
   the firm's settings only, from `deployments/<firm>/.env` and its
   `tenant.jsonc`, hiding whatever the shell and the working directory's
   `.env` hold, so the firm's key creates the firm's skills, agents,
   environment and memory store, never touching Jim's. The ids go into the
   firm's `tenant.jsonc` and `.env`, and the config is rendered again.
4. GitHub: two repository secrets per firm, named after the tenant in
   capitals with `-` as `_`: `CLOUDFLARE_API_TOKEN_ACME_LLP` and
   `CLOUDFLARE_ACCOUNT_ID_ACME_LLP`. Then commit `deployments/acme-llp/` and
   push.

**How it deploys.** `.github/workflows/deploy.yml` deploys Jim's first,
exactly as before. Then `python3 -m secondeye.tenant matrix` lists every other
tenant in `deployments/`, and each is deployed in its own job with its own
two secrets: `npx wrangler deploy --config ../deployments/<firm>/wrangler.jsonc`,
then its `/health`. Jim's is the canary: a commit that fails there goes to
no firm. A firm whose secrets are not set, or whose database does not exist
yet, is skipped and stays green, as the whole workflow does in a repository
with no token. CI bundles every firm's config with `wrangler deploy
--dry-run`. Checking a firm's rollout is as for Jim's (below), with
`npx wrangler containers list` run under the firm's token to find its
application id.

`second-eye tenant list` shows every deployment and its state; `second-eye tenant check`
is what the test runs.

Not yet per firm: the document-system MCP server (`cloudflare/dms-mcp`) is
deployed for Jim's only. A firm that connects iManage needs its own copy.

### Self-hosted option

By default we host each firm's deployment, as above. A security-strict firm
can instead run Second Eye entirely in its own Cloudflare account and its
own Anthropic organisation, with no standing access for us. The firm's IT
follows `docs/it/self-hosted.md`. What changes for us:

- **The tenant.** `second-eye tenant new <firm> --self-hosted --account-id <theirs>
  [--anthropic-org ...] [--anthropic-workspace ...]` marks it. A self-hosted
  tenant is never in `deploy.yml`'s matrix, and the `tenants` job prints
  which firms it skipped and why. Its provisioning plan ends in a release
  rather than GitHub secrets, and the firm's IT runs it with the firm's own
  token.
- **Releases.** Pushing a tag `vX.Y.Z` runs `.github/workflows/release.yml`.
  It refuses a tag that is not on main. It attaches these files to a
  **draft** GitHub release:
  - a reproducible source archive (`src/secondeye/release.py`), which leaves out
    `deployments/` and blanks Jim's names in the shared config;
  - the Worker bundle (`wrangler deploy --dry-run`);
  - a config template;
  - the resolved Python requirements;
  - a CycloneDX SBOM (syft);
  - `release.json`;
  - `SHA256SUMS`.

  Publish the draft once CI on the tag is green. The firm runs
  `second-eye tenant deploy <firm> --release vX.Y.Z` from the unpacked archive. Its
  wrangler builds the container image from the release's `Dockerfile`.
  There is no prebuilt image and no signature beyond the tag, and
  `release.json` says so.
- **The kill switch without a deploy.** `second-eye pause <firm>` and
  `second-eye resume <firm>` write `edge_settings.service_paused` in the firm's D1
  with `wrangler d1 execute --remote`. Without `--apply` they are a dry run.
  The Worker reads that row on every message, send, cron run and Workflow
  step (`cloudflare/src/pause.ts`). This works for every tenant, Jim's
  included. The `SERVICE_PAUSED` var still works, and it wins.
- **Support.** `second-eye tenant support <firm>` prints the scoped, expiring
  Cloudflare token the firm creates for us when it wants help. The token is
  read-only unless the firm passes `--write`, and it never includes R2. The
  firm deletes it afterwards. There is no other way in.
- **Leaving.** `second-eye tenant offboard <firm>` is a dry run that lists
  everything. With `--apply`, run by the firm, it does the following:
  1. Pauses the service.
  2. Exports the audit trail.
  3. Purges every client.
  4. Deletes the deployment's objects at Anthropic. It archives the agents
     instead, because the API has no delete for them.
  5. Deletes the Worker, Workflow, container application, D1 database and R2
     bucket.
  6. Removes the local secrets.
  7. Writes a deletion certificate (`src/secondeye/offboard.py`): ids, counts and
     times, no contents, with an HMAC-SHA256 under a key the firm supplies.
     `second-eye tenant certificate <file>` checks it.

None of this has run against a real account yet.

## The Workflow

Every email that passes the edge's checks starts a Cloudflare Workflow, one
instance per email, which runs the review as separate, separately retried
steps (`cloudflare/src/review-flow.ts`, `src/secondeye/flow.py`). It replaced a
queue, its dead-letter queue, a 25-minute retry, the job lease and an
in-process slow-notice timer (`docs/migration.md`, phases 2 and 5).

```
Worker email()  allowlist, DMARC, daily cap -> seal to R2
  └ ReviewFlow instance, id rf-<sha256(message-id|sender)>   (review-flow.ts)
      prepare          container /prepare         replies that are not reviews are sent here
      session-start    container /session/start   then session -> instance in D1
      idle-N/status-N  waitForEvent("idle", <= 60 s), then container /session/status
      notice           once, if still running at 90 s: "Reviewing X; back in about N minutes"
      finish           container /finish          the reply, sealed in R2 (job/...)
      send             Email Service, through the outbox; without files if too big
      finished         container /finished        aliases, announcements, job row
      on failure: fail -> send-failure -> finished-failure; errored only if nobody was told
Worker POST /anthropic/webhook  verify -> dedupe -> sendEvent("idle") -> 204
Worker scheduled() daily        wake instances still waiting; purge past retention;
                                container /retention/sweep (retention.py)
```

- **Every message.** There is no other path and no flag: rollback is a git
  revert. Mail held while `SERVICE_PAUSED` is released into a Workflow named
  after its message, as if it had just arrived.
- **One email, one instance.** The instance id is a hash of the Message-ID and
  the sender, so a second delivery of the same message finds the id taken and
  is dropped with its stored copy.
- **One send per kind.** Email Service has no idempotency key, so every send on
  this path is named `<instance>:<kind>` (`reply`, `notice`, `failure`,
  `prepare-1`...) and goes through the `outbox` table in D1 (`outbox.ts`); a
  retried step gets the first send's id back. The container's own sends from
  `/prepare` carry the same kind of name through `/internal/send`. The one
  window left, the provider accepting the message and the write that records it
  failing, sends a second copy rather than losing the reply.
- **The webhook only wakes it sooner.** Anthropic tries a delivery three times
  and then drops it silently, so every wait times out after 60 seconds and
  polls, and the daily cron wakes anything still waiting. Once verified the
  endpoint always answers 204; an unsigned or wrongly signed request gets 401.
  Verification is the Anthropic SDK's own `beta.webhooks.unwrap()`, which is
  pure JavaScript and runs in the Worker (`webhook.ts`).
- **The session runner** is `src/secondeye/sessions_api.py`: the clientless session
  (`review.start`, `review.finish`). Anthropic's session id is what the webhook
  names, and the session outlives the container being replaced between steps.
- **Retries.** prepare 3 x 30 s, session-start 3 x 10 s, each poll 3 x 10 s,
  finish and finished 3 x 15 s, send 5 x 30 s. The container answers 422 for
  what a retry will not change (a failed review), and the step stops at once.

Turning it on:

```bash
# 1. The webhook. Console -> Manage -> Webhooks: add
#    https://<worker-url>/anthropic/webhook, subscribe to
#    session.status_idled and session.status_terminated, copy the whsec_
#    secret (shown once), then:
npx wrangler secret put ANTHROPIC_WEBHOOK_SIGNING_KEY
# Without it the endpoint answers 404 and the Workflow polls every minute.

# 2. Belt and braces for R2: job state is deleted when a reply is sent and
#    by the daily cron after 7 days; a lifecycle rule holds even if both fail.
npx wrangler r2 bucket lifecycle add legal-review-agent-docs \
  --name expire-job-state --prefix job/ --expire-days 7

# The workflows binding and the cron deploy with the Worker.
```

**Moving an older deployment over (phase 5).** A deployment that had the
queue keeps two queue consumers registered on the Worker, and `wrangler
deploy` adds and updates consumers but never removes one. The deploy workflow
detaches them first (`wrangler queues consumer remove`, idempotent). Once
that deploy is green, delete the queues themselves:

```bash
npx wrangler queues list                         # both should show no backlog
npx wrangler queues delete legal-review-inbound
npx wrangler queues delete legal-review-inbound-dead
```

and run `second-eye agents apply` before the first model-backed review, so
`MANAGED_REVIEW_AGENT_ID` is the clientless reviewer. If
`MANAGED_REVIEW_DETACHED_AGENT_ID` is still set it adopts that agent and says
to delete the setting.

Watch it with `npx wrangler workflows instances list legal-review-flow` and
`npx wrangler workflows instances describe legal-review-flow <id>`, which shows
each step, its attempts and its output.

## The document-system MCP server

`cloudflare/dms-mcp` is a second Worker, `legal-review-agent-dms-mcp`, with no
bindings and one secret. The deploy workflow deploys it beside the edge; with
nothing configured it refuses every tool call, so it is safe to have deployed
before the firm's iManage exists. What it does and why is in
`docs/migration.md`, phase 4, and `src/secondeye/dms_mcp.py`.

One-time setup, once there is an iManage instance and its OAuth client:

```bash
# 1. One signing key, shared by the MCP Worker and the container.
KEY=$(openssl rand -base64 32)
cd cloudflare/dms-mcp && echo "$KEY" | npx wrangler secret put DMS_MCP_SIGNING_KEY
cd .. && echo "$KEY" | npx wrangler secret put DMS_MCP_SIGNING_KEY
#    The iManage OAuth client secret, for the consent callback and for the
#    refresh Anthropic performs:
npx wrangler secret put DMS_CLIENT_SECRET

# 2. In cloudflare/dms-mcp/wrangler.jsonc: IMANAGE_BASE_URL, the firm's
#    iManage Work host. In cloudflare/wrangler.jsonc vars: DMS_PROVIDER=imanage,
#    DMS_CLIENT_ID, DMS_TOKEN_URL, DMS_AUTHORIZE_URL,
#    PUBLIC_BASE_URL (the edge URL; the callback is /oauth/callback),
#    DMS_MCP_BINDING=query and
#    DMS_MCP_URL=https://legal-review-agent-dms-mcp.<account>.workers.dev/mcp.
#    Push.

# 3. Console -> Manage -> Webhooks: subscribe the existing
#    /anthropic/webhook endpoint to vault_credential.refresh_failed as well.

# 4. With the same DMS_* settings in .env: `second-eye agents apply`. It gives the
#    reviewer and the associate the MCP server with an always_allow toolset,
#    and turns allow_mcp_servers on in the environment.
```

The first live run settles the one open question: whether Anthropic's vault
carries the session URL's query string through to the server. If a connected
lawyer's review on a known matter gets "No document-system credential came with
this request" (the query stopped the credential matching) or "No matter is
attached to this session" (the query was dropped), set
`DMS_MCP_BINDING=capability` and push; no code
changes on either side. That mode needs the lawyer's token in our own table,
so each lawyer replies "connect" once more after the switch.

## What is on the internet

| Path on the Worker's public hostname | Who it is for | What guards it |
| --- | --- | --- |
| `email()` (not HTTP) | Cloudflare Email Routing | allowlist, DMARC pass, daily cap |
| `/health` | anyone | answers `{"edge":"ok"}` and nothing else |
| `/anthropic/webhook` | Anthropic | the webhook signature (`webhook.ts`); 404 without `ANTHROPIC_WEBHOOK_SIGNING_KEY` |
| `/oauth/callback` | a lawyer's browser, connecting a DMS | 404 unless `DMS_PROVIDER` is set; a one-time state |
| `/internal/*` | an operator, for one job | 404, the same as a path that does not exist, unless `OPERATOR_SECRET` is set; then only a signed, fresh, single-use request |

The container's own way to D1, R2 and the email service is not in that table.
`ReviewContainer.outboundByHost` maps `edge.internal` to a handler in the
Worker, and the container runtime routes the container's requests for that
name to it inside Cloudflare (`@cloudflare/containers` outbound handlers;
`export { ContainerProxy }` in `index.ts` is what lets the runtime intercept).
`EDGE_SECRET` is still checked there, but it opens nothing from outside: a
leaked `EDGE_SECRET` on its own reaches nothing.

**What the container can reach.** With `CONTAINER_EGRESS=allowlist` (the
default; empty means it too) the container starts with `enableInternet:
false`, and every outbound request, HTTP and HTTPS, goes through the
Worker's `ContainerProxy`, which lets through only:

- `edge.internal`, handled in the Worker, never on the network;
- `api.anthropic.com`: the Messages API, Managed Agents sessions, files,
  skills, agents, vaults and memory stores, all on that one host;
- the host of `DMS_TOKEN_URL`, only while `DMS_PROVIDER` is set: the
  authorisation-code exchange in `/oauth/callback`, and token refresh;
- anything listed in `EGRESS_ALLOWED_HOSTS`.

Everything else gets a 520 from Cloudflare and never leaves. Nothing else is
needed: the document-system MCP server is called by Anthropic, not by the
container, and web search runs on Anthropic's side. To filter HTTPS by host
the Worker terminates it under a CA Cloudflare creates for each instance, at
`/etc/cloudflare/certs/cloudflare-containers-ca.crt`; the image's start
command adds it to certifi's roots as `SSL_CERT_FILE` when it is there.
`CONTAINER_EGRESS=open` puts the container back on the open internet with no
inspection: a rollback that needs a deploy but no code change. The edge is
reached as `edge.internal` either way.

**Deploy-time checks for this change (2026-10).** No new secret is needed.
After the rollout (check the image digest, "A deploy is not instant"):

1. `curl -si -X POST https://<worker-url>/internal/db -d '{}'` answers
   `404 not found`, with or without `Authorization: Bearer <EDGE_SECRET>`.
2. Email the agent from an allowed address, and check the tail as in "Who
   can send to it?" above.
3. The review completes and the reply arrives: the container reached D1 and
   R2 through `edge.internal`, and Anthropic through the allowlist. If the
   Workflow's steps fail with TLS errors or 520s, set
   `CONTAINER_EGRESS=open`, push, and read the container's logs.

**Operator access.** `second-eye purge`, `second-eye audit` or an immediate release
against production from a laptop go through `/internal/*` on the public
hostname, which exists only while `OPERATOR_SECRET` is set on the Worker:

```bash
openssl rand -hex 32                          # -> the secret, for this job only
npx wrangler secret put OPERATOR_SECRET       # paste it
export OPERATOR_SECRET=<the same value>      # in the shell, never in .env
export DATABASE_URL=d1:// EDGE_URL=https://<worker-url>
.venv/bin/second-eye purge --lawyer jane@firm.com
.venv/bin/python -c "from secondeye import edge; print(edge.call('/internal/release').json())"
npx wrangler secret delete OPERATOR_SECRET    # and the door is gone again
```

`src/secondeye/edge.py` signs every request: HMAC-SHA256 under `OPERATOR_SECRET`
of the method, the path, a timestamp, a one-time nonce and the body's hash
(`cloudflare/src/operator.ts`). The Worker refuses a request more than five
minutes off its clock, a nonce it has seen (kept in `edge_operator_nonces`,
emptied by the daily cron), and one whose path or body was changed. A
secret shorter than 32 characters keeps the door shut. The container is
never given `OPERATOR_SECRET`, and `EDGE_SECRET` does not open this door.

## A deploy is not instant

`wrangler deploy` reports success when the new image is registered, not when it
is serving. A container that is awake keeps running the old image until the
rollout replaces it, which took between one and two minutes here. An email sent
45 seconds after a green deploy was answered by the previous version, and
looked exactly like the fix not working. Before judging a change from a real
inbox, check `npx wrangler containers info <application id>`: `health.active`
back at 0 with one `healthy` instance means the old one has gone.

## Checking it works

```bash
curl https://<worker-url>/health                     # {"edge":"ok"}
npx wrangler tail                                    # then, from an allowed address:
```

Email `review@<subdomain>` a Word document. In the tail you should see the
email handler (with `sender authenticated by dmarc`), then the Workflow's
container steps (`/prepare`, `/session/start`, `/session/status`, `/finish`,
`/finished`), with the container's calls to `edge.internal` (`/internal/db`,
`/internal/blob` PUTs, `/internal/send`) handled by the outbound handler
between them. The reply arrives in the thread.
Reply "undo everything" to confirm the conversation was found again, which
exercises D1, R2 and thread matching together.

If nothing arrives: `npx wrangler workflows instances list legal-review-flow`
shows the email's instance and `... describe legal-review-flow <id>` the step
that failed, and the container's own logs are in the dashboard under
Containers.

## How it is tested

Four layers, because the failure modes are in different places.

1. **The whole suite, twice.** `SECOND_EYE_TEST_BACKEND=d1 pytest` forces every
   test's storage through `d1.Connection`, the JSON wire protocol and object
   storage, against a stand-in Worker (`tests/fake_edge.py`). Six hundred tests
   written against sqlite3 are the specification the adapter has to meet. CI
   runs both modes.
2. **The Worker itself.** Type-checked, and bundled by Wrangler against the
   real config, in CI. Locally, `wrangler dev --enable-containers=false` runs it
   with Cloudflare's D1 and R2 emulators. The storage layer was run against
   that: atomic claim, row counts and insert ids, documents
   sealed into R2 and back byte for byte, FTS5 search, deletion reaching the
   objects, and an email sealed by the Worker's WebCrypto opened by Python's
   `cryptography`.
3. **`tests/test_cloudflare.py`.** What only exists in this mode: what is
   readable without the key, key rotation, retention deleting objects, a
   message from the Worker through `/prepare` end to end.
   `tests/test_workflow_steps.py` drives the Workflow's container routes the
   way the Workflow does, holds the reply to the one `handler.handle()` sends
   for the same message in one process, and replies to a review, a degraded
   review and a notice through the same steps.
4. **The Workflow, in workerd.** `cd cloudflare && npm test` runs
   `@cloudflare/vitest-plugin` (the successor to vitest-pool-workers) with the
   real `ReviewFlow`, D1, R2 and Workflows, a scripted stand-in for the
   container and a recording one for the Email Service (`cloudflare/test/`).
   `introspectWorkflowInstance` delivers the webhook's event (`mockEvent`),
   loses it (`forceEventTimeout`), fails steps (`mockStepError`) and fakes the
   clock (`mockStepResult` on a poll). It covers the step order, the notice
   never following the verdict, the deadline, every failure ending in one
   email, retried sends not sending twice, the size fallback, the signed
   webhook waking a waiting instance, and the daily sweep.

Not yet run against live Cloudflare or Anthropic: a real Workflow instance
calling the real container; a real `session.status_idled` delivery reaching
`/anthropic/webhook` and verifying; `E_CONTENT_TOO_LARGE` being the code the
Email Service binding actually throws, with its `code` intact across the
binding; and the daily cron firing.

Proved by the first production email on 2026-09-21: the container image build
on a GitHub runner, the container reaching its Worker (then over the public
internet; since 2026-10 through its outbound handler, not yet proved live),
and the Email Service in both directions. Not yet tested, because
no model-backed review has run in production: real D1 latency under a full
review (each statement is a round trip, and a review makes thirty to forty),
and the Email Service's behaviour around `Message-ID` across a multi-message
thread, which is why threads are also matched on the References chain.

## What changed in the application to make this possible

`store.connect()` was already the only way anything reached the database, from
42 call sites in 5 modules, using a small part of sqlite3. `src/secondeye/d1.py`
implements that part over HTTP, so nothing above it changed. Two things are
different on D1 and worth knowing:

- **A `with connect()` block is no longer one transaction.** Each statement
  commits alone. Eight blocks make more than one write. Each was checked: they
  are idempotent on retry (`add_alias`, `record_questions`, `oauth`), or a
  partial write is harmless because the parent row is written last or the
  children are deleted first (`thread.purge`, `archive.purge`, and
  `archive.store`, which was reordered for exactly this), or the second write only deactivates rows that a later
  rebuild ignores anyway (`supersede`). `record_changes` can leave a change
  recorded without its sibling; the rebuild applies what is there and the email
  lists what landed, so the lawyer sees the truth either way. Claiming a job,
  the one operation that must be atomic, is a single statement and always was.
- **Documents are not in the database.** D1 rows stop at 2 MB. `blobs.py` puts
  the bytes in R2 and a reference in the column. Locally the column still holds
  the bytes, so a laptop needs no object store.
