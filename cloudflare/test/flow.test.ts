/**
 * ReviewFlow against a scripted container and a recording email service.
 * What is held here: the order of the steps, that a lost webhook costs a
 * poll and nothing more, that the slow-review notice can only come before
 * the verdict, that every failure ends in exactly one email to the lawyer,
 * and that a retried step never sends twice.
 */

import { introspectWorkflowInstance } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import { outbound } from "./worker";
import {
  calls, failNextSend, newJob, plan, rawMessage, sent, sentSince, storeMessage, testEnv,
} from "./helpers";

async function start(job: string, key: string): Promise<void> {
  await testEnv.REVIEW_FLOW.create({ id: job, params: { key, receivedAt: new Date().toISOString() } });
}

const IDLE = { type: "idle", payload: { webhook: "session.status_idled" } };

describe("the review Workflow", () => {
  it("runs prepare, session, finish, send, finished when the webhook arrives", async () => {
    const job = newJob();
    const key = await storeMessage();
    const before = (await sent()).length;
    await using instance = await introspectWorkflowInstance(testEnv.REVIEW_FLOW, job);
    await instance.modify(async (m) => { await m.mockEvent(IDLE); });
    await start(job, key);
    await instance.waitForStatus("complete");

    expect(await instance.getOutput()).toMatchObject({ status: "replied", notice: null });
    expect((await calls(job)).map((c) => c.path)).toEqual([
      "/prepare", "/session/start", "/session/status", "/finish", "/finished",
    ]);
    const mail = await sentSince(before);
    expect(mail.map((m) => m.text)).toEqual(["The review."]);
    expect(mail[0].attachments).toHaveLength(1);
    // The message id the reply went out under reaches the container.
    const finished = (await calls(job)).find((c) => c.path === "/finished")!;
    expect(JSON.parse(finished.body)).toMatchObject({ kind: "review", messageId: expect.stringMatching(/^msg-/) });
    // The stored message is gone, and the session is no longer waited on.
    expect(await testEnv.DOCS.get(key)).toBeNull();
    const row = await testEnv.DB.prepare("SELECT done_at FROM review_sessions WHERE instance_id = ?")
      .bind(job).first<{ done_at: string | null }>();
    expect(row?.done_at).toBeTruthy();
  });

  it("polls when the webhook is lost, and keeps waiting while the session runs", async () => {
    const job = newJob();
    await plan(job, { "/session/status": [{ body: { state: "running" } }, { body: { state: "done" } }] });
    const before = (await sent()).length;
    await using instance = await introspectWorkflowInstance(testEnv.REVIEW_FLOW, job);
    await instance.modify(async (m) => {
      await m.forceEventTimeout({ name: "idle-1" });
      await m.forceEventTimeout({ name: "idle-2" });
    });
    await start(job, await storeMessage());
    await instance.waitForStatus("complete");

    const paths = (await calls(job)).map((c) => c.path);
    expect(paths.filter((p) => p === "/session/status")).toHaveLength(2);
    expect(paths.at(-2)).toBe("/finish");
    expect((await sentSince(before)).map((m) => m.text)).toEqual(["The review."]);
  });

  it("sends the slow-review notice once, before the verdict, and threads it", async () => {
    const job = newJob();
    const before = (await sent()).length;
    await using instance = await introspectWorkflowInstance(testEnv.REVIEW_FLOW, job);
    await instance.modify(async (m) => {
      await m.forceEventTimeout({ name: "idle-1" });
      await m.forceEventTimeout({ name: "idle-2" });
      // Still running 95 seconds in: the notice is due (90 s).
      await m.mockStepResult({ name: "status-1" }, { state: "running", elapsed: 95 });
    });
    await start(job, await storeMessage());
    await instance.waitForStatus("complete");

    const mail = await sentSince(before);
    expect(mail.map((m) => m.text)).toEqual(["Reviewing NDA.docx; back in about 5 minutes.", "The review."]);
    const output = (await instance.getOutput()) as { notice: string };
    expect(output.notice).toMatch(/^msg-/);
    // /finish is told, so a reply to the notice lands in the conversation.
    const finish = (await calls(job)).find((c) => c.path === "/finish")!;
    expect(JSON.parse(finish.body)).toEqual({ noticeMessageId: output.notice });
  });

  it("never sends the notice once the review is done, however late", async () => {
    const job = newJob();
    const before = (await sent()).length;
    await using instance = await introspectWorkflowInstance(testEnv.REVIEW_FLOW, job);
    await instance.modify(async (m) => {
      await m.forceEventTimeout({ name: "idle-1" });
      await m.mockStepResult({ name: "status-1" }, { state: "done", elapsed: 400 });
    });
    await start(job, await storeMessage());
    await instance.waitForStatus("complete");
    expect((await sentSince(before)).map((m) => m.text)).toEqual(["The review."]);
  });

  it("stops waiting at the deadline and collects what there is", async () => {
    const job = newJob();
    await plan(job, { "/session/status": [{ body: { state: "running" } }] });
    const before = (await sent()).length;
    await using instance = await introspectWorkflowInstance(testEnv.REVIEW_FLOW, job);
    await instance.modify(async (m) => {
      await m.forceEventTimeout({ name: "idle-1" });
      await m.mockStepResult({ name: "status-1" }, { state: "running", elapsed: 700 });
    });
    await start(job, await storeMessage());
    await instance.waitForStatus("complete");
    const paths = (await calls(job)).map((c) => c.path);
    expect(paths).toContain("/finish");
    // Past the notice's moment and still running when last asked: the notice
    // goes, and still before the verdict.
    expect((await sentSince(before)).map((m) => m.text)).toEqual([
      "Reviewing NDA.docx; back in about 5 minutes.", "The review."]);
  });

  it("answers a message that is not a review in prepare and sends nothing itself", async () => {
    const job = newJob();
    await plan(job, { "/prepare": [{ body: { status: "done" } }] });
    const key = await storeMessage();
    const before = (await sent()).length;
    await using instance = await introspectWorkflowInstance(testEnv.REVIEW_FLOW, job);
    await start(job, key);
    await instance.waitForStatus("complete");
    expect(await instance.getOutput()).toEqual({ status: "done" });
    expect((await calls(job)).map((c) => c.path)).toEqual(["/prepare"]);
    expect(await sentSince(before)).toEqual([]);
    expect(await testEnv.DOCS.get(key)).toBeNull();
  });

  it("sends the no-attachment variant when the reply is too big", async () => {
    const job = newJob();
    const before = (await sent()).length;
    await failNextSend("E_CONTENT_TOO_LARGE", true);
    await using instance = await introspectWorkflowInstance(testEnv.REVIEW_FLOW, job);
    await instance.modify(async (m) => { await m.mockEvent(IDLE); });
    await start(job, await storeMessage());
    await instance.waitForStatus("complete");
    expect((await sentSince(before)).map((m) => m.text)).toEqual(["The review, no files."]);
  });

  it("retries a failed send without sending twice", async () => {
    const job = newJob();
    const before = (await sent()).length;
    await failNextSend("E_RATE_LIMIT_EXCEEDED");
    // And the step after the send fails once, so it is retried too.
    await plan(job, { "/finished": [{ status: 500, body: { detail: "down" } }, { body: { status: "ok" } }] });
    await using instance = await introspectWorkflowInstance(testEnv.REVIEW_FLOW, job);
    await instance.modify(async (m) => {
      await m.mockEvent(IDLE);
      await m.disableRetryDelays();
    });
    await start(job, await storeMessage());
    await instance.waitForStatus("complete");
    expect((await sentSince(before)).map((m) => m.text)).toEqual(["The review."]);
    const row = await testEnv.DB.prepare("SELECT state FROM outbox WHERE key = ?")
      .bind(`${job}:reply`).first<{ state: string }>();
    expect(row?.state).toBe("sent");
  });

  it("sends nothing more when a step after the reply fails for good", async () => {
    const job = newJob();
    const before = (await sent()).length;
    await plan(job, { "/finished": [{ status: 500, body: { detail: "down" } }] });
    await using instance = await introspectWorkflowInstance(testEnv.REVIEW_FLOW, job);
    await instance.modify(async (m) => {
      await m.mockEvent(IDLE);
      await m.disableRetryDelays();
    });
    await start(job, await storeMessage());
    await instance.waitForStatus("complete");
    expect(await instance.getOutput()).toMatchObject({ status: "replied" });
    expect((await sentSince(before)).map((m) => m.text)).toEqual(["The review."]);
    expect((await calls(job)).map((c) => c.path)).not.toContain("/fail");
  });

  it("tells the lawyer once when a step fails for good, and ends handled, not errored", async () => {
    const job = newJob();
    const before = (await sent()).length;
    await plan(job, { "/session/start": [{ status: 500, body: { detail: "boom" } }] });
    await using instance = await introspectWorkflowInstance(testEnv.REVIEW_FLOW, job);
    await instance.modify(async (m) => { await m.disableRetryDelays(); });
    await start(job, await storeMessage());
    await instance.waitForStatus("complete");
    expect(await instance.getOutput()).toMatchObject({ status: "failed-replied" });

    const paths = (await calls(job)).map((c) => c.path);
    // Three retries after the first attempt.
    expect(paths.filter((p) => p === "/session/start")).toHaveLength(4);
    expect(paths.slice(-2)).toEqual(["/fail", "/finished"]);
    expect(JSON.parse((await calls(job)).at(-1)!.body)).toMatchObject({ kind: "failure" });
    expect((await sentSince(before)).map((m) => m.text)).toEqual(["I could not complete the full review."]);
  });

  it("goes to the failure path at once on a mocked step error", async () => {
    const job = newJob();
    const before = (await sent()).length;
    await using instance = await introspectWorkflowInstance(testEnv.REVIEW_FLOW, job);
    await instance.modify(async (m) => {
      await m.mockEvent(IDLE);
      await m.disableRetryDelays();
      await m.mockStepError({ name: "finish" }, new Error("the container went away"));
    });
    await start(job, await storeMessage());
    await instance.waitForStatus("complete");
    expect(await instance.getOutput()).toMatchObject({ status: "failed-replied" });
    const fail = (await calls(job)).find((c) => c.path === "/fail")!;
    expect(JSON.parse(fail.body).error).toContain("the container went away");
    expect((await sentSince(before)).map((m) => m.text)).toEqual(["I could not complete the full review."]);
  });

  it("does not retry what the container says will not change (422)", async () => {
    const job = newJob();
    await plan(job, { "/finish": [{ status: 422, body: { detail: "the review failed" } }] });
    const before = (await sent()).length;
    await using instance = await introspectWorkflowInstance(testEnv.REVIEW_FLOW, job);
    await instance.modify(async (m) => { await m.mockEvent(IDLE); });
    await start(job, await storeMessage());
    await instance.waitForStatus("complete");
    expect(await instance.getOutput()).toMatchObject({ status: "failed-replied" });
    expect((await calls(job)).filter((c) => c.path === "/finish")).toHaveLength(1);
    // Caught, so the failure path still runs: the mechanical-only reply.
    expect((await calls(job)).map((c) => c.path).slice(-2)).toEqual(["/fail", "/finished"]);
    expect((await sentSince(before)).map((m) => m.text)).toEqual(["I could not complete the full review."]);
  });

  it("tells /fail which notice went out, so a reply to it finds the conversation", async () => {
    const job = newJob();
    await plan(job, { "/finish": [{ status: 422, body: { detail: "the review failed" } }] });
    const before = (await sent()).length;
    await using instance = await introspectWorkflowInstance(testEnv.REVIEW_FLOW, job);
    await instance.modify(async (m) => {
      await m.forceEventTimeout({ name: "idle-1" });
      await m.forceEventTimeout({ name: "idle-2" });
      await m.mockStepResult({ name: "status-1" }, { state: "running", elapsed: 95 });
    });
    await start(job, await storeMessage());
    await instance.waitForStatus("complete");

    const mail = await sentSince(before);
    expect(mail.map((m) => m.text)).toEqual([
      "Reviewing NDA.docx; back in about 5 minutes.", "I could not complete the full review."]);
    const fail = (await calls(job)).find((c) => c.path === "/fail")!;
    expect(JSON.parse(fail.body).noticeMessageId).toMatch(/^msg-/);
  });

  it("answers from the headers alone when nothing could be prepared", async () => {
    const job = newJob();
    await plan(job, {
      "/prepare": [{ status: 422, body: { detail: "no" } }],
      "/fail": [{ body: { outbound: null } }],
    });
    const before = (await sent()).length;
    await using instance = await introspectWorkflowInstance(testEnv.REVIEW_FLOW, job);
    await start(job, await storeMessage(rawMessage({ subject: "Supply agreement" })));
    await instance.waitForStatus("complete");
    expect(await instance.getOutput()).toMatchObject({ status: "failed-replied" });
    const mail = await sentSince(before);
    expect(mail).toHaveLength(1);
    expect(mail[0].text).toBe('I couldn\'t review "Supply agreement". Nothing was sent to anyone else. Please resend it.\n'
      + "\nPrivileged & Confidential — Attorney Work Product\n");
    expect((mail[0] as unknown as { headers: Record<string, string> }).headers["X-Privileged"])
      .toBe("Privileged & Confidential - Attorney Work Product");
    expect((mail[0] as unknown as { to: string[] }).to).toEqual(["jim@firm.com"]);
    // No review state, so the container is not asked to record a failure reply.
    expect((await calls(job)).map((c) => c.path)).toEqual(["/prepare", "/fail"]);
  });

  it("says nothing to a sender the edge would not have admitted", async () => {
    const job = newJob();
    await plan(job, {
      "/prepare": [{ status: 422, body: { detail: "no" } }],
      "/fail": [{ body: { outbound: null } }],
    });
    const before = (await sent()).length;
    await using instance = await introspectWorkflowInstance(testEnv.REVIEW_FLOW, job);
    await start(job, await storeMessage(rawMessage({ from: "eve@elsewhere.com" })));
    await instance.waitForStatus("errored");
    expect(await sentSince(before)).toEqual([]);
  });

  it("stores the reply where the container says and reads it sealed", async () => {
    // The reply the container leaves is sealed with the data key; the fake
    // container seals exactly as jobstate.py does, and the send step opens it.
    const job = newJob();
    await plan(job, { "/finish": [{ store: { outbound: outbound("Sealed and opened."), fallback: outbound("x") } }] });
    const before = (await sent()).length;
    await using instance = await introspectWorkflowInstance(testEnv.REVIEW_FLOW, job);
    await instance.modify(async (m) => { await m.mockEvent(IDLE); });
    await start(job, await storeMessage());
    await instance.waitForStatus("complete");
    expect((await sentSince(before)).map((m) => m.text)).toEqual(["Sealed and opened."]);
  });
});
