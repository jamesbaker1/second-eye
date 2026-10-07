/**
 * POST /anthropic/webhook and the daily sweep. The signature is checked with
 * the Anthropic SDK's own `beta.webhooks.unwrap()`, running here in workerd,
 * against deliveries signed the Standard Webhooks way.
 */

import { introspectWorkflowInstance } from "cloudflare:test";
import { Webhook } from "standardwebhooks";
import { describe, expect, it } from "vitest";
import worker from "../src/index";
import { verify } from "../src/webhook";
import { calls, newJob, sent, sentSince, storeMessage, testEnv } from "./helpers";

const KEY = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw";

function delivery(event: object, key = KEY, at = new Date()): Request {
  const id = (event as { id: string }).id;
  const body = JSON.stringify(event);
  const signature = new Webhook(key).sign(id, at, body);
  return new Request("https://edge.test/anthropic/webhook", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "webhook-id": id,
      "webhook-timestamp": String(Math.floor(at.getTime() / 1000)),
      "webhook-signature": signature,
    },
    body,
  });
}

function idled(sessionId: string, id = `whe_${crypto.randomUUID()}`) {
  return {
    type: "event", id, created_at: new Date().toISOString(),
    data: { type: "session.status_idled", id: sessionId, organization_id: "org", workspace_id: "ws" },
  };
}

describe("the Anthropic webhook", () => {
  it("verifies with the SDK's unwrap, in workerd", () => {
    const sample = idled("sesn_1", "whe_1");
    const request = delivery(sample);
    const body = JSON.stringify(sample);
    const event = verify(body, request.headers, KEY);
    expect(event.data?.id).toBe("sesn_1");
    expect(() => verify(body.replace("sesn_1", "sesn_2"), request.headers, KEY)).toThrow();
    const stale = delivery(sample, KEY, new Date(Date.now() - 10 * 60_000));
    expect(() => verify(body, stale.headers, KEY)).toThrow();
  });

  it("refuses a delivery that is not signed with our key", async () => {
    const forged = delivery(idled("sesn_x"), "whsec_" + btoa("a different key, 32 bytes long!!"));
    expect((await worker.fetch(forged, testEnv)).status).toBe(401);
    const unsigned = new Request("https://edge.test/anthropic/webhook", { method: "POST", body: "{}" });
    expect((await worker.fetch(unsigned, testEnv)).status).toBe(401);
  });

  it("answers 404 when no signing key is configured", async () => {
    const response = await worker.fetch(delivery(idled("sesn_x")),
      { ...testEnv, ANTHROPIC_WEBHOOK_SIGNING_KEY: "" });
    expect(response.status).toBe(404);
  });

  it("wakes the instance waiting on that session, well before its 60-second wait ends", async () => {
    const job = newJob();
    const before = (await sent()).length;
    await using instance = await introspectWorkflowInstance(testEnv.REVIEW_FLOW, job);
    await testEnv.REVIEW_FLOW.create({ id: job, params: { key: await storeMessage(), receivedAt: "" } });
    await instance.waitForStepResult({ name: "session-map" });

    const response = await worker.fetch(delivery(idled(`sesn_${job}`)), testEnv);
    expect(response.status).toBe(204);
    await instance.waitForStatus("complete");
    expect((await calls(job)).map((c) => c.path)).toContain("/finish");
    expect((await sentSince(before)).map((m) => m.text)).toEqual(["The review."]);
  });

  it("acts on each event once, and always answers 2xx once verified", async () => {
    const event = idled("sesn_nobody_waits_on");
    expect((await worker.fetch(delivery(event), testEnv)).status).toBe(204);
    expect((await worker.fetch(delivery(event), testEnv)).status).toBe(204);
    const { n } = (await testEnv.DB.prepare("SELECT count(*) AS n FROM webhook_events WHERE id = ?")
      .bind(event.id).first<{ n: number }>())!;
    expect(n).toBe(1);
    // An event type we do not act on, and a malformed body, are still 2xx.
    const other = { ...idled("sesn_y"), data: { type: "agent.updated", id: "agent_1" } };
    expect((await worker.fetch(delivery(other), testEnv)).status).toBe(204);
  });
});

