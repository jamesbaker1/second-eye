/**
 * The edge of the legal review agent.
 *
 * The application is Python and runs in a container (../Dockerfile). A
 * container cannot bind to D1, R2, Queues or the email service; only a Worker
 * can. So this Worker does the four things that need a binding and nothing
 * else. It holds no product logic and reads no document body.
 *
 *   email()     a message arrives -> sealed into R2 -> a ReviewFlow instance
 *               starts for it (review-flow.ts), which carries it to a reply
 *   fetch()     /oauth/callback passed through to the container,
 *               /anthropic/webhook (webhook.ts), and /internal/* for an
 *               operator only, signed, and shut unless OPERATOR_SECRET is set
 *               (operator.ts)
 *   the container's outbound handler
 *               http://edge.internal/internal/{db,blob/*,send,release}: how the
 *               container reaches D1, R2 and the email service. It never
 *               leaves Cloudflare and is not on the public hostname at all
 *   scheduled() daily: wake Workflows a lost webhook left waiting, purge
 *               what is past its retention (here, and in the container's
 *               /retention/sweep), and release held mail
 *
 * Why a Workflow between the two halves: a review takes minutes, and an email
 * handler that waits minutes is an email that bounces. The Workflow runs each
 * stage as a step that is retried on its own, keeps what each step returned
 * across a container restart, and on a failure it cannot recover from sends
 * the one email the lawyer is owed. It replaced a queue, its dead-letter
 * queue and a job lease (docs/migration.md, phase 5).
 */

import { Container, getContainer } from "@cloudflare/containers";
import { env as workerEnv } from "cloudflare:workers";
import { headerFields, readHeaderBlock, senderAuthenticated, type Verdict } from "./auth";
import type { Env, Pointer } from "./env";
import { addressOf, HELD, isAllowed, parseHeaders, paused } from "./mail";
import { type Outbound, PolicyViolation, policed, purgeOutbox, sendOnce, toMessage } from "./outbox";
import { loadPause } from "./pause";
import { instanceId } from "./review-flow";
import { base64ToBytes, bytesToBase64, seal, unseal } from "./seal";
import { operatorEnabled, purgeOperatorNonces, sameSecret, verifyOperator } from "./operator";
import { anthropicWebhook, purgeWebhookEvents, sweep } from "./webhook";

export { ReviewFlow } from "./review-flow";
// The Worker entrypoint the container's outbound traffic is handed to. Without
// this export the runtime cannot intercept anything, and the container could
// reach neither the edge nor, in "allowlist" mode, the internet.
export { ContainerProxy } from "@cloudflare/containers";

/** Settings the application reads, forwarded only if they are set here. */
const FORWARDED = [
  "ANTHROPIC_API_KEY", "ANTHROPIC_WORKSPACE_ID", "REVIEW_MODEL", "AGENT_EFFORT",
  "MAIL_AGENT_ADDRESS", "MAIL_AGENT_NAME", "MAIL_AGENT_ALIASES", "FIRM_DOMAINS", "ALLOWED_SENDERS",
  "ALLOWLIST_CONTACT",
  "EDGE_SECRET", "DATA_KEY", "DATA_KEY_PREVIOUS",
  "MANAGED_REVIEW_AGENT_ID", "MANAGED_ASSOCIATE_AGENT_ID", "MANAGED_ENVIRONMENT_ID",
  "MANAGED_SESSION_BUDGET_CENTS", "MANAGED_FIRM_MEMORY_STORE_ID",
  "AGENT_TIME_BUDGET_SECONDS", "AGENT_REPORT_GRACE_SECONDS",
  "MANAGED_PLAYBOOK_AGENT_ID", "MANAGED_COMMENTS_AGENT_ID", "MANAGED_CLOSING_AGENT_ID",
  "MANAGED_BLACKLINE_AGENT_ID",
  "SANDBOX_SKILL_ID", "SANDBOX_PLAYBOOK_SKILL_ID", "SANDBOX_KEY_TERMS_SKILL_ID",
  "SANDBOX_COMMENTS_SKILL_ID", "SANDBOX_CLOSING_SKILL_ID", "SANDBOX_BLACKLINE_SKILL_ID",
  "SANDBOX_NEGOTIATION_SKILL_ID",
  "WEB_SEARCH_ENABLED", "REDLINE_AUTHOR", "MAX_ATTACHMENT_MB", "MAX_REVIEW_CHARACTERS",
  "INFERENCE_GEO", "ARCHIVE_ENABLED", "ARCHIVE_BLOBS", "ARCHIVE_RETENTION_DAYS",
  "THREAD_RETENTION_DAYS", "RETENTION_HOURS", "LEARN_FROM_OUTCOMES", "CLOUDFLARE_MAX_MESSAGE_MB",
  "DMS_PROVIDER", "LOG_LEVEL", "ONE_TAP_LINKS", "TRIAGE",
  // The document system over MCP and vaults (docs/migration.md, phase 4).
  "DMS_CLIENT_ID", "DMS_CLIENT_SECRET", "DMS_TOKEN_URL", "DMS_AUTHORIZE_URL",
  "PUBLIC_BASE_URL", "DMS_MCP_URL", "DMS_MCP_SIGNING_KEY", "DMS_MCP_BINDING",
  "DMS_MCP_BINDING_TTL_SECONDS",
  "REPLY_POLICY", "NO_AI_MATTERS", "PLAYBOOK_ADMINS", "PRIVILEGE_NOTICE", "ZERO_RETENTION",
  "SERVICE_PAUSED",
];

