/**
 * Did the sender's domain vouch for this message? The allowlist admits an
 * address, and a From header is one line anyone can write, so the allowlist
 * means nothing unless the domain in it authenticated the message.
 *
 * Mail is admitted only on Cloudflare's own verdict that the From domain
 * passed DMARC. Anything else is dropped: a fail, a domain with no DMARC
 * record (`dmarc=none`), a DNS error, no verdict at all. Until 2026-10 only an
 * explicit `dmarc=fail` was dropped, so a domain without DMARC, or a verdict
 * that never arrived, let a forged partner address through.
 *
 * Which verdict. Cloudflare Email Routing prepends its own header, shaped
 *
 *   Authentication-Results: mx.cloudflare.net; dkim=pass header.d=gmail.com
 *     header.s=20230601 header.b=...; dmarc=pass header.from=gmail.com
 *     policy.dmarc=none; spf=pass (mx.cloudflare.net: domain of ...) smtp.mailfrom=...
 *
 * above everything the sender wrote, and does not remove a sender's own
 * Authentication-Results, even one that claims to be from mx.cloudflare.net
 * (both observed on real deliveries to a Worker; test/auth.test.ts keeps
 * those shapes). So only the topmost Authentication-Results counts, and it
 * must be Cloudflare's. A forged "pass" lower down is never read.
 * ARC-Authentication-Results is an intermediary's claim and is not read.
 *
 * What this trusts: that Cloudflare stamps every message it delivers. If it
 * ever delivered one unstamped, a sender's own "mx.cloudflare.net; dmarc=pass"
 * would be the topmost. email() logs how many mx.cloudflare.net verdicts each
 * admitted message carried (one is normal; more means a sender wrote one), so
 * that would show.
 *
 * The escape hatch. A firm domain with no DMARC record (a subsidiary, a domain
 * just acquired) can be listed in DKIM_ONLY_DOMAINS. Its mail is admitted on
 * `dmarc=none` with a DKIM signature that passed for exactly that domain:
 * what DMARC itself would check, without the record that tells receivers to.
 * A `dmarc=fail` is dropped whatever the list says. Publishing a DMARC record
 * is the fix; the list is for the weeks until it is published.
 */

/** One header line, unfolded, in the order it appears in the message. */
export interface Field {
  name: string;
  value: string;
}

/** The header block of a raw message, every field in document order. */
export function headerFields(raw: ArrayBuffer | Uint8Array): Field[] {
  const bytes = raw instanceof Uint8Array ? raw : new Uint8Array(raw);
  const text = new TextDecoder().decode(bytes.subarray(0, 256 * 1024));
  const end = text.search(/\r?\n\r?\n/);
  const block = (end < 0 ? text : text.slice(0, end)).replace(/\r?\n[ \t]+/g, " ");
  const fields: Field[] = [];
  for (const line of block.split(/\r?\n/)) {
    const colon = line.indexOf(":");
    if (colon <= 0) continue;
    fields.push({ name: line.slice(0, colon).trim().toLowerCase(), value: line.slice(colon + 1).trim() });
  }
  return fields;
}

/** Reads a message stream only as far as the end of its headers. */
export async function readHeaderBlock(stream: ReadableStream<Uint8Array>, limit = 256 * 1024): Promise<Uint8Array> {
  const reader = stream.getReader();
  const chunks: Uint8Array[] = [];
  let length = 0;
  let seen = "";
  try {
    while (length < limit) {
      const { done, value } = await reader.read();
      if (done || !value) break;
      chunks.push(value);
      length += value.byteLength;
      // The last few characters of the previous chunk too, for a blank line
      // split across two chunks.
      seen = seen.slice(-3) + new TextDecoder().decode(value);
      if (/\r?\n\r?\n/.test(seen)) break;
    }
  } finally {
    await reader.cancel().catch(() => undefined);
  }
  const out = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) {
    out.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return out;
}

export const CLOUDFLARE_AUTHSERV = "mx.cloudflare.net";

interface Result {
  method: string;
  result: string;
  props: Record<string, string>;
}

