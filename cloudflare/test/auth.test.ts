/**
 * Who gets past the edge: only mail its sender's domain vouched for, on
 * Cloudflare's own verdict (src/auth.ts). Everything else is dropped silently,
 * before the daily cap counts it.
 */

import { introspectWorkflowInstance } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import { headerFields, readHeaderBlock, senderAuthenticated } from "../src/auth";
import worker from "../src/index";
import { instanceId } from "../src/review-flow";
import { cloudflareVerdict, inbound, rawMessage, testEnv } from "./helpers";

// The header Cloudflare Email Routing wrote on a real delivery to a Worker
// (an email service sending through Amazon SES, a probe Worker, 2026-09;
// the sending domain, selectors, signatures and address are replaced): two DKIM signatures,
// an SPF comment that itself names mx.cloudflare.net, and the DMARC verdict.
const REAL_RESEND =
  "mx.cloudflare.net; dkim=pass header.d=sender.example header.s=esp header.b=AbCdEfGh; " +
  "dkim=pass header.d=amazonses.com header.s=exampleselector0000000000000000 header.b=IjKlMnOp; " +
  "dmarc=pass header.from=sender.example policy.dmarc=none; " +
  "spf=none (mx.cloudflare.net: no SPF records found for postmaster@a1-2.smtp-out.amazonses.com) smtp.helo=a1-2.smtp-out.amazonses.com; " +
  "spf=pass (mx.cloudflare.net: domain of 0100@send.sender.example designates 192.0.2.63 as permitted sender) smtp.mailfrom=0100@send.sender.example";

/** A Gmail sender, as production receives one: Gmail publishes DMARC p=none. */
const SENDER = "fictional.lawyer@gmail.com"; // not a real mailbox

function fields(trace: string[], from = SENDER, extra: string[] = []) {
  return headerFields(new TextEncoder().encode(rawMessage({ from, trace: [...trace, ...extra] })));
}

describe("the verdict", () => {
  it("admits Jim's Gmail, which passes DMARC on its aligned DKIM", () => {
    const verdict = senderAuthenticated(fields([cloudflareVerdict("pass", "gmail.com")]), SENDER);
    expect(verdict).toEqual({ ok: true, how: "dmarc", cloudflareVerdicts: 1 });
  });

  it("reads the real Cloudflare header, comments and all", () => {
    const verdict = senderAuthenticated(
      fields([`Authentication-Results: ${REAL_RESEND}`], "noreply@sender.example"), "noreply@sender.example");
    expect(verdict.ok).toBe(true);
  });

  it.each([
    ["no DMARC record", "none", "dmarc=none"],
    ["a DMARC failure", "fail", "dmarc=fail"],
    ["a DNS error", "temperror", "dmarc=temperror"],
    ["a broken record", "permerror", "dmarc=permerror"],
  ])("drops %s", (_what, dmarc, why) => {
    expect(senderAuthenticated(fields([cloudflareVerdict(dmarc, "gmail.com")]), SENDER)).toEqual({ ok: false, why });
  });

  it("drops a message Cloudflare gave no verdict", () => {
    expect(senderAuthenticated(fields([]), SENDER)).toMatchObject({ ok: false, why: "no Authentication-Results" });
  });

  it("trusts only the topmost verdict, so a forged pass below Cloudflare's fail is not read", () => {
    const forged = "Authentication-Results: mx.cloudflare.net; dkim=pass header.d=gmail.com; dmarc=pass header.from=gmail.com";
    const verdict = senderAuthenticated(fields([cloudflareVerdict("fail", "gmail.com")], SENDER, [forged]), SENDER);
    expect(verdict).toEqual({ ok: false, why: "dmarc=fail" });
  });

  it("does not take a verdict from anyone but Cloudflare, even on top", () => {
    for (const top of [
      "Authentication-Results: mx.google.com; dmarc=pass header.from=gmail.com",
      "Authentication-Results: mx.cloudflare.net.evil.example; dmarc=pass header.from=gmail.com",
    ]) {
      const verdict = senderAuthenticated(fields([top, cloudflareVerdict("pass", "gmail.com")]), SENDER);
      expect(verdict.ok).toBe(false);
    }
  });

  it("ignores ARC, which is an intermediary's say-so", () => {
    const arc = "ARC-Authentication-Results: i=1; mx.cloudflare.net; dkim=pass header.d=gmail.com; dmarc=pass header.from=gmail.com";
    expect(senderAuthenticated(fields([arc]), SENDER).ok).toBe(false);
  });

  it("drops a pass for some other domain than the sender's", () => {
    const verdict = senderAuthenticated(fields([cloudflareVerdict("pass", "attacker.example")]), SENDER);
    expect(verdict).toMatchObject({ ok: false });
  });

  it("drops a message with two From headers", () => {
    const verdict = senderAuthenticated(
      fields([cloudflareVerdict("pass", "gmail.com")], SENDER, ["From: someone@gmail.com"]), SENDER);
    expect(verdict).toEqual({ ok: false, why: "2 From headers" });
  });

  it("does not read a result out of a comment", () => {
    const sly = "Authentication-Results: mx.cloudflare.net; dmarc=none (dmarc=pass header.from=gmail.com) header.from=gmail.com";
    expect(senderAuthenticated(fields([sly]), SENDER)).toEqual({ ok: false, why: "dmarc=none" });
  });
});

