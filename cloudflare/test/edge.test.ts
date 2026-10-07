/**
 * The edge's own paths into the Workflow: which messages start one, what a
 * second delivery of the same message does, and the outbox behind
 * /internal/send.
 */

import { introspectWorkflowInstance } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import worker, { fromContainer } from "../src/index";
import { sendOnce } from "../src/outbox";
import { instanceId } from "../src/review-flow";
import { inbound, rawMessage, sent, sentSince, testEnv } from "./helpers";
import { outbound } from "./worker";

async function inboundKeys(): Promise<string[]> {
  return (await testEnv.DOCS.list({ prefix: "inbound/" })).objects.map((o) => o.key);
}

describe("choosing the Workflow", () => {
  it("names an instance after the message and its sender, within Workflows' rules", async () => {
    const a = await instanceId("<m1@firm.com>", "jim@firm.com");
    expect(a).toMatch(/^[a-zA-Z0-9_][a-zA-Z0-9-_]*$/);
    expect(a.length).toBeLessThanOrEqual(100);
    expect(await instanceId(" <m1@firm.com> ", "Jim@Firm.com")).toBe(a);
    expect(await instanceId("<m1@firm.com>", "eve@firm.com")).not.toBe(a);
  });

  it("starts one instance for a message however often it is delivered", async () => {
    const messageId = `<twice-${crypto.randomUUID()}@firm.com>`;
    const id = await instanceId(messageId, "jim@firm.com");
    const before = (await sent()).length;
    await using instance = await introspectWorkflowInstance(testEnv.REVIEW_FLOW, id);
    await instance.modify(async (m) => { await m.mockEvent({ type: "idle", payload: {} }); });

    await worker.email(inbound(rawMessage({ messageId })), testEnv);
    await worker.email(inbound(rawMessage({ messageId })), testEnv);
    await instance.waitForStatus("complete");

    expect((await sentSince(before)).map((m) => m.text)).toEqual(["The review."]);
    // The second copy was dropped, and the first cleaned up by its instance.
    expect((await inboundKeys()).length).toBe(0);
  });

  it("starts one for every admitted sender, not only a canary", async () => {
    const messageId = `<ann-${crypto.randomUUID()}@firm.com>`;
    const id = await instanceId(messageId, "ann@firm.com");
    await using instance = await introspectWorkflowInstance(testEnv.REVIEW_FLOW, id);
    await instance.modify(async (m) => { await m.mockEvent({ type: "idle", payload: {} }); });
    await worker.email(inbound(rawMessage({ from: "ann@firm.com", messageId })), testEnv);
    await instance.waitForStatus("complete");
    expect(await instance.getOutput()).toMatchObject({ status: "replied" });
  });

  it("has no queue to fall back on", () => {
    expect("queue" in worker).toBe(false);
    expect("INBOUND" in testEnv).toBe(false);
  });
});

describe("the kill switch", () => {
  const pausedEnv = () => ({ ...testEnv, SERVICE_PAUSED: "true" }) as typeof testEnv;
  const heldKeys = async () => (await testEnv.DOCS.list({ prefix: "held/" })).objects.map((o) => o.key);

  it("holds mail while paused, sends nothing, and releases it when switched off", async () => {
    const before = (await sent()).length;
    const inboundBefore = (await inboundKeys()).length;
    const messageId = `<held-${crypto.randomUUID()}@firm.com>`;
    await worker.email(inbound(rawMessage({ from: "ann@firm.com", messageId })), pausedEnv());
    expect((await heldKeys()).length).toBe(1);
    expect((await inboundKeys()).length).toBe(inboundBefore);

    const refused = await fromContainer(new Request("http://edge.internal/internal/send", {
      method: "POST", headers: { authorization: "Bearer s3cret" }, body: JSON.stringify(outbound("x")),
    }), pausedEnv());
    expect(refused.status).toBe(503);
    await expect(sendOnce(pausedEnv(), "rf-paused:reply", outbound("x") as never)).rejects.toThrow(/PAUSED/);
    expect((await sentSince(before)).length).toBe(0);

    const release = (env: typeof testEnv) => fromContainer(new Request("http://edge.internal/internal/release", {
      method: "POST", headers: { authorization: "Bearer s3cret" },
    }), env);
    expect(await (await release(pausedEnv())).json()).toEqual({ released: 0, paused: true });
    // Released into a Workflow named after the message, as if it had just arrived.
    const id = await instanceId(messageId, "ann@firm.com");
    await using instance = await introspectWorkflowInstance(testEnv.REVIEW_FLOW, id);
    await instance.modify(async (m) => { await m.mockEvent({ type: "idle", payload: {} }); });
    expect(await (await release(testEnv)).json()).toEqual({ released: 1, paused: false });
    expect(await heldKeys()).toEqual([]);
    await instance.waitForStatus("complete");
    expect((await sentSince(before)).map((m) => m.text)).toEqual(["The review."]);
    expect((await inboundKeys()).length).toBe(inboundBefore);
  });

  it("still drops a stranger while paused", async () => {
    await worker.email(inbound(rawMessage({ from: "eve@elsewhere.com" })), pausedEnv());
    expect(await heldKeys()).toEqual([]);
  });
});

