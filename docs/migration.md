# Migration: clientless sessions, durable orchestration, the model decides

Planned 2026-09-28. Three changes, made behind flags, in the order below;
the flags and the paths they chose between were removed in phase 5
(2026-09-29), so what is described below as "behind a flag" is now simply how
it works.

1. **The review session runs with no client attached.** The agent writes its
   findings and edits as files, and Anthropic calls us back when it is done.
2. **A Cloudflare Workflow runs each email end to end.** It replaces the queue,
   the job lease, the 25-minute retry and the slow-notice timer.
3. **The model makes every decision** (DECISIONS 30). The deterministic code
   stays, as evidence and tools the model is given, not as rules that override
   it.

Nothing here has been run against the live API, because the account has no
credit. Phase 0 comes first for that reason.

## Why

Today the container holds the session's event stream open for up to about
six minutes, because the custom tools (`report_findings`, the DMS lookups,
`note_for_next_time`) need a connected client to answer them. That one fact
is behind the heartbeats, reconnect-and-dedupe, deadline timers, lease expiry
and the duplicate-reply guards in `managed.py`, `handler.py` and `store.py`.
Take the custom tools out of the session and the session no longer needs us
until it is finished.

## Target shape

```
mail → Worker email()   allowlist, DMARC, daily cap (unchanged) → seal to R2
  └ ReviewFlow instance, id = hash(message-id, sender)
      1 triage       Messages API call: the model reads the email, the
                     attachments, the thread and the evidence, and returns a
                     plan (intents, document, recipients, answers, undo ids)
      2 prepare      container: extract, mechanical checks, since-last-time
      3 start        container: create the session, document mounted
      4 wait         waitForEvent("idle", 60 s) → on timeout, poll the session
      5 finish       container: fetch findings.json, notes.json, the redline;
                     verification report handed to the model's decision
      6 send         outbox guard (one send per instance and kind)
      7 finished     container: aliases, announcements, job row
      sleep 90 s ∥ wait → one "Reviewing X; back in about N minutes" step
Worker POST /anthropic/webhook → verify → sendEvent("idle") → always 2xx
Cron (daily): sweep sessions a lost webhook left waiting; retention purge
```

## Phase 0: one live baseline (blocked on credit)

`second-eye skills sync`, `second-eye agents apply`, then `second-eye eval --live --budget-usd 40`
and one real email. `second-eye live-check` runs all of it but the email, in order,
within one budget. Every unknown below that says "needs a live run" is
settled here, and the scorecard gives a before number for each later phase.

## Phase 1: the session needs no client (`REVIEW_TRANSPORT=detached`)

Built 2026-09-28 and tested only against the fake in `tests/fake_sessions.py`
(`tests/test_detached.py`). `review.start` and `review.finish` are the two halves the Workflow's start
and finish steps call; `review.review` runs both in one process and polls.
Since phase 5 it is the only reviewer. First live
run: `second-eye agents apply` (it now creates the detached reviewer and prints
`MANAGED_REVIEW_DETACHED_AGENT_ID`), then `second-eye review --live --detached
<file.docx>` beside `second-eye review --live` on the same file.

- The agent writes `/mnt/session/outputs/findings.json`, the redline and
  `notes.json`. The schema moves out of `review.REPORT_TOOL` into
  `skills/lra-document-tools/findings.schema.json`, and a test holds it equal
  to `models.Finding`.
- A skill script, `validate_findings.py`, writes `findings.json` only when
  every finding validates, and otherwise prints today's "NOTHING WAS
  RECORDED" message. The correction loop moves into the sandbox. The prompt
  says to write a draft early, so a session that times out leaves one behind.
- A new agent definition, `agents/review-detached.agent.yaml`, has no custom
  tools. A custom tool would leave a clientless session waiting for ever.
- Notes: `memory.accept_agent_notes()` applies today's rules (no firm scope,
  300 characters, matter notes need a matter) and stores them pending.
- Finish: retrieve the session until it is no longer `running`, read the last
  idle's `stop_reason`, and handle `end_turn`, `budget_reached` (a partial
  review) and `retries_exhausted` (a mechanical-only reply). Fetch outputs by
  session scope, retrying for the indexing lag. If the file is missing or
  invalid, send one correction message, then degrade as today.
