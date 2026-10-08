/**
 * The Worker under test, plus two stand-ins the tests bind in its place:
 *
 *   FakeContainer  answers the container's step routes (src/secondeye/main.py) from
 *                  a plan the test writes to D1, and records every call.
 *   FakeEmail      the Email Service binding: records what is sent, and fails
 *                  on request (a size error, an outage).
 *
 * Both live in D1 so the test, the Workflow and the stand-ins, which run in
 * different contexts of one workerd, see the same state.
 */

import { DurableObject, WorkerEntrypoint } from "cloudflare:workers";
import type { Env } from "../src/env";
import { seal } from "../src/seal";

export { default, ReviewContainer, ReviewFlow } from "../src/index";

export const TEST_SCHEMA = [
  "CREATE TABLE IF NOT EXISTS test_plan (job TEXT PRIMARY KEY, plan TEXT NOT NULL)",
  "CREATE TABLE IF NOT EXISTS test_calls (seq INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT, path TEXT, body TEXT)",
  "CREATE TABLE IF NOT EXISTS test_sent (seq INTEGER PRIMARY KEY AUTOINCREMENT, message TEXT)",
  "CREATE TABLE IF NOT EXISTS test_email_fail (seq INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, when_attached INTEGER)",
];

export interface Reply {
  status?: number;
  body?: unknown;
  /** Store this reply sealed in R2 under the job's key for this part, and
   *  answer with the key, as flow.py does. */
  store?: Record<string, unknown>;
}

/** Responses per path, consumed in order; the last one repeats. */
export type Plan = Record<string, Reply[]>;

export class FakeContainer extends DurableObject<Env> {
  async fetch(request: Request): Promise<Response> {
    const env = this.env;
    for (const sql of TEST_SCHEMA) await env.DB.prepare(sql).run();
    const path = new URL(request.url).pathname;
    const job = request.headers.get("x-job-id") ?? "";
    const text = path === "/prepare" ? `${(await request.arrayBuffer()).byteLength} bytes` : await request.text();
    await env.DB.prepare("INSERT INTO test_calls (job, path, body) VALUES (?, ?, ?)").bind(job, path, text).run();

    const row = await env.DB.prepare("SELECT plan FROM test_plan WHERE job = ?").bind(job).first<{ plan: string }>();
    const plan = row ? (JSON.parse(row.plan) as Plan) : {};
    const replies = plan[path] ?? [];
    const reply = replies.length > 1 ? replies.shift()! : (replies[0] ?? defaults(path, job));
    if (row) {
      plan[path] = replies;
      await env.DB.prepare("UPDATE test_plan SET plan = ? WHERE job = ?").bind(JSON.stringify(plan), job).run();
    }
    let body = reply.body;
    if (reply.store) {
      const keys: Record<string, string> = {};
      for (const [part, message] of Object.entries(reply.store)) {
        keys[part] = await jobKey(job, part);
        const data = new TextEncoder().encode(JSON.stringify(message));
        await env.DOCS.put(keys[part], await seal(data.buffer as ArrayBuffer, env.DATA_KEY));
      }
      body = { ...(body as object ?? {}), ...keys };
    }
    return Response.json(body ?? {}, { status: reply.status ?? 200 });
  }
}

/** A happy path: a review, a session, done at the first poll. */
function defaults(path: string, job: string): Reply {
  switch (path) {
    case "/prepare":
      return { body: { status: "review", filename: "NDA.docx", deadlineSeconds: 600, noticeAfterSeconds: 90,
        notice: outbound("Reviewing NDA.docx; back in about 5 minutes.") } };
    case "/session/start":
      return { body: { sessionId: `sesn_${job}` } };
    case "/session/status":
      return { body: { state: "done" } };
    case "/finish":
      return { store: { outbound: outbound("The review.", true), fallback: outbound("The review, no files.") } };
    case "/fail":
      return { store: { outbound: outbound("I could not complete the full review.") } };
    default:
      return { body: { status: "ok" } };
  }
}

export function outbound(text: string, attached = false): Record<string, unknown> {
  return {
    from: { email: "review@legal.firm.com", name: "Second Eye" },
    to: ["jim@firm.com"],
    cc: [],
    subject: "Re: NDA",
    text,
    html: null,
    headers: { "In-Reply-To": "<m1@firm.com>", References: "<m1@firm.com>" },
    attachments: attached ? [{ filename: "NDA (redline).docx", type: "application/x", content: "YWJj" }] : [],
  };
}

/** jobstate.key(): job/ + the first 32 hex of sha256("<job>:<part>"). */
export async function jobKey(job: string, part: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(`${job}:${part}`));
  return "job/" + [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 32);
}

export class FakeEmail extends WorkerEntrypoint<Env> {
  async send(message: { attachments?: unknown[] }): Promise<{ messageId: string }> {
    const env = this.env;
    for (const sql of TEST_SCHEMA) await env.DB.prepare(sql).run();
    const failure = await env.DB.prepare("SELECT seq, code, when_attached FROM test_email_fail ORDER BY seq LIMIT 1")
      .first<{ seq: number; code: string; when_attached: number }>();
    if (failure && (!failure.when_attached || (message.attachments ?? []).length > 0)) {
      await env.DB.prepare("DELETE FROM test_email_fail WHERE seq = ?").bind(failure.seq).run();
      throw Object.assign(new Error(`${failure.code}: refused by the fake`), { code: failure.code });
    }
    const { meta } = await env.DB.prepare("INSERT INTO test_sent (message) VALUES (?)")
      .bind(JSON.stringify(message)).run();
    return { messageId: `msg-${meta.last_row_id}` };
  }
}
