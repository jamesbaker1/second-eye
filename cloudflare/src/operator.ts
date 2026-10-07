/**
 * The break-glass door to /internal/* from outside: for an operator running
 * `lra purge`, `lra audit` or a release from a laptop, never for the
 * container, which reaches the same handlers through its outbound handler
 * and never touches the internet to do it (index.ts, ReviewContainer).
 *
 * Shut unless OPERATOR_SECRET is set on the Worker, and then every request
 * must carry an HMAC-SHA256 under it of
 *
 *   v1 \n METHOD \n /path?query \n unix-seconds \n nonce \n hex(sha256(body))
 *
 * in `x-lra-signature`, with `x-lra-time` and `x-lra-nonce`. A request more
 * than five minutes from the Worker's clock is refused, and so is a nonce
 * already used, so a captured request cannot be replayed or kept for later.
 * The signature covers the body, so a captured request cannot be edited
 * either. src/lra/edge.py signs the same way.
 *
 * The secret is not EDGE_SECRET and is never given to the container: a
 * container that leaked its environment would leak nothing that opens this.
 * Set it for the job and delete it after (docs/deploy-cloudflare.md).
 */

export const SKEW_SECONDS = 300;
const NONCE = /^[A-Za-z0-9_-]{16,64}$/;

/** At least 32 characters, or the door stays shut. */
export function operatorEnabled(secret: unknown): secret is string {
  return typeof secret === "string" && secret.trim().length >= 32;
}

export function hex(data: ArrayBuffer | Uint8Array): string {
  return [...new Uint8Array(data)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export async function sign(secret: string, method: string, pathAndQuery: string, time: string,
  nonce: string, body: ArrayBuffer): Promise<string> {
  const encoder = new TextEncoder();
  const bodyHash = hex(await crypto.subtle.digest("SHA-256", body));
  const key = await crypto.subtle.importKey("raw", encoder.encode(secret), { name: "HMAC", hash: "SHA-256" },
    false, ["sign"]);
  const message = ["v1", method.toUpperCase(), pathAndQuery, time, nonce, bodyHash].join("\n");
  return hex(await crypto.subtle.sign("HMAC", key, encoder.encode(message)));
}

/** Constant time, by comparing digests rather than the strings themselves. */
export async function sameSecret(a: string, b: string): Promise<boolean> {
  const encoder = new TextEncoder();
  const [x, y] = await Promise.all([
    crypto.subtle.digest("SHA-256", encoder.encode(a)),
    crypto.subtle.digest("SHA-256", encoder.encode(b)),
  ]);
  const left = new Uint8Array(x);
  const right = new Uint8Array(y);
  let difference = 0;
  for (let i = 0; i < left.length; i++) difference |= left[i] ^ right[i];
  return difference === 0;
}

/**
 * The request, rebuilt with the body already read, if it is signed and
 * fresh; otherwise why not. The nonce is spent here, before the handler runs.
 */
export async function verifyOperator(request: Request, secret: string, db: D1Database,
  now = Date.now()): Promise<{ request: Request } | { refused: string }> {
  const time = request.headers.get("x-lra-time") ?? "";
  const nonce = request.headers.get("x-lra-nonce") ?? "";
  const signature = (request.headers.get("x-lra-signature") ?? "").toLowerCase();
  if (!/^\d{1,12}$/.test(time) || !NONCE.test(nonce) || !/^[0-9a-f]{64}$/.test(signature)) {
    return { refused: "unsigned" };
  }
  if (Math.abs(now / 1000 - Number(time)) > SKEW_SECONDS) return { refused: "stale" };
  const body = request.method === "GET" || request.method === "HEAD" ? new ArrayBuffer(0) : await request.arrayBuffer();
  const url = new URL(request.url);
  const expected = await sign(secret, request.method, url.pathname + url.search, time, nonce, body);
  if (!(await sameSecret(signature, expected))) return { refused: "bad signature" };
  // Spent once. The table is created on first use, as edge_usage is, and
  // emptied by the daily cron.
  const [, inserted] = await db.batch([
    db.prepare("CREATE TABLE IF NOT EXISTS edge_operator_nonces (nonce TEXT PRIMARY KEY, seen_at INTEGER NOT NULL)"),
    db.prepare("INSERT OR IGNORE INTO edge_operator_nonces (nonce, seen_at) VALUES (?, ?)").bind(nonce, Math.floor(now / 1000)),
  ]);
  if (!inserted.meta?.changes) return { refused: "replayed" };
  return {
    request: new Request(request.url, {
      method: request.method, headers: request.headers,
      body: request.method === "GET" || request.method === "HEAD" ? undefined : body,
    }),
  };
}

/** Nonces older than the window cannot be replayed anyway. */
export async function purgeOperatorNonces(db: D1Database, now = Date.now()): Promise<void> {
  await db.batch([
    db.prepare("CREATE TABLE IF NOT EXISTS edge_operator_nonces (nonce TEXT PRIMARY KEY, seen_at INTEGER NOT NULL)"),
    db.prepare("DELETE FROM edge_operator_nonces WHERE seen_at < ?").bind(Math.floor(now / 1000) - 2 * SKEW_SECONDS),
  ]);
}
