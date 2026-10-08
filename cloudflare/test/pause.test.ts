/**
 * The kill switch without a deploy (src/pause.ts): one row in D1, written by
 * `second-eye pause` / `second-eye resume` through `wrangler d1 execute`, read on every
 * message, send, cron run and Workflow step that could wake or send. The
 * SERVICE_PAUSED var still works and wins.
 */

import { introspectWorkflowInstance } from "cloudflare:test";
import { afterEach, describe, expect, it } from "vitest";
import worker, { EDGE_HOST, fromContainer } from "../src/index";
import { sendOnce } from "../src/outbox";
import { loadPause, PAUSE_KEY, setPause } from "../src/pause";
import { instanceId } from "../src/review-flow";
import { calls, inbound, newJob, rawMessage, sent, sentSince, storeMessage, testEnv } from "./helpers";
import { outbound } from "./worker";

const heldKeys = async () => (await testEnv.DOCS.list({ prefix: "held/" })).objects.map((o) => o.key);

/** Exactly what `second-eye pause` runs (src/secondeye/killswitch.py): the SQL is the contract. */
async function lraPause(on: boolean): Promise<void> {
  await setPause(testEnv.DB, on, "it@firm.com");
}

// The container's way in (index.ts, fromContainer): /internal/* is not on the public hostname.
const internal = (path: string, body?: unknown) => fromContainer(new Request(`http://${EDGE_HOST}${path}`, {
  method: "POST", headers: { authorization: "Bearer s3cret" }, body: body === undefined ? undefined : JSON.stringify(body),
}), testEnv);

afterEach(async () => {
  await lraPause(false);
  await loadPause(testEnv);
});

describe("the runtime kill switch", () => {
  it("is off in a database where it was never flipped", async () => {
    await testEnv.DB.prepare("DROP TABLE IF EXISTS edge_settings").run();
    expect(await loadPause(testEnv)).toBe(false);
  });

  it("is read from the row, and the var still pauses whatever the row says", async () => {
    await lraPause(true);
    expect(await loadPause(testEnv)).toBe(true);
    await lraPause(false);
    expect(await loadPause(testEnv)).toBe(false);
    expect(await loadPause({ ...testEnv, SERVICE_PAUSED: "true" })).toBe(true);
    const row = await testEnv.DB.prepare("SELECT value, updated_by FROM edge_settings WHERE key = ?")
      .bind(PAUSE_KEY).first();
    expect(row).toEqual({ value: "false", updated_by: "it@firm.com" });
  });

  it("holds mail when the database cannot be read, rather than sending", async () => {
    const broken = {
      prepare() { throw new Error("D1_ERROR: network connection lost"); },
    } as unknown as D1Database;
    expect(await loadPause({ DB: broken })).toBe(true);
  });

  it("holds mail, refuses sends and keeps held mail held, with no deploy", async () => {
    const before = (await sent()).length;
    const messageId = `<runtime-${crypto.randomUUID()}@firm.com>`;
    await lraPause(true);

    // testEnv's SERVICE_PAUSED is unset: only the row says paused.
    expect(testEnv.SERVICE_PAUSED).toBeUndefined();
    await worker.email(inbound(rawMessage({ from: "ann@firm.com", messageId })), testEnv);
    expect((await heldKeys()).length).toBe(1);
    expect((await internal("/internal/send", outbound("x"))).status).toBe(503);
    await expect(sendOnce(testEnv, "rf-runtime:reply", outbound("x") as never)).rejects.toThrow(/PAUSED/);
    expect(await (await internal("/internal/release")).json()).toEqual({ released: 0, paused: true });
    expect((await sentSince(before)).length).toBe(0);

    // `second-eye resume`: the next release lets it go, reviewed once.
    await lraPause(false);
    const id = await instanceId(messageId, "ann@firm.com");
    await using instance = await introspectWorkflowInstance(testEnv.REVIEW_FLOW, id);
    await instance.modify(async (m) => { await m.mockEvent({ type: "idle", payload: {} }); });
    expect(await (await internal("/internal/release")).json()).toEqual({ released: 1, paused: false });
    await instance.waitForStatus("complete");
    expect(await heldKeys()).toEqual([]);
    expect((await sentSince(before)).map((m) => m.text)).toEqual(["The review."]);
  });

  it("keeps a message already in a Workflow waiting, unread, until it is switched off", async () => {
    const job = newJob();
    const key = await storeMessage();
    const before = (await sent()).length;
    await lraPause(true);
    await using instance = await introspectWorkflowInstance(testEnv.REVIEW_FLOW, job);
    await instance.modify(async (m) => {
      await m.disableSleeps();
      await m.mockEvent({ type: "idle", payload: {} });
    });
    await testEnv.REVIEW_FLOW.create({ id: job, params: { key, receivedAt: new Date().toISOString() } });

    // The Workflow read the row itself, and waited instead of waking the container.
    expect(await instance.waitForStepResult({ name: "prepare-paused-1" })).toBe(true);
    await lraPause(false);
    await instance.waitForStatus("complete");
    expect(await instance.getOutput()).toMatchObject({ status: "replied" });
    expect((await calls(job)).map((c) => c.path)[0]).toBe("/prepare");
    expect((await sentSince(before)).map((m) => m.text)).toEqual(["The review."]);
  });
});
