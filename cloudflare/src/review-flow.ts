/**
 * One email, end to end, as a Cloudflare Workflow (docs/migration.md, Phase 2).
 *
 *   prepare         container /prepare: parse, claim, route; a reply that is
 *                   not a review is sent there and the instance ends
 *   session-start   container /session/start, then session-map in D1 so the
 *                   webhook can find this instance
 *   idle-N/status-N wait up to 60 s for the "idle" event (webhook.ts), then
 *                   ask the container how the session is, until it is not
 *                   running or the deadline passes
 *   notice          once, if the session is still running when the notice
 *                   is due (90 s): "Reviewing X; back in about N minutes"
 *   finish          container /finish: the reply, stored sealed in R2
 *   send            the reply, through the outbox; without its files if it
 *                   is too big
 *   finished        container /finished: aliases, announcements, job row
 *
 * On any failure: fail (container /fail) -> send-failure -> finished-failure,
 * and the instance ends errored so it shows up. Nothing is sent after the
 * verdict: the notice step is only reachable inside the wait loop, before
 * finish, and a failure after the reply was delivered sends nothing more.
 *
 * Every decision below is made from step results, never from the clock or
 * from state outside a step, because the engine replays run() from the top
 * after it hibernates and each step returns its stored result.
 */

import { getContainer } from "@cloudflare/containers";
import { WorkflowEntrypoint } from "cloudflare:workers";
import { NonRetryableError } from "cloudflare:workflows";
import type { WorkflowEvent, WorkflowStep, WorkflowStepConfig } from "cloudflare:workers";
import type { Env } from "./env";
import { addressOf, failureNotice, HELD, isAllowed, parseHeaders } from "./mail";
import { type Outbound, type Sent, sendOnce, tooLarge } from "./outbox";
import { loadPause } from "./pause";
import { unseal } from "./seal";
import { mapSession, sessionDone } from "./webhook";

export interface FlowParams {
  /** The sealed raw message in R2. */
  key: string;
  receivedAt: string;
}

interface Prepared {
  status: "review" | "done" | "duplicate" | "unparseable";
  filename?: string;
  deadlineSeconds?: number;
  noticeAfterSeconds?: number | null;
  notice?: Outbound | null;
}

interface Finished {
  outbound: string | null;
  fallback: string;
}

// The retry budgets in docs/migration.md. A step's wall clock is unlimited;
// the timeout is per attempt and covers a container that stops answering.
const PREPARE: WorkflowStepConfig = {
  retries: { limit: 3, delay: "30 seconds", backoff: "constant" }, timeout: "15 minutes",
};
const START: WorkflowStepConfig = {
  retries: { limit: 3, delay: "10 seconds", backoff: "constant" }, timeout: "5 minutes",
};
const POLL: WorkflowStepConfig = {
  retries: { limit: 3, delay: "10 seconds", backoff: "constant" }, timeout: "2 minutes",
};
const FINISH: WorkflowStepConfig = {
  retries: { limit: 3, delay: "15 seconds", backoff: "constant" }, timeout: "15 minutes",
};
const SEND: WorkflowStepConfig = {
  retries: { limit: 5, delay: "30 seconds", backoff: "constant" }, timeout: "2 minutes",
};
const LOCAL: WorkflowStepConfig = {
  retries: { limit: 3, delay: "5 seconds", backoff: "constant" }, timeout: "1 minute",
};

/** How often a paused instance looks again before giving up: an hour of
 *  five-minute checks, then hourly to 7 days. */
const PAUSED_CHECKS = 12 + 7 * 24 - 1;

/** How long one wait for the webhook lasts before polling anyway. */
export const WAIT_SECONDS = 60;

