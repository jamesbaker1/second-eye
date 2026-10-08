/**
 * /internal/* is not on the internet. The container reaches it through its
 * outbound handler (http://edge.internal); from the Worker's public hostname
 * it does not exist, unless an operator opens the signed door for a job.
 */

import { describe, expect, it } from "vitest";
import { ContainerProxy, ReviewContainer } from "../src/index";
import worker, { ANTHROPIC_HOST, EDGE_HOST, egress, fromContainer } from "../src/index";
import { SKEW_SECONDS, sign, verifyOperator } from "../src/operator";
import { testEnv } from "./helpers";

const OPERATOR = "o".repeat(40);
const operatorEnv = () => ({ ...testEnv, OPERATOR_SECRET: OPERATOR }) as typeof testEnv;
const DB_BODY = JSON.stringify({ statements: [{ sql: "SELECT 1 AS one", params: [] }] });

async function signed(path: string, opts: {
  method?: string; body?: string; secret?: string; time?: number; nonce?: string;
} = {}): Promise<Request> {
  const method = opts.method ?? "POST";
  const body = opts.body ?? DB_BODY;
  const time = String(Math.floor((opts.time ?? Date.now()) / 1000));
  const nonce = opts.nonce ?? crypto.randomUUID().replace(/-/g, "");
  const bytes = new TextEncoder().encode(method === "GET" ? "" : body).buffer as ArrayBuffer;
  const signature = await sign(opts.secret ?? OPERATOR, method, path, time, nonce, bytes);
  return new Request(`https://edge.test${path}`, {
    method,
    headers: { "x-second-eye-time": time, "x-second-eye-nonce": nonce, "x-second-eye-signature": signature },
    body: method === "GET" ? undefined : body,
  });
}

describe("from the internet", () => {
  it("every /internal path is a 404, with or without the old bearer", async () => {
    for (const [path, method] of [["/internal/db", "POST"], ["/internal/send", "POST"],
      ["/internal/release", "POST"], [`/internal/blob/job/${"a".repeat(32)}`, "GET"]]) {
      for (const headers of [{}, { authorization: `Bearer ${testEnv.EDGE_SECRET}` }] as Record<string, string>[]) {
        const response = await worker.fetch(new Request(`https://edge.test${path}`, {
          method, headers, body: method === "POST" ? DB_BODY : undefined,
        }), testEnv);
        expect(response.status).toBe(404);
        expect(await response.text()).toBe("not found");
      }
    }
  });

  it("a correctly signed request is a 404 too, while OPERATOR_SECRET is unset", async () => {
    expect((await worker.fetch(await signed("/internal/db"), testEnv)).status).toBe(404);
    // ...or too short to be a secret.
    const weak = { ...testEnv, OPERATOR_SECRET: "short" } as typeof testEnv;
    expect((await worker.fetch(await signed("/internal/db", { secret: "short" }), weak)).status).toBe(404);
  });
});