- `second-eye review --live --detached` polls, so it runs locally with no webhook.
- Skills stay on `second-eye skills sync`, pinned to the versions it returns, rather
  than loaded from GitHub. Discovery wants `.claude/skills/`, the document
  tools depend on bundled modules, and a repository mount puts a token and the
  whole repo beside a client's document.

## Phase 2: the Workflow (the canary flags were removed in phase 5)

Built 2026-09-28, behind the flag, not yet run live; `docs/deploy-cloudflare.md`
describes it as deployed. The session runner is `sessions_api.py`: with
`REVIEW_TRANSPORT=detached` it is Phase 1's `review.start`/`review.finish`,
otherwise today's streaming review on a thread in the container.

- New `cloudflare/src/review-flow.ts`, `webhook.ts`, `outbox.ts`, `seal.ts`;
  a `workflows` binding, a cron trigger, and the secret
  `ANTHROPIC_WEBHOOK_SIGNING_KEY` in `wrangler.jsonc`.
- Container endpoints `/prepare`, `/session/start`, `/session/status`, `/finish`, `/finished` and
  `/fail`, over a sealed job state in R2 (`src/secondeye/jobstate.py`). `/jobs`
  stays for the queue path and `second-eye replay`.
- The webhook is only a way to wake up sooner. Anthropic tries it three times
  and then drops it without telling anyone, so every wait has a 60-second
  timeout followed by a poll, and the daily sweep catches the rest.
- Sends go through an `outbox(key, state, message_id)` row keyed on
  `instance:kind`. Email Service has no idempotency key. The one window left
  (the provider accepted the message, then the write failed) errs towards a
  second copy rather than a lost reply.
- Retries: prepare 3×30 s, start 3×10 s, finish 3×15 s, send 5×30 s with the
  no-attachment variant on a size error. Step wall-clock is unlimited; CPU
  per step is 30 s by default.
- Kept in D1, not in Durable Objects: `thread.resolve` finds a conversation by
  any of many message ids, scoped to its owner. A Durable Object has one name,
  and the python-docx ledger rebuild stays in Python anyway. The Agents SDK
  adds nothing here.