export class ReviewFlow extends WorkflowEntrypoint<Env, FlowParams> {
  async run(event: Readonly<WorkflowEvent<FlowParams>>, step: WorkflowStep): Promise<unknown> {
    const env = this.env;
    const job = event.instanceId;
    const { key } = event.payload;
    let prepared: Prepared | null = null;
    let delivered = false;
    // The slow-review notice's id, once sent: a reply to it belongs to this
    // conversation, whether the review then succeeds or fails.
    let noticeId: string | null = null;
    try {
      // Paused after the message arrived (pause.ts): it waits here, unread,
      // and the container is not woken for it until the switch is off.
      await this.whilePaused(step, "prepare");
      prepared = await step.do("prepare", PREPARE, async () => {
        const object = await env.DOCS.get(key);
        if (!object) throw new NonRetryableError("the stored message is gone");
        return (await container(env, "/prepare", job, await object.arrayBuffer())) as Prepared;
      });
      if (prepared.status !== "review") {
        await step.do("discard", LOCAL, async () => { await env.DOCS.delete(key); return true; });
        return { status: prepared.status };
      }

      const started = await step.do("session-start", START, async () => {
        const { sessionId } = (await container(env, "/session/start", job, {})) as
          { sessionId: string };
        return { sessionId, at: Date.now() };
      });
      await step.do("session-map", LOCAL, async () => {
        await mapSession(env, started.sessionId, job);
        return true;
      });

      const deadline = prepared.deadlineSeconds ?? 600;
      const noticeDue = prepared.notice ? (prepared.noticeAfterSeconds ?? null) : null;
      let noticeSettled = noticeDue === null;
      let state = "running";
      let elapsed = 0;
      for (let round = 1; state === "running" && elapsed < deadline; round++) {
        // Wake at the notice's moment if it is sooner than a full wait.
        const untilNotice = noticeSettled ? Infinity : Math.ceil((noticeDue as number) - elapsed);
        const wait = Math.max(1, Math.min(WAIT_SECONDS, untilNotice));
        try {
          await step.waitForEvent(`idle-${round}`, { type: "idle", timeout: `${wait} seconds` });
        } catch {
          // A timeout: no webhook yet, or a lost one. Poll either way.
        }
        const polled = await step.do(`status-${round}`, POLL, async () => {
          const { state } = (await container(env, "/session/status", job,
            { sessionId: started.sessionId })) as { state: string };
          return { state, elapsed: (Date.now() - started.at) / 1000 };
        });
        state = polled.state;
        elapsed = polled.elapsed;
        if (state === "running" && !noticeSettled && elapsed >= (noticeDue as number)) {
          noticeSettled = true;
          try {
            noticeId = await step.do("notice", SEND, async () =>
              (await sendOnce(env, `${job}:notice`, prepared!.notice as Outbound)).messageId);
          } catch (error) {
            // Never the review's problem: the verdict still goes out.
            console.error("could not send the slow-review notice", job, String(error));
          }
        }
      }

      const finished = await step.do("finish", FINISH, async () =>
        (await container(env, "/finish", job, { noticeMessageId: noticeId })) as Finished);
      // A review that finishes while paused keeps its reply, sealed in R2,
      // and sends it when the switch is off: nothing leaves while paused.
      await this.whilePaused(step, "send");
      const sent = await step.do("send", SEND, () => sendReply(env, job, finished));
      delivered = true;
      await step.do("finished", FINISH, async () => {
        await container(env, "/finished", job, { kind: "review", messageId: sent.messageId });
        return true;
      });
      await step.do("discard", LOCAL, async () => {
        await sessionDone(env, job);
        await env.DOCS.delete(key);
        return true;
      });
      return { status: "replied", messageId: sent.messageId, notice: noticeId };
    } catch (error) {
      if (delivered) {
        // The lawyer has the review. Whatever failed afterwards must not
        // produce a second email saying it was never reviewed.
        console.error("after the reply was sent", job, String(error));
        return { status: "replied", afterwards: String(error).slice(0, 300) };
      }
      // The instance ends "errored" only when the lawyer heard nothing: a
      // review that failed but was answered is a handled outcome, and an
      // errored instance for it made the dashboard say otherwise.
      if (await this.failed(step, job, key, error, noticeId)) {
        return { status: "failed-replied", reason: String(error).slice(0, 300) };
      }
      throw error;
    }
  }

  /**
   * Wait, without waking anything, while the kill switch is on (pause.ts).
   * Every five minutes for the first hour, then hourly: `second-eye resume` reaches
   * a waiting instance within the hour (and mail held at the door at once,
   * by the release). Bounded by the instance's step budget; a message still
   * waiting after 7 days is gone with every unreviewed message (the R2
   * lifecycle on inbound/), and the failure path says so if it can.
   */
  private async whilePaused(step: WorkflowStep, at: string): Promise<void> {
    const env = this.env;
    for (let n = 1; n <= PAUSED_CHECKS; n++) {
      const on = await step.do(`${at}-paused-${n}`, LOCAL, async () => loadPause(env));
      if (!on) return;
      await step.sleep(`${at}-held-${n}`, n <= 12 ? "5 minutes" : "1 hour");
    }
    throw new NonRetryableError("SERVICE_PAUSED for longer than a message is kept");
  }

