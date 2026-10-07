/**
 * POST /anthropic/webhook: Anthropic saying a review session went idle or
 * ended, turned into an "idle" event on the Workflow instance waiting on it.
 *
 * The webhook is only a way to wake up sooner. Anthropic tries a delivery
 * three times and then drops it without telling anyone, so every wait in
 * review-flow.ts has a timeout followed by a poll, and the daily sweep below
 * wakes anything still waiting. That is why, once a request is verified, the
 * answer is always 2xx: a failure here costs a minute, and a stream of 5xx
 * would only get the endpoint auto-disabled.
 *
 * Verification is the SDK's own `client.beta.webhooks.unwrap()` (Standard
 * Webhooks: HMAC-SHA256 over "<webhook-id>.<webhook-timestamp>.<body>" with
 * the whsec_ key, five minutes' tolerance). It is pure JavaScript and runs
 * in workerd; test/webhook.test.ts runs it there. Only a request that fails
 * verification is refused.
 */

import Anthropic from "@anthropic-ai/sdk";
import type { Env } from "./env";
import { isAllowed, paused, PRIVILEGE_NOTICE } from "./mail";

/** The webhook `data.type`s that mean a session may have finished. */
const WAKES = new Set(["session.status_idled", "session.status_terminated"]);

const SESSIONS =
  "CREATE TABLE IF NOT EXISTS review_sessions (session_id TEXT PRIMARY KEY, " +
  "instance_id TEXT NOT NULL, created_at TEXT NOT NULL, done_at TEXT)";
const EVENTS = "CREATE TABLE IF NOT EXISTS webhook_events (id TEXT PRIMARY KEY, received_at TEXT NOT NULL)";

interface WebhookEvent {
  id?: string;
  type?: string;
  data?: { type?: string; id?: string; vault_id?: string };
}

export async function anthropicWebhook(request: Request, env: Env): Promise<Response> {
  const key = env.ANTHROPIC_WEBHOOK_SIGNING_KEY;
  if (!key) return new Response("not found", { status: 404 });
  const body = await request.text();
  let event: WebhookEvent;
  try {
    event = verify(body, request.headers, key);
  } catch {
    return new Response("bad signature", { status: 401 });
  }
  try {
    await wake(env, event);
  } catch (error) {
    console.error("webhook not acted on; the Workflow's poll will catch it", String(error));
  }
  try {
    await refreshFailed(env, event);
  } catch (error) {
    console.error("could not tell a lawyer their document-system connection lapsed", String(error));
  }
  return new Response(null, { status: 204 });
}

export function verify(body: string, headers: Headers, key: string): WebhookEvent {
  const client = new Anthropic({ apiKey: "unused-for-verification", webhookKey: key });
  return client.beta.webhooks.unwrap(body, { headers: Object.fromEntries(headers) }) as WebhookEvent;
}

async function wake(env: Env, event: WebhookEvent): Promise<void> {
  const id = event.id ?? "";
  const type = event.data?.type ?? "";
  const sessionId = event.data?.id ?? "";
  if (!id || !WAKES.has(type) || !sessionId) return;
  // Every delivery attempt of one event carries the same id.
  const [, inserted] = await env.DB.batch([
    env.DB.prepare(EVENTS),
    env.DB.prepare("INSERT INTO webhook_events (id, received_at) VALUES (?, ?) ON CONFLICT(id) DO NOTHING")
      .bind(id, new Date().toISOString()),
  ]);
  if (!inserted.meta.changes) return;
  const instanceId = await instanceFor(env, sessionId);
  if (!instanceId) return;
  const instance = await env.REVIEW_FLOW.get(instanceId);
  await instance.sendEvent({ type: "idle", payload: { webhook: type } });
}

/** What the lawyer is told when Anthropic could not refresh their token. */
export const RECONNECT =
  "I can no longer reach your document system as you: the connection needs renewing. " +
  "Until it is, I will review documents on their own.\n\n" +
  "Reply connect to reconnect your document system.\n";

/**
 * `vault_credential.refresh_failed`: Anthropic could not refresh a lawyer's
 * document-system token (src/lra/dms_mcp.py). The payload
 * is thin, a credential id and its vault id; the container recorded whose
 * they are in dms_vaults. The lawyer gets one email per event, and only if
 * they are still allowlisted and the service is not paused. Reviews go on
 * without the document system until they reconnect, and the MCP server says
 * so to each one.
 */
