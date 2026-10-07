/**
 * The document-system MCP server, in workerd, against a fake iManage.
 *
 * What is held here: the official MCP SDK's web-standard transport answers
 * initialize, tools/list and tools/call on Workers; a tool call runs only for
 * a verified (lawyer, matter) binding, only against that matter, and with the
 * lawyer's own token forwarded as X-Auth-Token; a binding that does not verify
 * is refused before anything runs.
 */

import { env } from "cloudflare:workers";
import { describe, expect, it } from "vitest";
import worker, { type Env, mcp } from "../src/index";
import { signCapability, signQuery } from "../src/binding";
import { fakeIManage, GOOD_TOKEN, REFUSED_TOKEN } from "./fake-imanage";

const testEnv = env as unknown as Env;
const KEY = "test-signing-key";
const JIM = "jim@firm.com";
const FUTURE = Math.floor(Date.now() / 1000) + 3600;

async function boundUrl(matter = "FAL-001", lawyer = JIM, exp = FUTURE): Promise<string> {
  const sig = await signQuery(KEY, lawyer, matter, exp);
  const params = new URLSearchParams({ u: lawyer, m: matter, exp: String(exp), sig });
  return `https://dms-mcp.test/mcp?${params}`;
}

let nextId = 1;

function rpc(url: string, method: string, params: unknown = {}, bearer: string | null = GOOD_TOKEN): Request {
  return new Request(url, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      accept: "application/json, text/event-stream",
      ...(bearer ? { authorization: `Bearer ${bearer}` } : {}),
    },
    body: JSON.stringify({ jsonrpc: "2.0", id: nextId++, method, params }),
  });
}

interface CallResult {
  content: { type: string; text: string }[];
  isError?: boolean;
}

async function call(
  url: string,
  name: string,
  args: Record<string, unknown>,
  bearer: string | null = GOOD_TOKEN,
  iManage = fakeIManage(),
): Promise<{ result: CallResult; text: string; calls: ReturnType<typeof fakeIManage>["calls"] }> {
  const response = await mcp(rpc(url, "tools/call", { name, arguments: args }, bearer), testEnv, iManage.fetch);
  expect(response.status).toBe(200);
  const body = (await response.json()) as { result: CallResult };
  return { result: body.result, text: body.result.content.map((c) => c.text).join(""), calls: iManage.calls };
}

describe("the MCP transport on Workers", () => {
  it("answers initialize with the server's name and a tools capability", async () => {
    const response = await mcp(
      rpc(await boundUrl(), "initialize", {
        protocolVersion: "2025-06-18",
        capabilities: {},
        clientInfo: { name: "test", version: "1" },
      }),
      testEnv,
    );
    expect(response.status).toBe(200);
    const body = (await response.json()) as { result: { serverInfo: { name: string }; capabilities: { tools?: unknown } } };
    expect(body.result.serverInfo.name).toBe("lra-document-system");
    expect(body.result.capabilities.tools).toBeDefined();
  });

  it("lists exactly the three read-only tools, with no matter parameter", async () => {
    const response = await mcp(rpc(await boundUrl(), "tools/list"), testEnv);
    const body = (await response.json()) as {
      result: { tools: { name: string; inputSchema: { properties: Record<string, unknown> } }[] };
    };
    const names = body.result.tools.map((t) => t.name).sort();
    expect(names).toEqual(["matter_history", "read_firm_document", "search_firm_documents"]);
    for (const tool of body.result.tools) {
      expect(Object.keys(tool.inputSchema.properties)).not.toContain("matter");
      expect(Object.keys(tool.inputSchema.properties)).not.toContain("matter_id");
    }
  });

  it("lists tools with no binding at all, so a session with no matter does not error", async () => {
    const response = await mcp(rpc("https://dms-mcp.test/mcp", "tools/list", {}, null), testEnv);
    expect(response.status).toBe(200);
  });

  it("serves /health and nothing but /mcp", async () => {
    expect(await (await worker.fetch(new Request("https://dms-mcp.test/health"), testEnv)).text()).toBe("ok");
    expect((await worker.fetch(new Request("https://dms-mcp.test/"), testEnv)).status).toBe(404);
  });
});

