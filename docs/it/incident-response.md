# Incident response, breach notice and offboarding

The runbook for when something goes wrong, and for when a firm leaves. Every
command here exists in the code today unless it says otherwise. It has not
yet been exercised in a drill. The notification commitments are a template
for the founder and each firm to agree (**[Founder decision]**).

Placeholders: `<firm>` is the tenant name in `deployments/`; commands that
start `npx wrangler` run from `cloudflare/` with a Cloudflare API token for
the firm's account.

## Who does what

| Role | Today | Customer-hosted |
| --- | --- | --- |
| Incident lead at Second Eye | The founder | The founder, on the firm's request |
| Firm contact | `ALLOWLIST_CONTACT` and the firm's security team | Same |
| Can pause the service | Whoever can deploy the firm's tenant (the founder; anyone with push to `main`) | The firm, from its Cloudflare account, as well |
| Can cut mail off | The firm (transport rule) and whoever holds the Cloudflare account (routing rule) | The firm |
| Can revoke model access | Whoever holds the Anthropic organisation | The firm |
| Can withdraw the key | Whoever holds the Worker's secrets and every copy of `DATA_KEY` | The firm |

## Detection sources

What exists, and what would show an incident:

| Source | Where | Shows |
| --- | --- | --- |
| Worker logs | Cloudflare dashboard, Workers Observability (on in `cloudflare/wrangler.jsonc`), or `npx wrangler tail --config ../deployments/<firm>/wrangler.jsonc` | Drops ("dropped mail from a sender not on the allowlist", "...did not authenticate"), refused sends ("refused a send"), held mail, internal endpoint errors |
| Workflow instances | `npx wrangler workflows instances list <workflow>` | A failed review, and which step failed |
| Audit trail | `second-eye audit --since <date> --format csv` | Who sent what, when, what ran, where the reply went (`reply_to`), outcome. A reply addressed to anyone but the sender is the signal of a policy failure |
| Purge history | `second-eye purge --history` | Every purge: who, scope, counts, finished or not |
| Anthropic Console | The organisation that owns the key | Sessions turn by turn, usage and spend, API key activity |
| Spend | Anthropic prepaid balance; Cloudflare budget alert | A loop or abuse |
| The firm's mail logs | Exchange message trace, Mimecast or Proofpoint logs | What was copied to the agent and what came back |
| GitHub | Repository commits and Actions runs | Every configuration change and deploy |
| People | Lawyers and the firm's IT | A reply that looks wrong, or arrives where it should not |

Not built: alerting of any kind, and a SIEM feed (`questionnaire.md`, 5).

## Containment levers, fastest first

| Lever | Who | How | Takes effect | Effect |
| --- | --- | --- | --- | --- |
| **Disable the firm's transport rule** | Firm IT | `Disable-TransportRule -Identity "Second Eye: copy pilot outbound documents"` (`mail-flow.md`) | As fast as Exchange applies a rule change | No more BCC copies. Forwarded mail still arrives |
| **Delete or disable the Email Routing rule** | Holder of the Cloudflare account | Dashboard: the zone → Email → Email Routing → rules, or `npx wrangler email routing rules ...` | When Cloudflare applies it; not timed by us | Nothing reaches the Worker. Senders may get bounces, **not verified** |
| **Revoke the Anthropic API key** | Holder of the Anthropic organisation | Console → API keys | Immediately for new requests, per the Console | No model calls. Reviews fall back to the deterministic checks, or the "I couldn't review" email |
| **Kill switch `SERVICE_PAUSED`** | Whoever can deploy the tenant | Set `"SERVICE_PAUSED": "true"` in `deployments/<firm>/tenant.jsonc`, `second-eye tenant render <firm>`, commit, push to `main`. CI runs, then `deploy.yml` deploys. In an emergency, deploy that tenant directly: `npx wrangler deploy --config ../deployments/<firm>/wrangler.jsonc`, then commit the same change at once, or the next deploy from `main` undoes it | The Worker enforces it as soon as its new version is live: the end of the deploy. The container picks it up only when its rollout replaces the old instance, which took one to two minutes in production (`docs/deploy-cloudflare.md`, "A deploy is not instant"), but the Worker already refuses every send. Through CI it also waits for the whole test suite; we have not timed that end to end | Mail accepted and held sealed in R2, never bounced; nothing reviewed; nothing sent (the Worker refuses every send while paused). Switched back, held mail is released by the daily cron or `POST /internal/release` |
| **Withdraw `DATA_KEY`** | Holder of the Worker's secrets and every copy | `npx wrangler secret delete DATA_KEY --config ../deployments/<firm>/wrangler.jsonc`, and destroy every other copy (`deployments/<firm>/.env`, the firm's vault) | When the Worker's new version is live | Everything sealed (documents, held mail, Workflow state, DMS tokens) is unreadable for good. The container refuses to start; new mail should fail at the Worker's seal step (not tested). **Irreversible.** D1 rows are not sealed and are not affected: purge or delete them too |

