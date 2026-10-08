import { cloudflareTest } from "@cloudflare/vitest-plugin";
import { kCurrentWorker } from "miniflare";
import { defineConfig } from "vitest/config";

// The Worker runs in workerd with the bindings it has in production, except
// two: the container (REVIEW) is a Durable Object that answers the step
// routes from a plan, and the email service (EMAIL) records what is sent
// (test/worker.ts). wrangler.jsonc is not loaded, because its container
// needs Docker to build.
export default defineConfig({
  // dms-mcp/ is a Worker of its own, tested with its own config.
  test: { exclude: ["**/node_modules/**", "dms-mcp/**"] },
  plugins: [
    cloudflareTest({
      main: "./test/worker.ts",
      miniflare: {
        compatibilityDate: "2026-09-01",
        d1Databases: ["DB"],
        r2Buckets: ["DOCS"],
        durableObjects: { REVIEW: "FakeContainer" },
        workflows: { REVIEW_FLOW: { name: "legal-review-flow", className: "ReviewFlow" } },
        serviceBindings: { EMAIL: { name: kCurrentWorker, entrypoint: "FakeEmail" } },
        bindings: {
          EDGE_SECRET: "s3cret",
          EDGE_URL: "https://edge.test",
          DATA_KEY: "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
          MAIL_AGENT_ADDRESS: "review@legal.firm.com",
          MAIL_AGENT_NAME: "Second Eye",
          ALLOWED_SENDERS: "jim@firm.com, firm.com",
          MAX_EMAILS_PER_DAY: "0",
          ANTHROPIC_WEBHOOK_SIGNING_KEY: "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw",
        },
      },
    }),
  ],
});
