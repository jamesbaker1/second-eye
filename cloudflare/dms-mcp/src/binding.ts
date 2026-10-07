/**
 * Which lawyer, which matter, and whose iManage token: read from the request
 * and checked before any tool runs.
 *
 * Two forms, because whether Anthropic's vault keeps a query string on the
 * way through is a thing only a live run settles (docs/migration.md, Phase 4).
 * The host picks one with DMS_MCP_BINDING; this server accepts either, each
 * checked on its own, so switching needs no deploy here.
 *
 *   query       (the default) The session's MCP URL is
 *               /mcp?u=<lawyer>&m=<matter>&exp=<unix>&sig=<hex HMAC>
 *               and the lawyer's own vault injects their iManage access token
 *               as `Authorization: Bearer`, refreshed by Anthropic.
 *
 *   capability  The URL is the bare /mcp, and a vault made for this one
 *               session holds a static bearer: cap1.<payload>.<mac>, where the
 *               payload carries the lawyer, the matter, the expiry and the
 *               iManage access token the host held when it started the session.
 *
 * The MAC is HMAC-SHA256 under DMS_MCP_SIGNING_KEY, a secret shared with the
 * container (src/lra/dms_mcp.py builds both forms). It is checked with
 * crypto.subtle.verify, which compares in constant time.
 */

export interface Binding {
  lawyer: string;
  matter: string;
  /** Unix seconds. */
  expires: number;
  /** The iManage access token, forwarded as X-Auth-Token. */
  token: string;
}

/** Why a request carries no usable binding, in a sentence the model can repeat. */
export type Refusal = { refused: string; status?: number };

export const CAPABILITY_PREFIX = "cap1.";

const encoder = new TextEncoder();

async function key(secret: string): Promise<CryptoKey> {
  return crypto.subtle.importKey("raw", encoder.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, [
    "sign",
    "verify",
  ]);
}

/** The string the query form signs. The container builds exactly this. */
export function queryMessage(lawyer: string, matter: string, expires: number): string {
  return `${lawyer}\n${matter}\n${expires}`;
}

function hexToBytes(hex: string): Uint8Array | null {
  if (!/^[0-9a-f]*$/i.test(hex) || hex.length % 2) return null;
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  return out;
}

function b64urlToBytes(value: string): Uint8Array | null {
  try {
    const padded = value.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (value.length % 4)) % 4);
    return Uint8Array.from(atob(padded), (c) => c.charCodeAt(0));
  } catch {
    return null;
  }
}

export function bytesToHex(bytes: ArrayBuffer): string {
  return [...new Uint8Array(bytes)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export function bytesToB64url(bytes: ArrayBuffer | Uint8Array): string {
  const view = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  return btoa(String.fromCharCode(...view)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** For tests and for anyone checking the container's arithmetic. */
export async function signQuery(secret: string, lawyer: string, matter: string, expires: number): Promise<string> {
  return bytesToHex(await crypto.subtle.sign("HMAC", await key(secret), encoder.encode(queryMessage(lawyer, matter, expires))));
}

export async function signCapability(
  secret: string,
  payload: { u: string; m: string; exp: number; t: string },
): Promise<string> {
  const body = bytesToB64url(encoder.encode(JSON.stringify(payload)));
  const mac = await crypto.subtle.sign("HMAC", await key(secret), encoder.encode(CAPABILITY_PREFIX + body));
  return `${CAPABILITY_PREFIX}${body}.${bytesToB64url(mac)}`;
}

function bearerOf(request: Request): string {
  const header = request.headers.get("authorization") ?? "";
  const match = header.match(/^Bearer\s+(.+)$/i);
  return match ? match[1].trim() : "";
}

const NO_MATTER =
  "No matter is attached to this session, so the document system cannot be used. Only documents " +
  "on the matter under review can be read. Do not guess which matter it belongs to.";

/**
 * The binding a request carries, or why it has none.
 *
 * A request with no binding at all is not an HTTP error: the session was
 * started with no matter, or before the lawyer connected, and its tool calls
 * are answered with a sentence saying so. A binding that fails its signature
 * is refused outright (403): somebody changed the URL or the token.
 */
export async function bindingOf(request: Request, secret: string | undefined, now = Date.now()): Promise<Binding | Refusal> {
  if (!secret) return { refused: "The document system is not configured on this server.", status: 503 };
  const url = new URL(request.url);
  const bearer = bearerOf(request);
  const seconds = Math.floor(now / 1000);

  if (bearer.startsWith(CAPABILITY_PREFIX)) {
    const [body, mac] = bearer.slice(CAPABILITY_PREFIX.length).split(".");
    const macBytes = b64urlToBytes(mac ?? "");
    const bodyBytes = b64urlToBytes(body ?? "");
    if (!body || !macBytes || !bodyBytes) return { refused: "The session's document-system credential is malformed.", status: 403 };
    const ok = await crypto.subtle.verify("HMAC", await key(secret), macBytes, encoder.encode(CAPABILITY_PREFIX + body));
    if (!ok) return { refused: "The session's document-system credential did not verify.", status: 403 };
    let payload: { u?: unknown; m?: unknown; exp?: unknown; t?: unknown };
    try {
      payload = JSON.parse(new TextDecoder().decode(bodyBytes));
    } catch {
      return { refused: "The session's document-system credential is malformed.", status: 403 };
    }
    const lawyer = String(payload.u ?? "");
    const matter = String(payload.m ?? "");
    const expires = Number(payload.exp);
    const token = String(payload.t ?? "");
    if (!matter) return { refused: NO_MATTER };
    if (!Number.isFinite(expires) || expires < seconds) return { refused: expired() };
    if (!token) return { refused: notConnected() };
    return { lawyer, matter, expires, token };
  }

  const matter = url.searchParams.get("m") ?? "";
  const sig = url.searchParams.get("sig") ?? "";
  if (!matter && !sig) return { refused: NO_MATTER };
  const lawyer = (url.searchParams.get("u") ?? "").toLowerCase();
  const expires = Number(url.searchParams.get("exp"));
  const sigBytes = hexToBytes(sig);
  if (!matter || !lawyer || !Number.isFinite(expires) || !sigBytes) {
    return { refused: "The session's matter binding is incomplete.", status: 403 };
  }
  const ok = await crypto.subtle.verify("HMAC", await key(secret), sigBytes, encoder.encode(queryMessage(lawyer, matter, expires)));
  if (!ok) return { refused: "The session's matter binding did not verify.", status: 403 };
  if (expires < seconds) return { refused: expired() };
  if (!bearer) return { refused: notConnected() };
  return { lawyer, matter, expires, token: bearer };
}

function expired(): string {
  return "This session's access to the document system has expired. Carry on without it and say so.";
}

function notConnected(): string {
  return (
    "No document-system credential came with this request, so nothing was searched. The lawyer may " +
    "not have connected their document system; carry on without it and say so."
  );
}

export function isRefusal(value: Binding | Refusal): value is Refusal {
  return "refused" in value;
}
