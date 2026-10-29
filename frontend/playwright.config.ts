import { defineConfig } from "@playwright/test";

// Smoke test against a running, disposable Maestro service (see qa/smoke.spec.ts).
export default defineConfig({
  testDir: "./qa",
  testMatch: "*.spec.ts",
  fullyParallel: false,
  timeout: 120_000,
  use: {
    baseURL: process.env.MAESTRO_WEB_URL ?? "http://127.0.0.1:8431",
    headless: true,
    // The tests find elements by their Chinese text; the English UI has its own test.
    locale: "zh-CN",
  },
  reporter: "list",
  outputDir: "/tmp/maestro-web-ui-results",
});