/** An Authentication-Results value: its authserv-id and each method's result. */
export function parseResults(value: string): { authserv: string; results: Result[] } {
  // Comments are free text ("(mx.cloudflare.net: domain of ... designates
  // ...)") and may hold anything, including "dmarc=pass". Drop them first.
  let text = value;
  for (let i = 0; i < 5 && /\([^()]*\)/.test(text); i++) text = text.replace(/\([^()]*\)/g, " ");
  const [head, ...rest] = text.split(";");
  const authserv = (head ?? "").trim().split(/\s+/)[0]?.toLowerCase() ?? "";
  const results: Result[] = [];
  for (const part of rest) {
    const tokens = part.trim().split(/\s+/).filter(Boolean);
    const first = tokens.shift();
    if (!first || !first.includes("=")) continue;
    const [method, result] = first.split("=", 2).map((s) => s.toLowerCase());
    const props: Record<string, string> = {};
    for (const token of tokens) {
      const eq = token.indexOf("=");
      if (eq > 0) props[token.slice(0, eq).toLowerCase()] = token.slice(eq + 1).replace(/^"|"$/g, "").toLowerCase();
    }
    results.push({ method, result, props });
  }
  return { authserv, results };
}

export type Verdict =
  | { ok: true; how: "dmarc" | "aligned-dkim"; cloudflareVerdicts: number }
  | { ok: false; why: string };

/** How many Authentication-Results claim to be Cloudflare's. One is normal. */
export function cloudflareVerdicts(fields: Field[]): number {
  return fields.filter((f) => f.name === "authentication-results"
    && parseResults(f.value).authserv === CLOUDFLARE_AUTHSERV).length;
}

function domainOf(address: string): string {
  return (address.split("@").pop() ?? "").trim().toLowerCase();
}

function listed(list: string | undefined): string[] {
  return (list ?? "").toLowerCase().split(/[\s,]+/).filter(Boolean);
}

/**
 * Whether `sender` (the address the allowlist admitted, from the From header)
 * is vouched for by its domain, on Cloudflare's verdict. Never throws: a
 * message it cannot read is a message it does not admit.
 */
export function senderAuthenticated(fields: Field[], sender: string, dkimOnlyDomains?: string): Verdict {
  const froms = fields.filter((f) => f.name === "from");
  // Two From headers is how a forger shows the filter one domain and the
  // reader another; DMARC says to reject it (RFC 7489 6.6.1).
  if (froms.length !== 1) return { ok: false, why: `${froms.length} From headers` };
  const domain = domainOf(sender);
  if (!domain) return { ok: false, why: "no sender domain" };

  const top = fields.find((f) => f.name === "authentication-results");
  if (!top) return { ok: false, why: "no Authentication-Results" };
  const { authserv, results } = parseResults(top.value);
  if (authserv !== CLOUDFLARE_AUTHSERV) return { ok: false, why: `topmost verdict is not Cloudflare's (${authserv.slice(0, 60)})` };

  const dmarc = results.filter((r) => r.method === "dmarc");
  // One verdict per message; two in Cloudflare's own header is not a shape
  // it writes, so it is not one to guess about.
  if (dmarc.length > 1) return { ok: false, why: "more than one dmarc result" };
  const verdict = dmarc[0]?.result ?? "missing";
  const headerFrom = dmarc[0]?.props["header.from"] ?? "";
  if (verdict === "pass") {
    if (headerFrom !== domain) return { ok: false, why: `dmarc=pass for ${headerFrom.slice(0, 60)}, not the sender's domain` };
    return { ok: true, how: "dmarc", cloudflareVerdicts: cloudflareVerdicts(fields) };
  }
  if (verdict === "none" && listed(dkimOnlyDomains).includes(domain)) {
    const aligned = results.some((r) => r.method === "dkim" && r.result === "pass" && r.props["header.d"] === domain);
    if (aligned) return { ok: true, how: "aligned-dkim", cloudflareVerdicts: cloudflareVerdicts(fields) };
    return { ok: false, why: "dmarc=none and no DKIM pass for the sender's domain" };
  }
  return { ok: false, why: `dmarc=${verdict.slice(0, 20)}` };
}