const MAX_INBOUND_BYTES = 25 * 1024 * 1024;

/**
 * The name the container calls this Worker by. It is not a real hostname:
 * `.internal` is reserved for private use and never resolves on the internet.
 * The container runtime hands every request to it to the outbound handler
 * below, inside Cloudflare, so the edge's database, document and send
 * endpoints exist only for the container and are not on the Worker's public
 * hostname. Plain HTTP because it never leaves the machine.
 */
export const EDGE_HOST = "edge.internal";

/** What the container may reach on the internet. */
export const ANTHROPIC_HOST = "api.anthropic.com";

export interface Egress {
  enableInternet: boolean;
  interceptHttps: boolean;
  allowedHosts?: string[];
}

/**
 * CONTAINER_EGRESS. "allowlist" (the default; empty means it too): the
 * container has no internet. Its outbound HTTP and HTTPS both pass through
 * the ContainerProxy entrypoint, which lets through only the edge (handled
 * here, never on the network), api.anthropic.com, the document system's token
 * endpoint when one is configured, and any host in EGRESS_ALLOWED_HOSTS.
 * Anything else is refused with a 520 before it leaves Cloudflare. HTTPS is
 * intercepted under Cloudflare's per-instance CA, which the image trusts at
 * start (Dockerfile). "open": the internet as before, a rollback that needs
 * no code change; the edge is reached the same way in both.
 */
export function egress(env: Env): Egress {
  const mode = String(env.CONTAINER_EGRESS ?? "").trim().toLowerCase();
  if (mode === "open") return { enableInternet: true, interceptHttps: false };
  const hosts = new Set([EDGE_HOST, ANTHROPIC_HOST]);
  const dms = String(env.DMS_PROVIDER ?? "none").trim().toLowerCase();
  if (dms && dms !== "none" && typeof env.DMS_TOKEN_URL === "string" && env.DMS_TOKEN_URL) {
    try {
      hosts.add(new URL(env.DMS_TOKEN_URL).hostname.toLowerCase());
    } catch {
      // A malformed URL reaches nothing; the token exchange will say so.
    }
  }
  for (const host of String(env.EGRESS_ALLOWED_HOSTS ?? "").toLowerCase().split(/[\s,]+/)) {
    if (host) hosts.add(host);
  }
  return { enableInternet: false, interceptHttps: true, allowedHosts: [...hosts] };
}