describe("a firm domain with no DMARC record (DKIM_ONLY_DOMAINS)", () => {
  const ANN = "ann@sub.firm.com";
  const noRecord = (dkimDomain: string, dmarc = "none") =>
    fields([cloudflareVerdict(dmarc, "sub.firm.com", dkimDomain)], ANN);

  it("is admitted on a DKIM pass for exactly that domain, when listed", () => {
    expect(senderAuthenticated(noRecord("sub.firm.com"), ANN, "other.com, sub.firm.com"))
      .toEqual({ ok: true, how: "aligned-dkim", cloudflareVerdicts: 1 });
  });

  it("is dropped when not listed", () => {
    expect(senderAuthenticated(noRecord("sub.firm.com"), ANN, "").ok).toBe(false);
  });

  it("is dropped when the signature is some other domain's", () => {
    expect(senderAuthenticated(noRecord("mailer.example"), ANN, "sub.firm.com").ok).toBe(false);
    expect(senderAuthenticated(noRecord("firm.com"), ANN, "sub.firm.com").ok).toBe(false);
  });

  it("does not excuse a DMARC failure", () => {
    expect(senderAuthenticated(noRecord("sub.firm.com", "fail"), ANN, "sub.firm.com").ok).toBe(false);
  });
});

describe("email()", () => {
  const capped = () => ({ ...testEnv, MAX_EMAILS_PER_DAY: "5" }) as typeof testEnv;
  const today = async () => {
    await testEnv.DB.prepare("CREATE TABLE IF NOT EXISTS edge_usage (day TEXT PRIMARY KEY, emails INTEGER NOT NULL)").run();
    const row = await testEnv.DB.prepare("SELECT emails FROM edge_usage WHERE day = ?")
      .bind(new Date().toISOString().slice(0, 10)).first<{ emails: number }>();
    return row?.emails ?? 0;
  };
  const stored = async () => (await testEnv.DOCS.list({ prefix: "inbound/" })).objects.length;

  it("drops an unauthenticated allowlisted sender silently, before the cap counts it", async () => {
    const before = { count: await today(), stored: await stored() };
    for (const dmarc of ["none", "fail"]) {
      const message = inbound(rawMessage({ trace: [cloudflareVerdict(dmarc, "firm.com")] }));
      await worker.email(message, capped());
      expect(message.rejected).toEqual([]);
    }
    const bare = inbound(rawMessage({ trace: [] }));
    await worker.email(bare, capped());
    expect(bare.rejected).toEqual([]);
    expect(await today()).toBe(before.count);
    expect(await stored()).toBe(before.stored);
  });

  it("drops it while paused too, holding nothing", async () => {
    await worker.email(inbound(rawMessage({ trace: [cloudflareVerdict("none", "firm.com")] })),
      { ...testEnv, SERVICE_PAUSED: "true" } as typeof testEnv);
    expect((await testEnv.DOCS.list({ prefix: "held/" })).objects).toEqual([]);
  });

  it("admits Jim's real-shaped Gmail message and starts its review", async () => {
    const env = { ...testEnv, ALLOWED_SENDERS: SENDER } as typeof testEnv;
    const messageId = `<CAHx9f2k-${crypto.randomUUID()}@mail.gmail.com>`;
    await using instance = await introspectWorkflowInstance(testEnv.REVIEW_FLOW, await instanceId(messageId, SENDER));
    await instance.modify(async (m) => { await m.mockEvent({ type: "idle", payload: {} }); });
    await worker.email(inbound(rawMessage({ from: SENDER, messageId })), env);
    await instance.waitForStatus("complete");
    expect(await instance.getOutput()).toMatchObject({ status: "replied" });
  });

  it("reads only the headers of a message too big to take, however the stream splits", async () => {
    const message = rawMessage();
    const split = message.indexOf("\r\n\r\n") + 2; // the blank line across two chunks
    let pulled = 0;
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) {
        pulled += 1;
        if (pulled === 1) controller.enqueue(new TextEncoder().encode(message.slice(0, split)));
        else if (pulled === 2) controller.enqueue(new TextEncoder().encode(message.slice(split)));
        else controller.enqueue(new Uint8Array(64 * 1024)); // a body that never ends
      },
    });
    const head = await readHeaderBlock(stream);
    expect(pulled).toBeLessThanOrEqual(3);
    expect(senderAuthenticated(headerFields(head), "jim@firm.com").ok).toBe(true);
  });
});
