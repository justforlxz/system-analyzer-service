import { defineConfig, devices } from "@playwright/test";

const baseURL = "http://127.0.0.1:3000";
const mockApiUrl = "http://127.0.0.1:3110";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  reporter: "list",
  use: {
    baseURL,
    trace: "on-first-retry",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  // E2E 不需要真实后端：先用 fixtures 起一个假 API，再让 Next.js 的同源转发指向它。
  // 服务端渲染与浏览器端请求因此都走同一条路径，测试结果可重复。
  webServer: [
    {
      command: "node e2e/mock-api/server.mjs",
      url: `${mockApiUrl}/api/health`,
      reuseExistingServer: !process.env.CI,
      timeout: 30_000,
    },
    {
      command: "npm run dev",
      url: baseURL,
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
      env: { NEXT_PUBLIC_API_BASE_URL: mockApiUrl },
    },
  ],
});
