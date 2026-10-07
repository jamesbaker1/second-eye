import { cloudflareTest } from "@cloudflare/vitest-plugin";
import { defineConfig } from "vitest/config";

// The MCP server runs in workerd, as in production. iManage is a fake the
// tests hand to `mcp()` as its fetch (test/fake-imanage.ts); nothing leaves
// the machine.
export default defineConfig({
  plugins: [
    cloudflareTest({
      main: "./src/index.ts",
      miniflare: {
        compatibilityDate: "2026-09-01",
        bindings: {
          DMS_MCP_SIGNING_KEY: "test-signing-key",
          IMANAGE_BASE_URL: "https://imanage.test",
        },
      },
    }),
  ],
});