A no-deploy kill switch (an admin email, `second-eye pause`, or a KV flag) is
**not built**. `second-eye selftest` shows the switch's state.

## Scenarios

**A secret leaks.**

- `EDGE_SECRET`: whoever has it can run SQL on D1 and send mail as the agent
  through `/internal/*` while those endpoints are on the internet. Pause,
  then put a new one: `openssl rand -base64 48 | npx wrangler secret put
  EDGE_SECRET --config ...`, update `deployments/<firm>/.env`, redeploy so
  the container takes it. Read the Worker logs and the audit trail for the
  window. Moving `/internal/*` off the internet (in progress) removes this
  path.
- `DATA_KEY`: rotation reads but cannot yet re-seal: set the new key as
  `DATA_KEY` and the old one as `DATA_KEY_PREVIOUS`. What was sealed under
  the old key stays readable by whoever holds it **and** the storage until
  it is deleted or re-sealed; there is no re-seal command. Purge what must
  not stay exposed. Treat this as notifiable if storage access is also
  suspected.
- The Anthropic API key: revoke it in the Console, create a new one, put it
  (`npx wrangler secret put ANTHROPIC_API_KEY --config ...`), redeploy.
  Review the Console for sessions not in the audit trail.
- A Cloudflare API token or GitHub access: revoke it in Cloudflare or
  GitHub, rotate the GitHub Actions secrets, and read the GitHub and
  Cloudflare audit logs for what it was used for.

**A reply went somewhere it should not.** Under `REPLY_POLICY=sender_only`
the application readdresses every send to the sender and the Worker refuses
any send not to exactly one allowlisted sender, so this needs two failures.
Pause; find the job in `second-eye audit` (`reply_to`, `reply_sent_at`); recover
the message with the recipient's firm if possible; then notify (below).

**A forged sender got a review.** The allowlist admits by address or domain
and the DMARC drop catches only an explicit fail (`architecture.md`). Pause
if it repeats; check the message headers in the firm's own logs; tighten
`ALLOWED_SENDERS` to exact addresses; ensure the firm's domains publish
DMARC `p=reject`. Requiring a DMARC pass at the Worker is in progress.

**A client says its guidelines forbid AI and its documents went through.**
Add it to `NO_AI_MATTERS` (by deploy) or have an admin email "no AI for
<client>"; then `second-eye purge --client <number>` (or `--matter`) with
`--apply`, which deletes Anthropic's sessions, files and memory stores for
that scope as well as ours; keep the purge record for the client.