describe("the matter binding", () => {
  it("matches the container's HMAC byte for byte", async () => {
    // tests/test_dms_mcp.py holds the same vector.
    expect(await signQuery(KEY, JIM, "FAL-001", 2000000000)).toBe(
      "8f0c86c8e1d04d9fce17d0b8ddef505339f78782b19f4ec2aebf9b0612141bdd",
    );
  });

  it("searches the bound matter as the lawyer, forwarding their token as X-Auth-Token", async () => {
    const { text, calls, result } = await call(await boundUrl(), "search_firm_documents", { query: "liability" });
    expect(result.isError).toBeFalsy();
    expect(text).toContain("Falcon SPA v3 (id=1001");
    // The fake leaks a HER-002 document into every search; it is dropped.
    expect(text).not.toContain("Heron");
    expect(calls).toHaveLength(1);
    expect(calls[0].token).toBe(GOOD_TOKEN);
    expect(new URL(calls[0].url).searchParams.get("custom1")).toBe("FAL-001");
  });

  it("ignores a matter the model puts in the arguments", async () => {
    const { text, calls } = await call(await boundUrl(), "search_firm_documents", {
      query: "",
      matter: "HER-002",
      matter_id: "HER-002",
    });
    expect(text).not.toContain("Heron");
    expect(new URL(calls[0].url).searchParams.get("custom1")).toBe("FAL-001");
  });

  it("will not read a document on another matter, or one refiled from another", async () => {
    for (const docId of ["2001", "3001"]) {
      const { text, calls } = await call(await boundUrl(), "read_firm_document", { doc_id: docId });
      expect(text).toContain("is not on this matter");
      expect(calls.some((c) => c.url.endsWith("/download"))).toBe(false);
    }
  });

  it("reads a document on the matter, and a long one a window at a time", async () => {
    expect((await call(await boundUrl(), "read_firm_document", { doc_id: "1001" })).text).toBe(
      "Limitation of liability: capped at the price.",
    );
    const long = (await call(await boundUrl(), "read_firm_document", { doc_id: "1002" })).text;
    expect(long.startsWith("[Characters 0 to 60000 of 150000.]")).toBe(true);
    expect(long).toContain('Call read_firm_document(doc_id="1002", start=60000)');
  });

  it("lists the matter's history and nothing else's", async () => {
    const { text } = await call(await boundUrl(), "matter_history", {});
    expect(text).toContain("Falcon SPA v3");
    expect(text).not.toContain("Heron");
  });

  it("refuses a tool call with no matter, in a sentence, without calling iManage", async () => {
    const { result, text, calls } = await call("https://dms-mcp.test/mcp", "search_firm_documents", { query: "x" });
    expect(result.isError).toBe(true);
    expect(text).toContain("No matter is attached to this session");
    expect(calls).toHaveLength(0);
  });

  it("refuses outright a URL whose matter was changed after signing", async () => {
    const url = (await boundUrl("FAL-001")).replace("m=FAL-001", "m=HER-002");
    const response = await mcp(rpc(url, "tools/call", { name: "matter_history", arguments: {} }), testEnv);
    expect(response.status).toBe(403);
  });

  it("refuses outright a binding signed for another lawyer's address", async () => {
    const url = (await boundUrl("FAL-001", "jim@firm.com")).replace("u=jim%40firm.com", "u=eve%40firm.com");
    expect((await mcp(rpc(url, "tools/list"), testEnv)).status).toBe(403);
  });

  it("says so when the binding has expired", async () => {
    const { result, text } = await call(await boundUrl("FAL-001", JIM, 1000), "matter_history", {});
    expect(result.isError).toBe(true);
    expect(text).toContain("expired");
  });

  it("says so when no credential came with the request (the vault did not match)", async () => {
    const { result, text, calls } = await call(await boundUrl(), "matter_history", {}, null);
    expect(result.isError).toBe(true);
    expect(text).toContain("No document-system credential");
    expect(calls).toHaveLength(0);
  });

  it("passes iManage's own refusal on in a sentence", async () => {
    const { text } = await call(await boundUrl(), "search_firm_documents", { query: "x" }, REFUSED_TOKEN);
    expect(text).toContain("Could not search: The document system refused that request under your own permissions.");
  });

  it("refuses everything when the server has no signing key", async () => {
    const { result } = await (async () => {
      const response = await mcp(
        rpc(await boundUrl(), "tools/call", { name: "matter_history", arguments: {} }),
        { ...testEnv, DMS_MCP_SIGNING_KEY: "" },
        fakeIManage().fetch,
      );
      return (await response.json()) as { result: CallResult };
    })();
    expect(result.isError).toBe(true);
  });
});

describe("the capability fallback (DMS_MCP_BINDING=capability)", () => {
  it("reads lawyer, matter and iManage token from a signed bearer on the bare URL", async () => {
    const cap = await signCapability(KEY, { u: JIM, m: "FAL-001", exp: FUTURE, t: GOOD_TOKEN });
    const { text, calls } = await call("https://dms-mcp.test/mcp", "search_firm_documents", { query: "liability" }, cap);
    expect(text).toContain("Falcon SPA v3");
    expect(calls[0].token).toBe(GOOD_TOKEN);
    expect(new URL(calls[0].url).searchParams.get("custom1")).toBe("FAL-001");
  });

  it("refuses a capability that was altered or signed with another key", async () => {
    const cap = await signCapability("another-key", { u: JIM, m: "HER-002", exp: FUTURE, t: GOOD_TOKEN });
    const response = await mcp(rpc("https://dms-mcp.test/mcp", "tools/list", {}, cap), testEnv);
    expect(response.status).toBe(403);
  });

  it("says so when the capability has expired", async () => {
    const cap = await signCapability(KEY, { u: JIM, m: "FAL-001", exp: 1000, t: GOOD_TOKEN });
    const { result, text } = await call("https://dms-mcp.test/mcp", "matter_history", {}, cap);
    expect(result.isError).toBe(true);
    expect(text).toContain("expired");
  });
});