export class ReviewContainer extends Container {
  defaultPort = 8080;
  pingEndpoint = "health";
  // Billed while awake. Long enough that a reply a few minutes after a review
  // lands on a warm container, short enough that one email does not buy an
  // hour. A request in flight keeps it up however long the review takes.
  sleepAfter = "10m";
  enableInternet = egress(workerEnv as Env).enableInternet;
  interceptHttps = egress(workerEnv as Env).interceptHttps;
  allowedHosts = egress(workerEnv as Env).allowedHosts;
  envVars = {
    MAIL_PROVIDER: "cloudflare",
    DATABASE_URL: "d1://",
    ...Object.fromEntries(
      FORWARDED.filter((name) => typeof (workerEnv as Env)[name] === "string")
        .map((name) => [name, (workerEnv as Env)[name] as string]),
    ),
    // Not the public URL in the EDGE_URL var: the outbound handler's name.
    EDGE_URL: `http://${EDGE_HOST}`,
  };
}

/**
 * The container's way in: http://edge.internal/internal/... Only the
 * container can send a request here. It still carries EDGE_SECRET, so a
 * request that reached this handler some other way would get nothing.
 */
export async function fromContainer(request: Request, env: Env): Promise<Response> {
  const presented = request.headers.get("authorization") ?? "";
  if (!env.EDGE_SECRET || !(await sameSecret(presented, `Bearer ${env.EDGE_SECRET}`))) {
    return new Response("unauthorised", { status: 401 });
  }
  // The kill switch as it is now (pause.ts). This route does not pass through
  // fetch(), so without it a send would see whatever the last fetch() read.
  await loadPause(env);
  return internal(request, env);
}

ReviewContainer.outboundByHost = {
  [EDGE_HOST]: (request: Request, env: unknown) => fromContainer(request, env as Env),
};

export default {
  async email(message: ForwardableEmailMessage, env: Env): Promise<void> {
    // Everything that costs money is behind these checks: the container that
    // wakes, the model that runs, the reply that is sent. Cloudflare has no
    // spending cap, only an alert that arrives a day late, so the cap is here.
    //
    // A stranger is dropped, not refused. The address is visible whenever the
    // agent is CC'd, so the stranger is usually opposing counsel hitting
    // reply-all, and a bounce reading "only accepts mail from approved
    // senders" told them the firm runs its drafts past a machine.
    const sender = addressOf(message.headers.get("from") ?? message.from);
    if (!isAllowed(sender, env.ALLOWED_SENDERS)) {
      console.log("dropped mail from a sender not on the allowlist");
      return;
    }
    // A forged From passes the allowlist. Admit only what the sender's domain
    // vouched for, on Cloudflare's verdict (auth.ts), and drop the rest
    // silently before it is counted, or thirty forgeries use up the day's cap
    // and the lawyer's real mail bounces. Read from the raw header block,
    // because only the order of the headers there says which verdict is
    // Cloudflare's. A message within the size limit is read once, here.
    const raw = message.rawSize <= MAX_INBOUND_BYTES ? await new Response(message.raw).arrayBuffer() : null;
    const verdict = unauthenticated(raw ? new Uint8Array(raw) : await readHeaderBlock(message.raw), sender, env);
    if (!verdict.ok) {
      console.log("dropped mail whose sender domain did not authenticate:", verdict.why);
      return;
    }
    console.log("sender authenticated by", verdict.how, "; Cloudflare verdicts:", verdict.cloudflareVerdicts);
    // The kill switch. Accepted, not bounced: a bounce tells the sender, and
    // whoever reads it, that something is wrong. The message waits sealed in
    // R2 and is released when the switch is turned off (release()).
    if ((await loadPause(env)) && raw) {
      await env.DOCS.put(`${HELD}${crypto.randomUUID()}.eml`, await seal(raw, env.DATA_KEY));
      console.log("SERVICE_PAUSED: holding a message");
      return;
    }
    if (!raw) {
      message.setReject("Message too large. Send the document on its own, under 25 MB.");
      return;
    }
    const limit = Number(env.MAX_EMAILS_PER_DAY ?? "0");
    if (limit > 0 && (await countToday(env)) > limit) {
      message.setReject(`Daily limit of ${limit} messages reached. Try again tomorrow.`);
      return;
    }
    const key = `inbound/${crypto.randomUUID()}.eml`;
    await env.DOCS.put(key, await seal(raw, env.DATA_KEY));
    const pointer = { key, receivedAt: new Date().toISOString() };
    await startFlow(env, pointer, message.headers.get("message-id"), sender);
  },

  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    if (url.pathname === "/health") return Response.json({ edge: "ok" });
    await loadPause(env); // the kill switch as it is now, for every path below (pause.ts)
    if (url.pathname === "/anthropic/webhook" && request.method === "POST") {
      return anthropicWebhook(request, env);
    }
    // The only public path that reaches the container, so the only one a
    // stranger could use to keep it awake. Closed unless a document system is
    // actually configured to call it.
    if (url.pathname === "/oauth/callback") {
      const dms = (env.DMS_PROVIDER ?? "none").toLowerCase();
      if (dms === "none" || dms === "") return new Response("not found", { status: 404 });
      return getContainer(env.REVIEW).fetch(request);
    }

    // /internal/* is the container's, through its outbound handler, and is not
    // served here. From outside it answers exactly as a path that does not
    // exist, unless an operator has opened the signed door for a job
    // (operator.ts).
    if (!url.pathname.startsWith("/internal/") || !operatorEnabled(env.OPERATOR_SECRET)) {
      return new Response("not found", { status: 404 });
    }
    const checked = await verifyOperator(request, env.OPERATOR_SECRET, env.DB);
    if ("refused" in checked) {
      console.log("refused an operator request:", checked.refused);
      return new Response("unauthorised", { status: 401 });
    }
    console.log("operator request", request.method, url.pathname);
    return internal(checked.request, env);
  },
  async scheduled(_controller: ScheduledController, env: Env): Promise<void> {
    await loadPause(env); // pause.ts
    const swept = await sweep(env);
    console.log("swept review sessions", JSON.stringify(swept));
    await purge(env);
    await retention(env);
    const released = await release(env);
    if (released.released) console.log("released held mail", JSON.stringify(released));
  },
} satisfies ExportedHandler<Env>;

