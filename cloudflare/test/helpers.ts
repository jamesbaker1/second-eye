import { env } from "cloudflare:workers";
import type { Env } from "../src/env";
import { seal } from "../src/seal";
import { type Plan, TEST_SCHEMA } from "./worker";

export const testEnv = env as unknown as Env;

let counter = 0;

/** A fresh job id per test, so nothing one test leaves is read by another. */
export function newJob(): string {
  counter += 1;
  return `rf-test-${Date.now().toString(36)}-${counter}`;
}

export async function schema(): Promise<void> {
  for (const sql of TEST_SCHEMA) await testEnv.DB.prepare(sql).run();
}

export async function plan(job: string, plan: Plan): Promise<void> {
  await schema();
  await testEnv.DB.prepare("INSERT OR REPLACE INTO test_plan (job, plan) VALUES (?, ?)")
    .bind(job, JSON.stringify(plan)).run();
}

export async function calls(job: string): Promise<{ path: string; body: string }[]> {
  await schema();
  const { results } = await testEnv.DB.prepare("SELECT path, body FROM test_calls WHERE job = ? ORDER BY seq")
    .bind(job).all<{ path: string; body: string }>();
  return results;
}

export async function sent(): Promise<{ subject: string; text: string; attachments: unknown[] }[]> {
  await schema();
  const { results } = await testEnv.DB.prepare("SELECT message FROM test_sent ORDER BY seq")
    .all<{ message: string }>();
  return results.map((r) => JSON.parse(r.message));
}

export async function sentSince(before: number) {
  return (await sent()).slice(before);
}

export async function failNextSend(code: string, onlyWithAttachments = false): Promise<void> {
  await schema();
  await testEnv.DB.prepare("INSERT INTO test_email_fail (code, when_attached) VALUES (?, ?)")
    .bind(code, onlyWithAttachments ? 1 : 0).run();
}

/**
 * The header Cloudflare Email Routing prepends to every message it hands a
 * Worker, in the shape it writes (captured from real deliveries: a Gmail
 * sender, whose DMARC policy is p=none, and passes on its aligned DKIM).
 */
export function cloudflareVerdict(dmarc: string, fromDomain: string, dkimDomain = fromDomain): string {
  const local = "jim";
  return `Authentication-Results: mx.cloudflare.net; dkim=pass header.d=${dkimDomain} header.s=20230601 header.b=AbCdEf12;\r\n`
    + `\tdmarc=${dmarc} header.from=${fromDomain} policy.dmarc=none;\r\n`
    + `\tspf=pass (mx.cloudflare.net: domain of ${local}@${fromDomain} designates 209.85.128.41 as permitted sender) smtp.mailfrom=${local}@${fromDomain}`;
}

export function rawMessage(opts: {
  from?: string; messageId?: string; subject?: string;
  /** Cloudflare's own verdict lines on top; [] for a message with none. */
  trace?: string[];
} = {}): string {
  const from = opts.from ?? "jim@firm.com";
  const trace = opts.trace ?? [
    cloudflareVerdict("pass", from.split("@")[1]),
    `Received-SPF: pass (mx.cloudflare.net: domain of ${from} designates 209.85.128.41 as permitted sender) receiver=mx.cloudflare.net; client-ip=209.85.128.41;`,
    "Received: from mail-wm1-f41.google.com (209.85.128.41) by cloudflare-email.net (cloudflare) id 9M7MLUvJNdlk for <review@legal.firm.com>; Sun, 04 Oct 2026 09:56:41 +0000",
  ];
  return [
    ...trace,
    `From: Jim Baker <${from}>`,
    "To: review@legal.firm.com",
    `Subject: ${opts.subject ?? "NDA"}`,
    `Message-ID: ${opts.messageId ?? "<m1@firm.com>"}`,
    "",
    "Quick look before this goes to the client?",
    "",
  ].join("\r\n");
}

/** A message as Email Routing hands it to email(). */
export function inbound(raw: string): ForwardableEmailMessage & { rejected: string[] } {
  const bytes = new TextEncoder().encode(raw);
  const headers = new Headers();
  for (const line of raw.split("\r\n\r\n")[0].replace(/\r\n[ \t]+/g, " ").split("\r\n")) {
    const colon = line.indexOf(":");
    headers.append(line.slice(0, colon), line.slice(colon + 1).trim());
  }
  const rejected: string[] = [];
  return {
    from: headers.get("from")!, to: "review@legal.firm.com", headers, rawSize: bytes.length,
    raw: new Response(bytes).body!,
    rejected,
    setReject(reason: string) { rejected.push(reason); },
    forward: async () => ({ messageId: "" }),
    reply: async () => ({ messageId: "" }),
  } as unknown as ForwardableEmailMessage & { rejected: string[] };
}

/** The sealed message in R2, as email() leaves it. */
export async function storeMessage(raw = rawMessage()): Promise<string> {
  const key = `inbound/${crypto.randomUUID()}.eml`;
  await testEnv.DOCS.put(key, await seal(new TextEncoder().encode(raw).buffer as ArrayBuffer, testEnv.DATA_KEY));
  return key;
}