describe("the outbox", () => {
  it("sends once per key and hands back the first message id", async () => {
    const before = (await sent()).length;
    const key = `rf-outbox-${crypto.randomUUID().slice(0, 8)}:reply`;
    const first = await sendOnce(testEnv, key, outbound("once") as never);
    const again = await sendOnce(testEnv, key, outbound("once") as never);
    expect(again).toEqual({ messageId: first.messageId, duplicate: true });
    expect((await sentSince(before)).map((m) => m.text)).toEqual(["once"]);
  });

  it("guards the container's own sends on the Workflow path", async () => {
    const before = (await sent()).length;
    const body = JSON.stringify({ ...outbound("a clean copy"), outbox: "rf-abc:prepare-1" });
    const post = () => fromContainer(new Request("http://edge.internal/internal/send", {
      method: "POST", headers: { authorization: "Bearer s3cret" }, body,
    }), testEnv);
    const first = (await (await post()).json()) as { messageId: string };
    const second = (await (await post()).json()) as { messageId: string; duplicate: boolean };
    expect(second).toEqual({ messageId: first.messageId, duplicate: true });
    expect((await sentSince(before)).map((m) => m.text)).toEqual(["a clean copy"]);

    const bad = JSON.stringify({ ...outbound("x"), outbox: "no spaces allowed: here" });
    const refused = await fromContainer(new Request("http://edge.internal/internal/send", {
      method: "POST", headers: { authorization: "Bearer s3cret" }, body: bad,
    }), testEnv);
    expect(refused.status).toBe(400);
  });

  it("sends a reply to one admitted sender only, while REPLY_POLICY is sender_only", async () => {
    const before = (await sent()).length;
    const post = (message: Record<string, unknown>, env = testEnv) =>
      fromContainer(new Request("http://edge.internal/internal/send", {
        method: "POST", headers: { authorization: "Bearer s3cret" }, body: JSON.stringify(message),
      }), env);
    const counterparty = await post({ ...outbound("to them"), to: ["opp@other.com"] });
    expect(counterparty.status).toBe(403);
    const two = await post({ ...outbound("to both"), to: ["jim@firm.com", "opp@other.com"] });
    expect(two.status).toBe(403);
    const keyed = `rf-policy-${crypto.randomUUID().slice(0, 8)}:reply`;
    await expect(sendOnce(testEnv, keyed, { ...outbound("x"), to: ["opp@other.com"] } as never))
      .rejects.toThrow(/sender_only/);
    const copied = await post({ ...outbound("cc dropped"), cc: ["opp@other.com"] });
    expect(copied.status).toBe(200);
    const went = await sentSince(before);
    expect(went.map((m) => m.text)).toEqual(["cc dropped"]);
    expect((went[0] as { cc?: unknown }).cc).toBeUndefined();

    // A firm that set REPLY_POLICY=model has the recipients as composed.
    const open = await post({ ...outbound("model"), to: ["opp@other.com"] },
      { ...testEnv, REPLY_POLICY: "model" });
    expect(open.status).toBe(200);
  });

  it("stores job state under the key shape the container writes", async () => {
    const put = await fromContainer(new Request(`http://edge.internal/internal/blob/job/${"a".repeat(32)}`, {
      method: "PUT", headers: { authorization: "Bearer s3cret" }, body: "enc:v1:...",
    }), testEnv);
    expect(put.status).toBe(200);
    const bad = await fromContainer(new Request("http://edge.internal/internal/blob/job/../thread", {
      method: "PUT", headers: { authorization: "Bearer s3cret" }, body: "x",
    }), testEnv);
    expect(bad.status).toBe(400);
  });
});