describe("the operator's signed door", () => {
  it("answers a signed request once OPERATOR_SECRET is set", async () => {
    const response = await worker.fetch(await signed("/internal/db"), operatorEnv());
    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({ results: [{ columns: ["one"], rows: [[1]] }] });
  });

  it("refuses the old bearer, an unsigned request, and the wrong key", async () => {
    const bearer = await worker.fetch(new Request("https://edge.test/internal/db", {
      method: "POST", headers: { authorization: `Bearer ${testEnv.EDGE_SECRET}` }, body: DB_BODY,
    }), operatorEnv());
    expect(bearer.status).toBe(401);
    const wrongKey = await worker.fetch(await signed("/internal/db", { secret: "x".repeat(40) }), operatorEnv());
    expect(wrongKey.status).toBe(401);
  });

  it("refuses a replay", async () => {
    const nonce = crypto.randomUUID().replace(/-/g, "");
    expect((await worker.fetch(await signed("/internal/db", { nonce }), operatorEnv())).status).toBe(200);
    expect((await worker.fetch(await signed("/internal/db", { nonce }), operatorEnv())).status).toBe(401);
  });

  it("refuses a request outside the five-minute window", async () => {
    const old = await signed("/internal/db", { time: Date.now() - (SKEW_SECONDS + 5) * 1000 });
    expect((await worker.fetch(old, operatorEnv())).status).toBe(401);
    const future = await signed("/internal/db", { time: Date.now() + (SKEW_SECONDS + 5) * 1000 });
    expect((await worker.fetch(future, operatorEnv())).status).toBe(401);
  });

  it("refuses a signed request whose body or path was changed", async () => {
    const original = await signed("/internal/db");
    const edited = new Request(original.url, {
      method: "POST", headers: original.headers,
      body: JSON.stringify({ statements: [{ sql: "DELETE FROM outbox", params: [] }] }),
    });
    expect((await worker.fetch(edited, operatorEnv())).status).toBe(401);
    const moved = new Request("https://edge.test/internal/send", {
      method: "POST", headers: (await signed("/internal/db")).headers, body: DB_BODY,
    });
    expect((await worker.fetch(moved, operatorEnv())).status).toBe(401);
  });

  it("signs exactly as the application does (tests/test_edge_access.py holds the same vector)", async () => {
    const body = new TextEncoder().encode('{"statements":[]}').buffer as ArrayBuffer;
    expect(await sign(OPERATOR, "POST", "/internal/db", "1790000000", "abcdefabcdefabcdef", body))
      .toBe("3307803425551c43d0a83a8635e1c9e5ab5cb05a59a08334991fe56eac204712");
  });

  it("tells the caller why only in the log", async () => {
    const result = await verifyOperator(new Request("https://edge.test/internal/db", { method: "POST", body: "{}" }),
      OPERATOR, testEnv.DB);
    expect(result).toEqual({ refused: "unsigned" });
  });
});

describe("from the container", () => {
  it("reaches the endpoints through its outbound handler, with EDGE_SECRET", async () => {
    const handler = ReviewContainer.outboundByHost?.[EDGE_HOST];
    expect(handler).toBeTypeOf("function");
    const response = await handler!(new Request(`http://${EDGE_HOST}/internal/db`, {
      method: "POST", headers: { authorization: `Bearer ${testEnv.EDGE_SECRET}` }, body: DB_BODY,
    }), testEnv, { containerId: "c", className: "ReviewContainer" });
    expect(response.status).toBe(200);
  });

  it("is refused without EDGE_SECRET even there", async () => {
    const response = await fromContainer(new Request(`http://${EDGE_HOST}/internal/db`, {
      method: "POST", headers: { authorization: "Bearer wrong" }, body: DB_BODY,
    }), testEnv);
    expect(response.status).toBe(401);
  });

  it("calls the edge by its internal name, not the public URL", () => {
    expect(EDGE_HOST.endsWith(".internal")).toBe(true);
  });

  it("is exported so the runtime can intercept the container's traffic", () => {
    expect(ContainerProxy).toBeTypeOf("function");
  });
});

describe("what the container can reach", () => {
  it("by default, only the edge and Anthropic, with no internet and HTTPS inspected", () => {
    expect(egress(testEnv)).toEqual({
      enableInternet: false, interceptHttps: true, allowedHosts: [EDGE_HOST, ANTHROPIC_HOST],
    });
    expect(egress({ ...testEnv, CONTAINER_EGRESS: "" } as typeof testEnv).enableInternet).toBe(false);
  });

  it("adds the document system's token host only when one is configured", () => {
    const dms = { ...testEnv, DMS_PROVIDER: "imanage", DMS_TOKEN_URL: "https://cloudimanage.com/auth/oauth2/token" };
    expect(egress(dms as typeof testEnv).allowedHosts).toEqual([EDGE_HOST, ANTHROPIC_HOST, "cloudimanage.com"]);
    const none = { ...dms, DMS_PROVIDER: "none" };
    expect(egress(none as typeof testEnv).allowedHosts).toEqual([EDGE_HOST, ANTHROPIC_HOST]);
  });

  it("adds what EGRESS_ALLOWED_HOSTS lists", () => {
    const more = { ...testEnv, EGRESS_ALLOWED_HOSTS: "a.example.com, b.example.com" } as typeof testEnv;
    expect(egress(more).allowedHosts).toEqual([EDGE_HOST, ANTHROPIC_HOST, "a.example.com", "b.example.com"]);
  });

  it("goes back to the open internet only when told to, as a rollback", () => {
    expect(egress({ ...testEnv, CONTAINER_EGRESS: "open" } as typeof testEnv))
      .toEqual({ enableInternet: true, interceptHttps: false });
  });
});