async function refreshFailed(env: Env, event: WebhookEvent): Promise<void> {
  const id = event.id ?? "";
  const data = event.data ?? {};
  if (!id || data.type !== "vault_credential.refresh_failed" || !data.id || !data.vault_id) return;
  let row: { user_address: string } | null;
  try {
    row = await env.DB.prepare("SELECT user_address FROM dms_vaults WHERE vault_id = ? AND credential_id = ?")
      .bind(data.vault_id, data.id).first<{ user_address: string }>();
  } catch {
    row = null; // No table: nobody has connected through a vault here.
  }
  const lawyer = (row?.user_address ?? "").toLowerCase();
  if (!lawyer) {
    console.log("refresh_failed for a credential with no record here", data.id);
    return;
  }
  if (!isAllowed(lawyer, env.ALLOWED_SENDERS) || paused(env)) return;
  const [, inserted] = await env.DB.batch([
    env.DB.prepare(EVENTS),
    env.DB.prepare("INSERT INTO webhook_events (id, received_at) VALUES (?, ?) ON CONFLICT(id) DO NOTHING")
      .bind(id, new Date().toISOString()),
  ]);
  if (!inserted.meta.changes) return;
  const notice = typeof env.PRIVILEGE_NOTICE === "string" ? env.PRIVILEGE_NOTICE : PRIVILEGE_NOTICE;
  await env.EMAIL.send({
    from: { email: env.MAIL_AGENT_ADDRESS, name: env.MAIL_AGENT_NAME || "Redline Desk" },
    to: [lawyer],
    subject: "Your document system needs reconnecting",
    text: RECONNECT + (notice ? `\n${notice}\n` : ""),
  });
}

/** Tie a session to the Workflow instance waiting on it. */
export async function mapSession(env: Env, sessionId: string, instanceId: string): Promise<void> {
  await env.DB.batch([
    env.DB.prepare(SESSIONS),
    env.DB.prepare(
      "INSERT INTO review_sessions (session_id, instance_id, created_at) VALUES (?, ?, ?) " +
      "ON CONFLICT(session_id) DO UPDATE SET instance_id = excluded.instance_id, done_at = NULL",
    ).bind(sessionId, instanceId, new Date().toISOString()),
  ]);
}

/** The instance is no longer waiting on its session. */
export async function sessionDone(env: Env, instanceId: string): Promise<void> {
  await env.DB.batch([
    env.DB.prepare(SESSIONS),
    env.DB.prepare("UPDATE review_sessions SET done_at = ? WHERE instance_id = ? AND done_at IS NULL")
      .bind(new Date().toISOString(), instanceId),
  ]);
}

async function instanceFor(env: Env, sessionId: string): Promise<string | null> {
  await env.DB.prepare(SESSIONS).run();
  const row = await env.DB.prepare(
    "SELECT instance_id FROM review_sessions WHERE session_id = ? AND done_at IS NULL",
  ).bind(sessionId).first<{ instance_id: string }>();
  return row?.instance_id ?? null;
}

/**
 * The daily sweep. An instance still marked as waiting on its session a while
 * after it started is woken, so it polls; one that has ended (or is past its
 * retention and gone) is marked done. Nothing here can send an email: a woken
 * instance only asks the container how its session is.
 */
export async function sweep(env: Env, olderThanMinutes = 10): Promise<{ woken: number; closed: number }> {
  await env.DB.prepare(SESSIONS).run();
  const cutoff = new Date(Date.now() - olderThanMinutes * 60_000).toISOString();
  const { results } = await env.DB.prepare(
    "SELECT session_id, instance_id FROM review_sessions WHERE done_at IS NULL AND created_at < ? LIMIT 500",
  ).bind(cutoff).all<{ session_id: string; instance_id: string }>();
  let woken = 0;
  let closed = 0;
  for (const row of results ?? []) {
    try {
      const instance = await env.REVIEW_FLOW.get(row.instance_id);
      const { status } = await instance.status();
      if (status === "complete" || status === "errored" || status === "terminated") {
        await sessionDone(env, row.instance_id);
        closed++;
      } else {
        // Waiting, or between two waits: an event is buffered until the next
        // wait takes it, and costs one extra poll at worst.
        await instance.sendEvent({ type: "idle", payload: { swept: true } });
        woken++;
      }
    } catch (error) {
      // Not found: past the Workflow's retention. Nothing is waiting on it.
      console.log("sweep: closing a session whose instance is gone", row.instance_id, String(error));
      await sessionDone(env, row.instance_id);
      closed++;
    }
  }
  return { woken, closed };
}

/** Event ids are only needed while Anthropic may still retry (minutes). */
export async function purgeWebhookEvents(env: Env, days: number): Promise<void> {
  await env.DB.prepare(EVENTS).run();
  const cutoff = new Date(Date.now() - days * 86_400_000).toISOString();
  await env.DB.batch([
    env.DB.prepare("DELETE FROM webhook_events WHERE received_at < ?").bind(cutoff),
    env.DB.prepare(SESSIONS),
    env.DB.prepare("DELETE FROM review_sessions WHERE done_at IS NOT NULL AND done_at < ?").bind(cutoff),
  ]);
}
