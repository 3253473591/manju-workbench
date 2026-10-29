import { defineConfig, devices } from "@playwright/test";

/**
 * 冒烟 e2e：真实浏览器 + 真实后端（mock provider），不依赖剧集素材。
 * CI 里由 workflow 先起 uvicorn 与 vite，再跑本配置；本地同理。
 */
export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  expect: { timeout: 10_000 },
  retries: process.env.CI ? 2 : 0,
  workers: 1,
  reporter: process.env.CI ? [["github"], ["line"]] : [["list"]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://127.0.0.1:5173",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    ...devices["Desktop Chrome"],
    // 本地没有下载 chromium 时用 PW_CHANNEL=chrome 复用系统 Chrome；CI 不设，用 workflow 装的 chromium
    channel: process.env.PW_CHANNEL || undefined,
  },
});
