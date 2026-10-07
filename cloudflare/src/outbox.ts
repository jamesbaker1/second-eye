/**
 * One send per job and kind.
 *
 * Email Service has no idempotency key, and a Workflow step is retried when
 * it fails, including when it fails after the send went through. So every
 * send on the Workflow path is named "<instance>:<kind>" (reply, notice,
 * failure, prepare-1 ...) and goes through a row in D1:
 *
 *   outbox(key, state, message_id)   state: sending | sent
 *
 * A key already "sent" is not sent again; the caller gets the first message
 * id back. The one window left is the provider accepting the message and the
 * "sent" write then failing: the row stays "sending", the retry sends again,
 * and the lawyer gets a second copy. That is the side to err on; the other
 * side is a lost reply.
 */

import type { Env } from "./env";
import { addressOf, isAllowed, paused } from "./mail";
import { loadPause } from "./pause";

export interface Outbound {
  from: { email: string; name?: string };
  to: string[];
  cc?: string[];
  subject: string;
  text: string;
  html?: string | null;
  headers?: Record<string, string>;
  attachments?: { filename: string; type: string; content: string }[];
  /** Set by the container on the Workflow path: the outbox key. */
  outbox?: string;
}

export interface Sent {
  messageId: string;
  duplicate: boolean;
}

const SCHEMA =
  "CREATE TABLE IF NOT EXISTS outbox (key TEXT PRIMARY KEY, state TEXT NOT NULL, " +
  "message_id TEXT, updated_at TEXT NOT NULL)";

/** The message in the shape the Email Service binding takes. */
export function toMessage(out: Outbound): unknown {
  return {
    from: out.from,
    to: out.to,
    ...(out.cc?.length ? { cc: out.cc } : {}),
    subject: out.subject,
    text: out.text,
    ...(out.html ? { html: out.html } : {}),
    ...(out.headers && Object.keys(out.headers).length ? { headers: out.headers } : {}),
    attachments: (out.attachments ?? []).map((a) => ({ ...a, disposition: "attachment" })),
  };
}

/** A message firm policy will not let leave. */
export class PolicyViolation extends Error {}

/**
 * REPLY_POLICY (DECISIONS 31), held here as well as in the container
 * (policy.py), because this is where mail actually leaves. The Worker cannot
 * know which message a reply answers, so it holds the shape the container's
 * rule produces: under sender_only, one recipient, a sender the edge admits
 * (ALLOWED_SENDERS), and no CC. Anything else is refused, not repaired.
 */
export function policed(env: Env, out: Outbound): Outbound {
  if (paused(env)) throw new PolicyViolation("SERVICE_PAUSED: nothing is sent");
  const policy = String(env.REPLY_POLICY ?? "sender_only").trim().toLowerCase();
  if (policy === "model") return out;
  const to = out.to ?? [];
  if (to.length !== 1 || !isAllowed(addressOf(to[0]), env.ALLOWED_SENDERS)) {
    throw new PolicyViolation("REPLY_POLICY=sender_only: a reply goes to one admitted sender");
  }
  if (out.cc?.length) console.log("REPLY_POLICY=sender_only: dropping the CC on a reply");
  const { cc: _, ...rest } = out;
  return rest;
}

export async function sendOnce(env: Env, key: string, out: Outbound): Promise<Sent> {
  // A Workflow's send is an entry point of its own: the switch as it is now.
  await loadPause(env);
  out = policed(env, out);
  await env.DB.prepare(SCHEMA).run();
  const row = await env.DB.prepare("SELECT state, message_id FROM outbox WHERE key = ?")
    .bind(key).first<{ state: string; message_id: string | null }>();
  if (row?.state === "sent") return { messageId: row.message_id ?? "", duplicate: true };
  const now = new Date().toISOString();
  await env.DB.prepare(
    "INSERT INTO outbox (key, state, updated_at) VALUES (?, 'sending', ?) " +
    "ON CONFLICT(key) DO UPDATE SET state = 'sending', updated_at = excluded.updated_at",
  ).bind(key, now).run();
  const { outbox: _, ...message } = out;
  const result = await env.EMAIL.send(toMessage(message));
  await env.DB.prepare("UPDATE outbox SET state = 'sent', message_id = ?, updated_at = ? WHERE key = ?")
    .bind(result.messageId, new Date().toISOString(), key).run();
  return { messageId: result.messageId, duplicate: false };
}

/** Whether a send failed because the message was too big (Email Service's
 *  E_CONTENT_TOO_LARGE), which the no-attachment variant answers. */
export function tooLarge(error: unknown): boolean {
  const e = error as { code?: unknown; message?: unknown };
  return e?.code === "E_CONTENT_TOO_LARGE" || /E_CONTENT_TOO_LARGE/.test(String(e?.message ?? error));
}

/** Rows older than the retention window. A key is only needed while its
 *  Workflow instance can still retry, which is days, not months. */
export async function purgeOutbox(env: Env, days: number): Promise<void> {
  await env.DB.prepare(SCHEMA).run();
  const cutoff = new Date(Date.now() - days * 86_400_000).toISOString();
  await env.DB.prepare("DELETE FROM outbox WHERE updated_at < ?").bind(cutoff).run();
}