/** /internal/db, /internal/blob/*, /internal/send and /internal/release, for
 *  a caller already known to be the container or a signed operator. */
export async function internal(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  try {
    if (url.pathname === "/internal/db" && request.method === "POST") return await db(request, env);
    if (url.pathname === "/internal/send" && request.method === "POST") return await send(request, env);
    if (url.pathname === "/internal/release" && request.method === "POST") {
      return Response.json(await release(env));
    }
    if (url.pathname.startsWith("/internal/blob/")) {
      return await blob(request, env, decodeURIComponent(url.pathname.slice("/internal/blob/".length)));
    }
  } catch (error) {
    // Our own message only. A D1 error can quote the statement, never a document.
    console.error("internal endpoint failed", url.pathname, String(error));
    return Response.json({ error: String(error).slice(0, 300) }, { status: 500 });
  }
  return new Response("not found", { status: 404 });
}

/**
 * A ReviewFlow instance for one message, named after the message: a second
 * delivery of the same message finds the name taken and is dropped, with
 * its stored copy. Without a Message-ID there is nothing to dedupe on, and
 * the stored copy's random name is used.
 */
async function startFlow(env: Env, pointer: Pointer, messageId: string | null, sender: string): Promise<void> {
  const id = await instanceId(messageId?.trim() || pointer.key, sender);
  try {
    await env.REVIEW_FLOW.create({ id, params: pointer });
  } catch (error) {
    let exists = false;
    try {
      await (await env.REVIEW_FLOW.get(id)).status();
      exists = true;
    } catch {
      // not there: the create failed for some other reason
    }
    if (!exists) throw error;
    console.log("a message already under review arrived again; dropping it", id);
    await env.DOCS.delete(pointer.key);
  }
}

// How long a stored message or job state is kept when its instance never
// cleaned up (it was terminated, say). Both hold a document, so they get the
// window everything about a document gets (THREAD_RETENTION_DAYS, 7 by
// default), and the bucket's lifecycle rules say 7 days as well
// (`second-eye tenant provision`). Mail held by the kill switch (held/) is not
// here: it waits, sealed, until it is released.
const STORED_AFTER_DAYS = 7;
// How long the rows that hold only ids are kept: an outbox key, a webhook
// event id, a session-to-instance mapping. None holds a document, a name or
// an address; a key is only needed while its instance can retry, and
// instances are retained 30 days at most.
const IDS_AFTER_DAYS = 31;