**A vendor incident.** Anthropic commits to notify its customer within 48
hours of becoming aware of a security breach
([DPA, G.1](https://www.anthropic.com/legal/data-processing-addendum)).
Cloudflare's notice terms are in the firm's or our Cloudflare agreement.
Customer-hosted, the vendor notifies the firm directly.

## Notification

What a firm's outside-counsel guidelines usually demand is notice within 24
to 72 hours of becoming aware. **The window is the firm's choice**, set in
the agreement; this is the template.

> **Security incident notice.** Second Eye will notify the Customer's
> named security contact **[name, email, phone]** without undue delay and in
> any event within **[24 / 48 / 72]** hours of becoming aware of a security
> incident affecting Customer Data, including one at a sub-processor that
> Second Eye is notified of. The notice will say what is known of what
> happened, when, which data and which lawyers, clients or matters are
> affected (from the audit trail), what has been done to contain it, and
> who to contact; and it will be updated as more is known. Second Eye will
> keep the logs and audit rows that bear on the incident for **[90]** days,
> or longer if the Customer asks, and will not purge them meanwhile.

Inside that window, in order: contain (the levers above); preserve
(`second-eye audit` export for the period, Worker logs, Workflow instance details,
the Anthropic session ids from the audit rows, before any purge); assess
(which jobs, lawyers, clients); notify; remediate; write up.

Not in place yet: a security contact address, a status page, or a signed
notice commitment. **[Founder decision]**

## Offboarding a firm

There is no single `second-eye tenant offboard` command yet, and no signed
deletion certificate. The steps, in order, with what exists:

1. **Stop new mail.** The firm disables its transport rule; the routing rule
   for the agent's address is deleted; `SERVICE_PAUSED=true` meanwhile.
2. **Export what the firm keeps.** `second-eye audit --since <first day> --format
   csv` (the supervision record). Replies and their files are already in the
   lawyers' mailboxes.
3. **Purge, Anthropic included.** For each lawyer who used it
   (`ALLOWED_SENDERS`, and senders in the audit trail):
   `second-eye purge --lawyer <address> --apply --by <name>`, and for each
   registered client `second-eye purge --client <number> --apply`. This deletes
   conversations, documents and versions, closings, archived mail, memory
   notes and suppressions, job rows, and at Anthropic each session (with its
   files), each uploaded file and each memory store for that scope; it runs
   from a machine with the firm's `DATABASE_URL=d1://`, `EDGE_URL`,
   `EDGE_SECRET`, `DATA_KEY` and `ANTHROPIC_API_KEY`. Run each again until
   it reports nothing left; `second-eye purge --history` is the record.
4. **Delete the rest at Anthropic.** In the firm's (or our) Anthropic
   organisation: archive or delete the agents and the environment named in
   the tenant's vars, delete the firm memory store
   (`MANAGED_FIRM_MEMORY_STORE_ID`) and the uploaded skills, delete any
   remaining sessions, then revoke the API key. If the organisation was the
   firm's own, the firm can close it. Anthropic deletes customer data within
   thirty days of termination of its agreement, with stated exceptions
   ([DPA, H.1](https://www.anthropic.com/legal/data-processing-addendum)).
5. **Delete the Cloudflare resources.** From `cloudflare/`, with the firm's
   token: delete every object in the R2 bucket, then the bucket
   (`npx wrangler r2 bucket delete <bucket>`); the D1 database
   (`npx wrangler d1 delete <database>`); the Worker and its container
   (`npx wrangler delete --config ../deployments/<firm>/wrangler.jsonc`);
   the Email Routing rule and sending records for the subdomain. **Verify**
   each command's current form with `npx wrangler <command> --help`; we have
   not run them against a tenant.
6. **Destroy the key.** Delete `DATA_KEY` from every place it was kept:
   `deployments/<firm>/.env`, the firm's vault, any offline copy. Anything
   sealed that survives anywhere (a backup) is then unreadable.
7. **Remove the deployment from the repository.** Delete
   `deployments/<firm>/` (which removes client names on `NO_AI_MATTERS`
   from the current tree; they remain in git history) and the GitHub
   secrets `CLOUDFLARE_API_TOKEN_<FIRM>` and `CLOUDFLARE_ACCOUNT_ID_<FIRM>`.
8. **Confirm in writing.** Until a certificate is generated, a letter
   listing steps 1-7 with dates, the purge ids from `second-eye purge --history`,
   and the Anthropic ids deleted. **[Founder decision]** on the form.

Customer-hosted, the firm runs steps 2-7 itself; Second Eye deletes any
copy of the firm's `.env` and confirms.