  /** The one email a failed review still owes, then clean up. */
  private async failed(step: WorkflowStep, job: string, key: string, error: unknown,
    noticeId: string | null): Promise<boolean> {
    const env = this.env;
    const reason = String(error).slice(0, 300);
    console.error("review failed", job, reason);
    let replied = false;
    try {
      const failed = await step.do("fail", FINISH, async () =>
        (await container(env, "/fail", job, { error: reason, noticeMessageId: noticeId })) as
          { outbound: string | null });
      const sent = await step.do("send-failure", SEND, async (): Promise<Sent | null> => {
        if (failed.outbound) {
          return sendOnce(env, `${job}:failure`, await readStored(env, failed.outbound));
        }
        // Nothing was prepared, so the application has nothing to say. Answer
        // from the headers alone, as the Worker always has, and only to
        // a sender the edge admitted.
        const object = await env.DOCS.get(key);
        if (!object) return null;
        const headers = parseHeaders(await unseal(await object.arrayBuffer(), env));
        const sender = addressOf(headers.get("from") ?? "");
        if (!sender.includes("@") || !isAllowed(sender, env.ALLOWED_SENDERS) || !env.MAIL_AGENT_ADDRESS) {
          return null;
        }
        return sendOnce(env, `${job}:failure`, failureNotice(headers, sender, env) as Outbound);
      });
      replied = sent !== null;
      if (failed.outbound) {
        await step.do("finished-failure", FINISH, async () => {
          await container(env, "/finished", job, { kind: "failure", messageId: sent?.messageId ?? null });
          return true;
        });
      }
    } catch (second) {
      console.error("could not tell the sender about a failed review", job, String(second));
    }
    try {
      await step.do("discard-failed", LOCAL, async () => {
        await sessionDone(env, job);
        const object = (await loadPause(env)) ? await env.DOCS.get(key) : null;
        if (object) {
          // SERVICE_PAUSED: the review failed because nothing may be sent or
          // run. Held for release, as a new message would be.
          await env.DOCS.put(`${HELD}${crypto.randomUUID()}.eml`, await object.arrayBuffer());
        }
        await env.DOCS.delete(key);
        return true;
      });
    } catch (third) {
      console.error("could not clean up a failed review", job, String(third));
    }
    return replied;
  }
}

/** The reply, or its no-attachment variant if the full one is too big. One
 *  outbox key for both: whichever goes, it is the one reply. */
async function sendReply(env: Env, job: string, finished: Finished): Promise<Sent> {
  if (finished.outbound) {
    try {
      return await sendOnce(env, `${job}:reply`, await readStored(env, finished.outbound));
    } catch (error) {
      if (!tooLarge(error)) throw error;
      console.log("reply too big for Email Service; sending it without its files", job);
    }
  }
  return sendOnce(env, `${job}:reply`, await readStored(env, finished.fallback));
}

/** A reply the container left sealed in R2 (jobstate.py). */
async function readStored(env: Env, key: string): Promise<Outbound> {
  if (!/^job\/[0-9a-f]{32}$/.test(key)) throw new NonRetryableError(`bad job key ${key}`);
  const object = await env.DOCS.get(key);
  if (!object) throw new NonRetryableError(`the stored reply ${key} is gone`);
  return JSON.parse(new TextDecoder().decode(await unseal(await object.arrayBuffer(), env))) as Outbound;
}

/**
 * One call to a container step. 2xx is the step's result. 400, 401, 404 and
 * 422 will not change on a retry, so they stop it (NonRetryableError);
 * anything else, a busy container included, is retried by the step's config.
 */
async function container(env: Env, path: string, job: string, body: unknown): Promise<unknown> {
  const raw = body instanceof ArrayBuffer;
  const response = await getContainer(env.REVIEW).fetch(new Request(`http://container${path}`, {
    method: "POST",
    headers: {
      authorization: `Bearer ${env.EDGE_SECRET}`,
      "x-job-id": job,
      "content-type": raw ? "message/rfc822" : "application/json",
    },
    body: raw ? body : JSON.stringify(body),
  }));
  if (response.ok) return response.json();
  const detail = (await response.text()).slice(0, 300);
  const message = `container ${path} answered ${response.status}: ${detail}`;
  if ([400, 401, 404, 422].includes(response.status)) throw new NonRetryableError(message);
  throw new Error(message);
}

/**
 * The instance id for a message: the same message from the same sender is
 * the same instance, so a second delivery cannot start a second review.
 * Workflows allow ^[a-zA-Z0-9_][a-zA-Z0-9-_]*$, up to 100 characters.
 */
export async function instanceId(messageId: string, sender: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256",
    new TextEncoder().encode(`${messageId.trim()}|${sender.trim().toLowerCase()}`));
  const hex = [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
  return `rf-${hex.slice(0, 40)}`;
}