/** The Worker's own retention, run by the daily cron. The application's
 *  (retention.py) is run by `retention` below. */
async function purge(env: Env): Promise<void> {
  const cutoff = Date.now() - STORED_AFTER_DAYS * 86_400_000;
  for (const prefix of ["inbound/", "job/"]) {
    let cursor: string | undefined;
    do {
      const listed = await env.DOCS.list({ prefix, cursor });
      const old = listed.objects.filter((o) => o.uploaded.getTime() < cutoff).map((o) => o.key);
      if (old.length) await env.DOCS.delete(old);
      cursor = listed.truncated ? listed.cursor : undefined;
    } while (cursor);
  }
  await purgeOutbox(env, IDS_AFTER_DAYS);
  await purgeWebhookEvents(env, IDS_AFTER_DAYS);
  await purgeOperatorNonces(env.DB);
}

/**
 * The application's retention sweep (src/secondeye/retention.py): conversations,
 * closings, unconfirmed notes and Anthropic's sessions past their window.
 * It used to ride the write path alone, so a quiet week deleted nothing.
 * A failure is logged and the rest of the cron carries on; tomorrow's runs
 * it again.
 */
async function retention(env: Env): Promise<void> {
  try {
    const response = await getContainer(env.REVIEW).fetch(new Request("http://container/retention/sweep", {
      method: "POST",
      headers: { authorization: `Bearer ${env.EDGE_SECRET}` },
    }));
    const body = (await response.text()).slice(0, 500);
    if (response.ok) console.log("retention sweep", body);
    else console.error("retention sweep failed", response.status, body);
  } catch (error) {
    console.error("retention sweep failed", String(error));
  }
}

// ---------------------------------------------------------------------------
// /internal/db: a batch of statements in, one result per statement out.
// D1 runs a batch as a single transaction, so a multi-statement request is
// atomic even though the application's `with` blocks no longer are.
// ---------------------------------------------------------------------------

interface Statement {
  sql: string;
  params: unknown[];
}

async function db(request: Request, env: Env): Promise<Response> {
  const { statements } = (await request.json()) as { statements: Statement[] };
  const prepared = statements.map((s) => env.DB.prepare(s.sql).bind(...s.params.map(decodeParam)));
  const results = await env.DB.batch(prepared);
  return Response.json({
    results: results.map((result) => {
      const rows = (result.results ?? []) as Record<string, unknown>[];
      const columns = rows.length ? Object.keys(rows[0]) : [];
      return {
        columns,
        rows: rows.map((row) => columns.map((column) => encodeValue(row[column]))),
        changes: result.meta?.changes ?? 0,
        last_row_id: result.meta?.last_row_id ?? null,
      };
    }),
  });
}

function decodeParam(value: unknown): unknown {
  if (value && typeof value === "object" && "$bytes" in value) {
    return base64ToBytes((value as { $bytes: string }).$bytes).buffer;
  }
  return value;
}

function encodeValue(value: unknown): unknown {
  // D1 hands a BLOB back as an ArrayBuffer or as an array of byte values. No
  // column here stores a JSON array natively, so an array is always bytes.
  if (value instanceof ArrayBuffer) return { $bytes: bytesToBase64(new Uint8Array(value)) };
  if (ArrayBuffer.isView(value)) {
    return { $bytes: bytesToBase64(new Uint8Array(value.buffer, value.byteOffset, value.byteLength)) };
  }
  if (Array.isArray(value)) return { $bytes: bytesToBase64(Uint8Array.from(value as number[])) };
  return value;
}

// ---------------------------------------------------------------------------
// /internal/blob/<key>: documents. Already sealed by the application.
// ---------------------------------------------------------------------------