describe("the daily sweep", () => {
  it("wakes an instance still waiting on its session, which then polls", async () => {
    const job = newJob();
    const before = (await sent()).length;
    await using instance = await introspectWorkflowInstance(testEnv.REVIEW_FLOW, job);
    await testEnv.REVIEW_FLOW.create({ id: job, params: { key: await storeMessage(), receivedAt: "" } });
    await instance.waitForStepResult({ name: "session-map" });
    await testEnv.DB.prepare("UPDATE review_sessions SET created_at = ? WHERE instance_id = ?")
      .bind(new Date(Date.now() - 3600_000).toISOString(), job).run();

    await worker.scheduled({ cron: "17 4 * * *", scheduledTime: Date.now(), noRetry() {} } as ScheduledController,
      testEnv);
    await instance.waitForStatus("complete");
    expect((await sentSince(before)).map((m) => m.text)).toEqual(["The review."]);
  });

  it("closes sessions whose instance has ended or gone, and purges old rows", async () => {
    const job = newJob();
    await using instance = await introspectWorkflowInstance(testEnv.REVIEW_FLOW, job);
    await instance.modify(async (m) => { await m.mockEvent({ type: "idle", payload: {} }); });
    await testEnv.REVIEW_FLOW.create({ id: job, params: { key: await storeMessage(), receivedAt: "" } });
    await instance.waitForStatus("complete");

    const long = new Date(Date.now() - 3600_000).toISOString();
    const ancient = new Date(Date.now() - 40 * 86_400_000).toISOString();
    await testEnv.DB.batch([
      // As if the instance had ended without marking its session done.
      testEnv.DB.prepare("UPDATE review_sessions SET done_at = NULL, created_at = ? WHERE instance_id = ?")
        .bind(long, job),
      testEnv.DB.prepare("INSERT INTO review_sessions (session_id, instance_id, created_at) VALUES (?, ?, ?)")
        .bind("sesn_orphan", "rf-never-created", long),
      testEnv.DB.prepare("INSERT INTO outbox (key, state, message_id, updated_at) VALUES (?, 'sent', 'm', ?)")
        .bind("rf-old:reply", ancient),
    ]);

    await worker.scheduled({ cron: "17 4 * * *", scheduledTime: Date.now(), noRetry() {} } as ScheduledController,
      testEnv);

    const open = await testEnv.DB.prepare(
      "SELECT count(*) AS n FROM review_sessions WHERE done_at IS NULL AND instance_id IN (?, ?)",
    ).bind(job, "rf-never-created").first<{ n: number }>();
    expect(open?.n).toBe(0);
    expect(await testEnv.DB.prepare("SELECT 1 FROM outbox WHERE key = 'rf-old:reply'").first()).toBeNull();
  });

  it("runs the container's retention sweep, as our Worker", async () => {
    const before = (await calls("")).filter((c) => c.path === "/retention/sweep").length;
    await worker.scheduled({ cron: "17 4 * * *", scheduledTime: Date.now(), noRetry() {} } as ScheduledController,
      testEnv);
    const swept = (await calls("")).filter((c) => c.path === "/retention/sweep");
    expect(swept.length).toBe(before + 1);
  });
});

describe("a lawyer's document-system token that Anthropic could not refresh", () => {
  function refreshFailed(credentialId: string, vaultId: string, id = `whe_${crypto.randomUUID()}`) {
    return {
      type: "event", id, created_at: new Date().toISOString(),
      data: { type: "vault_credential.refresh_failed", id: credentialId, vault_id: vaultId,
        organization_id: "org", workspace_id: "ws" },
    };
  }

  async function connected(address: string, vaultId: string, credentialId: string): Promise<void> {
    // The table the container writes on consent (src/lra/dms_mcp.py).
    await testEnv.DB.batch([
      testEnv.DB.prepare("CREATE TABLE IF NOT EXISTS dms_vaults (user_address TEXT PRIMARY KEY, " +
        "vault_id TEXT NOT NULL, credential_id TEXT, mcp_server_url TEXT, updated_at TEXT)"),
      testEnv.DB.prepare("INSERT OR REPLACE INTO dms_vaults (user_address, vault_id, credential_id) VALUES (?, ?, ?)")
        .bind(address, vaultId, credentialId),
    ]);
  }

  it("emails the lawyer once, telling them to reply connect", async () => {
    await connected("jim@firm.com", "vlt_jim", "vcrd_jim");
    const before = (await sent()).length;
    const event = refreshFailed("vcrd_jim", "vlt_jim");
    expect((await worker.fetch(delivery(event), testEnv)).status).toBe(204);
    expect((await worker.fetch(delivery(event), testEnv)).status).toBe(204);
    const out = await sentSince(before);
    expect(out).toHaveLength(1);
    expect(out[0].text).toContain("Reply connect to reconnect your document system.");
    expect((out[0] as unknown as { to: string[] }).to).toEqual(["jim@firm.com"]);
  });

  it("emails nobody for a credential it has no record of, or one since replaced", async () => {
    await connected("ann@firm.com", "vlt_ann", "vcrd_ann_2");
    const before = (await sent()).length;
    for (const event of [refreshFailed("vcrd_unknown", "vlt_unknown"), refreshFailed("vcrd_ann_1", "vlt_ann")]) {
      expect((await worker.fetch(delivery(event), testEnv)).status).toBe(204);
    }
    expect(await sentSince(before)).toHaveLength(0);
  });

  it("emails nobody while the service is paused, or anyone off the allowlist", async () => {
    await connected("stranger@elsewhere.com", "vlt_s", "vcrd_s");
    await connected("jim@firm.com", "vlt_jim", "vcrd_jim");
    const before = (await sent()).length;
    await worker.fetch(delivery(refreshFailed("vcrd_s", "vlt_s")), testEnv);
    await worker.fetch(delivery(refreshFailed("vcrd_jim", "vlt_jim")), { ...testEnv, SERVICE_PAUSED: "true" });
    expect(await sentSince(before)).toHaveLength(0);
  });
});