- Tests: `@cloudflare/vitest-plugin` (vitest-pool-workers' successor) with `introspectWorkflowInstance`
  (`mockEvent`, `forceEventTimeout` for a lost webhook, `mockStepError`).
- Rollback, until phase 5: flip `ORCHESTRATOR` back. Since then, a git revert.

## Phase 3: the model decides (DECISIONS 30)

Triage built 2026-09-28 behind `TRIAGE=rules|shadow|model` (default `shadow`),
tested only with the call stubbed (`tests/test_triage.py`). `src/secondeye/triage.py`
makes the call and holds `rules_plan`, the rules' decision as the same plan;
shadow writes both, content-free, to the audit row's `triage` column; model
carries the plan out through `handler._dispatch`, which calls the handlers the
rules path calls. Never triaged: a no-AI client (matter, recipients, the
conversation's document, an attached Word file's parties). Recipients from a
plan take effect only under `REPLY_POLICY=model`. Scored with `second-eye eval
--triage --stub` (the rules' baseline) and `second-eye eval --triage --live`.
The other rows of the table below are not started.

Every decision now made by a regex or a rule is made by the model, from the
same evidence:

| Decision | Today | After | Evidence the model is given |
|---|---|---|---|
| What the email asks for | `intake`/`router` phrase lists | triage plan | email text, thread state, attachments |
| Which attachment is the document | `pick_document` | triage plan | name, type, size, tracked changes of each |
| Their paper or ours | `their_paper.detect` | triage plan | sender, authors, forward markers |
| Who the reply goes to | always the sender | triage plan | each address with internal/external, allowlisted, authenticated |
| What a reply means ("30", "undo 2", "B is fine") | `followup` + `router` | triage plan, by id | numbered changes, lettered findings, open questions |
| Which mechanical findings are real | all reported | the review keeps or drops each | the check's finding and the clause text |
| Whether a returned file is sound | `redline.verify` refuses | the model decides | the verification report, in full |
| The reply's words | `reply.compose` | the model writes it | sections, voice rules, rubric |

- Triage is one Messages API call with structured output
  (`output_config.format`), not a session: seconds, not minutes. The schema is
  the plan: intents, document choice, recipients, answers, undo ids,
  dismissals, instructions, and a one-line reason for each.
- The deterministic code stays as evidence and as tools. Checks still run
  first and are free, `verify` still runs on every file, and the internal and
  external flags still exist. What changes is that their outputs go to the
  model and the model's call stands.
- The edge keeps two things, because they happen before any model call: the
  allowlist and the DMARC drop are cost and intake filters, not decisions
  about a document. Say so if you want those model-decided too.
- The eval harness gains triage cases: forwarded chains, reply-all with a
  counterparty on CC, ambiguous undo, several attachments. Each rule-based
  path that is removed must be matched or beaten on the scorecard first.

## Phase 4: document-system access through MCP and vaults

Built 2026-09-28 behind `DMS_TRANSPORT=mcp` (the only path since phase 5), tested
against fakes only: no iManage instance exists and the account has no credit.
James approved Anthropic holding each lawyer's iManage tokens.

- `cloudflare/dms-mcp/`: a second Worker speaking MCP Streamable HTTP with the
  official TypeScript SDK (its web-standard transport runs in workerd; Ajv is
  swapped for the Cloudflare JSON Schema validator because workerd forbids
  `new Function`). It serves `search_firm_documents`, `read_firm_document` and
  `matter_history` with the custom tools' wording and rules, forwarding the
  bearer to iManage as `X-Auth-Token`.
- The matter binding: each session's MCP URL is
  `/mcp?u=<lawyer>&m=<matter>&exp=<unix>&sig=<HMAC>` under
  `DMS_MCP_SIGNING_KEY`, set per session by `sessions.create` with
  `agent: {type: agent_with_overrides, mcp_servers: [...]}`. The tools have no
  matter parameter; a URL whose matter or lawyer was changed is refused with a
  403; no matter, an expired binding or no credential gets a sentence the
  model can pass on.
- Consent (`src/secondeye/dms_mcp.py`): the callback exchanges the code as before
  and writes an `mcp_oauth` credential into the lawyer's own vault (one per
  lawyer, recorded in `dms_vaults`), with iManage's token endpoint as the
  refresh endpoint. "revoke" archives it. Sessions attach it with `vault_ids`
  (create-only).
- Agents whose manifest has `document_system: true` get the MCP server and an
  `mcp_toolset` with `always_allow` (the MCP default, `always_ask`, would idle
  a clientless session for ever) in place of the three custom tools, and the
  environment gets `allow_mcp_servers`. The detached reviewer has DMS access
  again.
- `vault_credential.refresh_failed` reaches `/anthropic/webhook`, which emails
  the lawyer "Reply connect to reconnect your document system."
- Under `mcp` the token table is not read or refreshed and the custom DMS tool
  path is gated off; `oauth.py` and `dms/` stay until phase 5.
- The fallback, `DMS_MCP_BINDING=capability`, for if the vault does not carry
  the query string through: the bare URL, and a vault made for the one session
  holding a `static_bearer` capability (lawyer, matter, expiry and the iManage
  token), deleted when the session closes or swept after expiry. The token
  comes from our table, so in that mode the table stays and phase 5 cannot
  delete `oauth.py`. The server accepts both forms, so switching is a setting.
- One-time setup: `docs/deploy-cloudflare.md`, "The document-system MCP
  server".

## Phase 5: remove the old paths (done 2026-09-29)

James asked for it before a live model run, after two real emails went
through the Workflow in production (a clean copy in 12 seconds; a review that
fell back correctly to the mechanical-only reply, the account having no model
credit) and with every email path covered by tests through the flow stages.
Rollback is a git revert, not a flag (DECISIONS 32).

A production canary found one bug first, fixed before anything was removed:
"30" in reply to that mechanical-only review was answered "I could not find a
document to review". The failure path kept the conversation only in
/finished, only for a conversation it had never seen, and the id Email
Service returns for a send need not be spelt as the Message-ID header the
lawyer's client quotes. `/fail` now keeps it as a finished review does
(`handler._hold`), a notice sent before the failure is aliased too, and
aliases are stored and matched with and without angle brackets.

Removed:

- **The queue.** `email()` starts a ReviewFlow instance for every admitted
  message (allowlist, DMARC, pause, daily cap and the deterministic instance
  id unchanged). `ORCHESTRATOR`, `WORKFLOW_SENDERS`, the `INBOUND` producer,
  the `queue()` consumer, the dead-letter consumer, `retry_delay` and
  `busyFor` are gone; the Workflow's failure path answers the sender from the
  headers when nothing was prepared, as the dead-letter consumer did. Held
  mail is released into a Workflow. The container's `/jobs` route is gone:
  `second-eye replay` and the tests call `handler.handle()` directly.
- **The streaming review.** `REVIEW_TRANSPORT` and the stream half of
  `review.review` (report_findings answered over the stream, `OUT_OF_TIME`,
  `REPORT_TOOL`, `CUSTOM_TOOLS`), `sessions_api.InProcessSessions` and its
  heartbeat, and the Workflow's 5-second poll for sessions no webhook could
  wake. `agents/review.agent.yaml` is now the clientless reviewer (it was
  `review-detached.agent.yaml`), with its prompt and rubric. `second-eye agents
  apply` updates the agent at `MANAGED_REVIEW_AGENT_ID` in place to it, or
  adopts the agent at `MANAGED_REVIEW_DETACHED_AGENT_ID` when that is still
  set. `second-eye review --live` polls; `--detached` is gone.
- **The custom DMS tools.** `DMS_TRANSPORT`, `DMS_BASE_URL`, the three lookups
  as custom tools and `src/secondeye/dms/`. The document system is the MCP server.
- **The timer and the lease.** `notice.SlowReviewNotice`, `notice.start`,
  `notice.Guarded`, the lease takeover in `store.claim`, `store.touch` and
  `LEASE_MINUTES`. The Workflow sends the notice (`notice.compose`), and
  retries a step under the same instance id (`store.claim_for`).

Kept, on purpose:

- **The rule-based routing** (`intake`, `router`, `followup`,
  `triage.rules_plan`). It is triage's fallback when the model's plan is
  missing or refused, and the baseline the live model has to beat on the
  scorecard (`second-eye eval --triage`: 85%, 40% on the hard cases). Removing it
  waits on a live triage run that beats it, per DECISIONS 30.
- **The stream driver in `managed.run_session`** (reconnect, dedupe, the
  hard-deadline watchdog, custom-tool answering). The associate
  (`make_changes`, `read_document`, `note_for_next_time`), the playbook reader
  (`record_playbook`), the closing agent and the convert and repair jobs
  still run with a client attached. Moving them to files is later work.
- **`oauth.py` and its token table.** The consent link is still ours, and
  `DMS_MCP_BINDING=capability`, the fallback if a vault drops the session
  URL's query string, embeds the lawyer's token from that table.
- **`handler.handle()`**, the whole message in one process, for `second-eye replay`
  and a mail vendor's webhook (`/webhooks/inbound`). It no longer sends a
  slow-review notice.

Deploy-time steps:

- `wrangler deploy` adds and updates queue consumers but never removes one,
  so `.github/workflows/deploy.yml` detaches both before deploying
  (`wrangler queues consumer remove`, idempotent). The queues themselves are
  deleted by hand once the deploy is green: `npx wrangler queues delete
  legal-review-inbound` and `legal-review-inbound-dead`. They were already
  empty: every allowlisted sender was on the canary.
- Before the first live model run, `second-eye agents apply` against production, so
  `MANAGED_REVIEW_AGENT_ID` is the clientless reviewer. Until then a review
  starts on the old streaming agent, stops on `requires_action`, and degrades
  to the mechanical-only reply.

## Later

- Long documents (ROADMAP O16): a coordinator agent that splits a document
  across copies of itself (`agents/review-long.agent.yaml`).
- Cost (O18): Sonnet 5 as the worker with an Opus 5 advisor, measured with the
  scorecard.
- `inference_geo` is wired, but offers only `us` or `global`. There is no EU
  pin, and the docs say nothing about where containers and files live.

## Unknowns only a live run settles

Whether the grader can read the outputs; whether a brief idle fires a
webhook; how late webhooks arrive; whether output filenames with spaces
survive; whether the TypeScript SDK's webhook `unwrap` runs on Workers (it does in
workerd locally, so the Python fallback was not needed; not yet against a
real delivery); whether vault matching ignores a query string;
iManage's refresh flow under `mcp_oauth`; whether Email Service can set a
Message-ID; and the latency of a webhook round trip compared with the stream
it replaces.
