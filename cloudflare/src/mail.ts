/**
 * Reading a raw message's headers, and the one reply the Worker writes itself:
 * "I couldn't review ...", for a message that failed before the application
 * could say anything: the Workflow's failure path, when nothing was prepared.
 */

import { runtimePaused, varPaused } from "./pause";
import { base64ToBytes } from "./seal";

export interface Sender {
  MAIL_AGENT_ADDRESS?: string;
  MAIL_AGENT_NAME?: string;
  PRIVILEGE_NOTICE?: string;
}

/** The application's default (config.py). Empty in the environment sends none. */
export const PRIVILEGE_NOTICE = "Privileged & Confidential — Attorney Work Product";

export function failureNotice(headers: Map<string, string>, sender: string, env: Sender): unknown {
  const subject = oneLine(decodeWords(headers.get("subject") ?? "")).slice(0, 200);
  const what = subject ? `"${subject}"` : "the document you sent";
  const messageId = oneLine(headers.get("message-id") ?? "");
  // The legend every message from the application carries (policy.privileged):
  // the last line, and the X-Privileged header.
  const notice = oneLine(env.PRIVILEGE_NOTICE ?? PRIVILEGE_NOTICE);
  const extra: Record<string, string> = {
    ...(messageId ? { "In-Reply-To": messageId, References: messageId } : {}),
    ...(notice ? { "X-Privileged": notice.replace(/[\u2013\u2014]/g, "-").replace(/[^\x20-\x7e]/g, "") } : {}),
  };
  return {
    from: { email: env.MAIL_AGENT_ADDRESS, name: env.MAIL_AGENT_NAME || "Redline Desk" },
    to: [sender],
    subject: subject ? `Re: ${subject.replace(/^re:\s*/i, "")}` : "I could not review your document",
    text: `I couldn't review ${what}. Nothing was sent to anyone else. Please resend it.\n`
      + (notice ? `\n${notice}\n` : ""),
    ...(Object.keys(extra).length ? { headers: extra } : {}),
  };
}

/** The header block of a raw message, unfolded, by lower-case name. First wins. */
export function parseHeaders(raw: ArrayBuffer): Map<string, string> {
  const text = new TextDecoder().decode(raw.slice(0, 256 * 1024));
  const end = text.search(/\r?\n\r?\n/);
  const block = (end < 0 ? text : text.slice(0, end)).replace(/\r?\n[ \t]+/g, " ");
  const out = new Map<string, string>();
  for (const line of block.split(/\r?\n/)) {
    const colon = line.indexOf(":");
    if (colon <= 0) continue;
    const name = line.slice(0, colon).trim().toLowerCase();
    if (!out.has(name)) out.set(name, line.slice(colon + 1).trim());
  }
  return out;
}

/** RFC 2047 encoded words ("=?UTF-8?B?...?="), which is how most subjects arrive. */
function decodeWords(value: string): string {
  return value.replace(/=\?([^?]+)\?([bBqQ])\?([^?]*)\?=\s*/g, (whole, charset: string, enc: string, body: string) => {
    try {
      const bytes = enc.toUpperCase() === "B"
        ? base64ToBytes(body)
        : Uint8Array.from(
          body.replace(/_/g, " ").replace(/=([0-9a-fA-F]{2})/g, (_m, h: string) => String.fromCharCode(parseInt(h, 16))),
          (c) => c.charCodeAt(0),
        );
      return new TextDecoder(charset).decode(bytes);
    } catch {
      return whole;
    }
  });
}

/** The kill switch: the SERVICE_PAUSED var, or the runtime switch in D1 as
 *  last loaded for this env (pause.ts). Mail is accepted and held, nothing is
 *  sent. */
export function paused(env: { SERVICE_PAUSED?: unknown }): boolean {
  return varPaused(env) || runtimePaused(env);
}

/** Where a message waits while the service is paused, until released. */
export const HELD = "held/";

export function oneLine(value: string): string {
  return value.replace(/[\r\n\t]+/g, " ").trim();
}

export function addressOf(header: string): string {
  const bracketed = header.match(/<([^<>\s]+@[^<>\s]+)>/);
  return (bracketed ? bracketed[1] : header).trim().toLowerCase();
}

/** Same rule as intake.check_sender: an entry is an address or a whole domain.
 *  Unlike the application, an empty list here admits nobody. The application's
 *  default suits a laptop; an address on the public internet needs a list. */
export function isAllowed(sender: string, allowlist: string | undefined): boolean {
  const entries = (allowlist ?? "").toLowerCase().split(/[\s,]+/).filter(Boolean);
  const domain = sender.split("@").pop() ?? "";
  return entries.includes(sender) || entries.includes(domain);
}
