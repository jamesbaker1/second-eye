/**
 * The same envelope src/lra/crypto.py writes: "enc:v1:" + 12-byte nonce +
 * AES-256-GCM ciphertext. A raw message waits in R2 between arriving and
 * being reviewed, and it is a client document like any other, so it waits
 * under the firm's key. With no key it is stored as it came; the application
 * refuses to start in that configuration, so that is a development state.
 *
 * The Workflow reads what the container sealed (the reply it built, in
 * jobstate.py) with unseal() here, so both directions have to agree byte for
 * byte; tests/test_cloudflare.py pins the prefix on both sides.
 */

export interface Keys {
  DATA_KEY: string;
  DATA_KEY_PREVIOUS?: string;
}

const PREFIX = new TextEncoder().encode("enc:v1:");

export async function seal(data: ArrayBuffer, dataKey: string): Promise<ArrayBuffer> {
  if (!dataKey) return data;
  const key = await crypto.subtle.importKey("raw", keyBytes(dataKey), "AES-GCM", false, ["encrypt"]);
  const nonce = crypto.getRandomValues(new Uint8Array(12));
  const ciphertext = new Uint8Array(await crypto.subtle.encrypt({ name: "AES-GCM", iv: nonce }, key, data));
  const sealed = new Uint8Array(PREFIX.length + nonce.length + ciphertext.length);
  sealed.set(PREFIX, 0);
  sealed.set(nonce, PREFIX.length);
  sealed.set(ciphertext, PREFIX.length + nonce.length);
  return sealed.buffer;
}

/** The inverse of seal(), under the current key or the one it replaced. */
export async function unseal(stored: ArrayBuffer, env: Keys): Promise<ArrayBuffer> {
  const bytes = new Uint8Array(stored);
  const sealed = bytes.length > PREFIX.length && PREFIX.every((b, i) => bytes[i] === b);
  const keys = [env.DATA_KEY, env.DATA_KEY_PREVIOUS].filter((k): k is string => !!k);
  if (!sealed) {
    if (keys.length) throw new Error("a key is configured and the stored message is not sealed");
    return stored;
  }
  const nonce = bytes.slice(PREFIX.length, PREFIX.length + 12);
  const ciphertext = bytes.slice(PREFIX.length + 12);
  for (const dataKey of keys) {
    const key = await crypto.subtle.importKey("raw", keyBytes(dataKey), "AES-GCM", false, ["decrypt"]);
    try {
      return await crypto.subtle.decrypt({ name: "AES-GCM", iv: nonce }, key, ciphertext);
    } catch {
      // sealed under the other key; try that one
    }
  }
  throw new Error("the stored message is sealed and no configured key opens it");
}

function keyBytes(dataKey: string): Uint8Array<ArrayBuffer> {
  return base64ToBytes(dataKey.replace(/-/g, "+").replace(/_/g, "/"));
}

export function base64ToBytes(value: string): Uint8Array<ArrayBuffer> {
  const padded = value + "=".repeat((4 - (value.length % 4)) % 4);
  return Uint8Array.from(atob(padded), (c) => c.charCodeAt(0));
}

export function bytesToBase64(bytes: Uint8Array): string {
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  }
  return btoa(binary);
}
