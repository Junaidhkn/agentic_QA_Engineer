import { defineConfig } from "@playwright/test";

const proxy = process.env.HTTPS_PROXY || process.env.HTTP_PROXY;

export default defineConfig({
  testDir: "./tests",
  timeout: 60_000,
  retries: 1, // a test that passes only on retry is reported as flaky, not passed
  workers: 2,
  reporter: [["json", { outputFile: process.env.PW_REPORT_FILE || "report.json" }]],
  outputDir: process.env.PW_OUTPUT_DIR || "test-results",
  use: {
    baseURL: process.env.BASE_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    extraHTTPHeaders: { "User-Agent": "ReliabilityEngineer-Playwright" },
    ...(proxy ? { proxy: { server: proxy } } : {}),
  },
});