async function blob(request: Request, env: Env, key: string): Promise<Response> {
  if (!/^(thread|archive|doc|job)\/[0-9a-f]{32}$/.test(key)) {
    return new Response("bad key", { status: 400 });
  }
  if (request.method === "PUT") {
    await env.DOCS.put(key, await request.arrayBuffer());
    return Response.json({ stored: key });
  }
  if (request.method === "GET") {
    const object = await env.DOCS.get(key);
    return object ? new Response(object.body) : new Response("missing", { status: 404 });
  }
  if (request.method === "DELETE") {
    await env.DOCS.delete(key);
    return Response.json({ deleted: key });
  }
  return new Response("method not allowed", { status: 405 });
}

// ---------------------------------------------------------------------------
// /internal/send: the reply.
// ---------------------------------------------------------------------------

async function send(request: Request, env: Env): Promise<Response> {
  let out = (await request.json()) as Outbound;
  try {
    out = policed(env, out);
  } catch (error) {
    if (!(error instanceof PolicyViolation)) throw error;
    console.error("refused a send", error.message);
    return Response.json({ error: error.message }, { status: paused(env) ? 503 : 403 });
  }
  // On the Workflow path the container names each send, and a retried step
  // gets the first send's id back instead of a second email (outbox.ts).
  if (out.outbox) {
    if (!/^[A-Za-z0-9_-]{1,100}:[a-z0-9-]{1,40}$/.test(out.outbox)) {
      return new Response("bad outbox key", { status: 400 });
    }
    return Response.json(await sendOnce(env, out.outbox, out));
  }
  const result = await env.EMAIL.send(toMessage(out));
  return Response.json({ messageId: result.messageId });
}

// ---------------------------------------------------------------------------
// The kill switch (SERVICE_PAUSED)
// ---------------------------------------------------------------------------

/**
 * Everything held while paused starts a Workflow, oldest first, once the
 * switch is off: from the daily cron, or at once by POST /internal/release.
 * Each is named after its message as a new arrival is, so a message held
 * twice is still reviewed once.
 */
async function release(env: Env): Promise<{ released: number; paused: boolean }> {
  if (paused(env)) return { released: 0, paused: true };
  let released = 0;
  let cursor: string | undefined;
  do {
    const listed = await env.DOCS.list({ prefix: HELD, cursor });
    const objects = [...listed.objects].sort((a, b) => a.uploaded.getTime() - b.uploaded.getTime());
    for (const o of objects) {
      const object = await env.DOCS.get(o.key);
      if (!object) continue;
      const sealed = await object.arrayBuffer();
      const headers = parseHeaders(await unseal(sealed, env));
      const key = `inbound/${crypto.randomUUID()}.eml`;
      await env.DOCS.put(key, sealed);
      await startFlow(env, { key, receivedAt: o.uploaded.toISOString() },
        headers.get("message-id") ?? null, addressOf(headers.get("from") ?? ""));
      await env.DOCS.delete(o.key);
      released += 1;
    }
    cursor = listed.truncated ? listed.cursor : undefined;
  } while (cursor);
  return { released, paused: false };
}

// ---------------------------------------------------------------------------
// Spending guards
// ---------------------------------------------------------------------------

/** Cloudflare's verdict on the sender's domain (auth.ts). DKIM_ONLY_DOMAINS
 *  lists firm domains with no DMARC record yet, admitted on aligned DKIM. */
function unauthenticated(raw: Uint8Array, sender: string, env: Env): Verdict {
  return senderAuthenticated(headerFields(raw), sender, env.DKIM_ONLY_DOMAINS);
}

/** Counts this message and returns today's total, in one atomic statement. */
async function countToday(env: Env): Promise<number> {
  const day = new Date().toISOString().slice(0, 10);
  const [, counted] = await env.DB.batch([
    env.DB.prepare("CREATE TABLE IF NOT EXISTS edge_usage (day TEXT PRIMARY KEY, emails INTEGER NOT NULL)"),
    env.DB.prepare(
      "INSERT INTO edge_usage (day, emails) VALUES (?, 1) " +
      "ON CONFLICT(day) DO UPDATE SET emails = emails + 1 RETURNING emails",
    ).bind(day),
  ]);
  return Number((counted.results?.[0] as { emails: number } | undefined)?.emails ?? 0);
}
