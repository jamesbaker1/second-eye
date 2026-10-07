/**
 * The firm's document system as an MCP server, for one matter at a time
 * (docs/migration.md, Phase 4).
 *
 * A review session reaches iManage through this Worker instead of through
 * custom tools answered by the container. The session needs no client for
 * it, and the lawyer's iManage tokens live in their Anthropic vault, which
 * refreshes them, rather than in our database.
 *
 *   POST /mcp     MCP Streamable HTTP, stateless, JSON responses. The official
 *                 TypeScript SDK's web-standard transport, which runs in
 *                 workerd, with the Cloudflare JSON Schema validator (Ajv
 *                 compiles schemas with `new Function`, which workerd forbids).
 *   GET  /health  "ok"
 *
 * Every request is checked for its binding first (binding.ts). initialize and
 * tools/list need none: the tool list is not sensitive, and refusing them
 * would turn a session started with no matter into a session.error rather than
 * a sentence the model can pass on. tools/call runs only for a verified
 * binding, and only against its matter.
 *
 * Nothing is stored. Each request builds its server, answers, and is gone.
 * Every tool call is logged with the lawyer and the matter (Workers
 * observability), which is the audit line docs/oauth.md asks for.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { WebStandardStreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/webStandardStreamableHttp.js";
import { CfWorkerJsonSchemaValidator } from "@modelcontextprotocol/sdk/validation/cfworker-provider.js";
import { z } from "zod";
import { type Binding, bindingOf, isRefusal, type Refusal } from "./binding";
import { IManage } from "./imanage";
import { DESCRIPTIONS, Tools, WINDOW } from "./tools";

export interface Env {
  /** Shared with the container (DMS_MCP_SIGNING_KEY there). Unset: every tool call is refused. */
  DMS_MCP_SIGNING_KEY?: string;
  /** The firm's iManage Work host, e.g. https://firm.imanage.work. Unset: every tool call is refused. */
  IMANAGE_BASE_URL?: string;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/health") return new Response("ok");
    if (url.pathname !== "/mcp") return new Response("not found", { status: 404 });
    return mcp(request, env);
  },
};

export async function mcp(request: Request, env: Env, fetcher: typeof fetch = fetch): Promise<Response> {
  const binding = await bindingOf(request, env.DMS_MCP_SIGNING_KEY);
  if (isRefusal(binding) && binding.status === 403) {
    // Somebody changed the URL or the token. Not a sentence for the model.
    console.warn("dms-mcp: refused a request whose binding did not verify", binding.refused);
    return Response.json(
      { jsonrpc: "2.0", error: { code: -32001, message: binding.refused }, id: null },
      { status: 403 },
    );
  }
  const server = build(binding, env, fetcher);
  const transport = new WebStandardStreamableHTTPServerTransport({
    sessionIdGenerator: undefined,
    enableJsonResponse: true,
  });
  await server.connect(transport);
  return transport.handleRequest(request);
}

type Text = { content: { type: "text"; text: string }[]; isError?: boolean };

function text(value: string, isError = false): Text {
  return { content: [{ type: "text", text: value }], ...(isError ? { isError: true } : {}) };
}

function build(binding: Binding | Refusal, env: Env, fetcher: typeof fetch): McpServer {
  const server = new McpServer(
    { name: "lra-document-system", version: "1.0.0" },
    { jsonSchemaValidator: new CfWorkerJsonSchemaValidator() },
  );

  const run = async (tool: string, body: (tools: Tools) => Promise<string>): Promise<Text> => {
    if (isRefusal(binding)) return text(binding.refused, true);
    if (!env.IMANAGE_BASE_URL) return text("The document system is not configured on this server.", true);
    const tools = new Tools(new IManage(env.IMANAGE_BASE_URL, binding.token, fetcher), binding.matter, (name, detail) =>
      console.log(JSON.stringify({ dms_call: name, lawyer: binding.lawyer, matter: binding.matter, ...detail })),
    );
    try {
      return text(await body(tools));
    } catch (e) {
      console.error("dms-mcp:", tool, "failed", String(e));
      return text(`${tool} failed: the document system did not answer (${e instanceof Error ? e.message : e}).`, true);
    }
  };

  server.registerTool(
    "search_firm_documents",
    {
      description: DESCRIPTIONS.search_firm_documents,
      inputSchema: {
        query: z.string().describe("What to look for. Natural language or a distinctive phrase."),
        limit: z.number().optional().describe("How many documents to return."),
      },
      annotations: { readOnlyHint: true },
    },
    ({ query, limit }) => run("search_firm_documents", (t) => t.search(query, limit)),
  );

  server.registerTool(
    "read_firm_document",
    {
      description: DESCRIPTIONS.read_firm_document,
      inputSchema: {
        doc_id: z.string().describe("The id from a search result."),
        start: z.number().optional().describe("First character to read."),
        end: z
          .number()
          .optional()
          .describe(`Last character to read, exclusive. At most ${WINDOW.toLocaleString("en")} characters come back in one call.`),
      },
      annotations: { readOnlyHint: true },
    },
    ({ doc_id, start, end }) => run("read_firm_document", (t) => t.read(doc_id, start, end)),
  );

  server.registerTool(
    "matter_history",
    {
      description: DESCRIPTIONS.matter_history,
      inputSchema: { limit: z.number().optional().describe("How many documents to list.") },
      annotations: { readOnlyHint: true },
    },
    ({ limit }) => run("matter_history", (t) => t.history(limit)),
  );

  return server;
}
