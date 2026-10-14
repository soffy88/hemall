import { defineConfig, devices } from '@playwright/test';

/**
 * 前端冒烟（audit 核验5）。
 *
 * 目标只有一条：验证「同源 BFF + HttpOnly cookie 会话」这条链路在真实浏览器里通。
 * 用 baseURL 指向已构建并 next start 起来的 web（默认 3010），后端地址由
 * HEMALL_BACKEND_URL 在 Next 服务端注入，浏览器侧永远只看到同源 /api。
 *
 *   npm run build && npx next start -p 3010
 *   npx playwright test
 */
export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  reporter: [['list']],
  use: {
    baseURL: process.env.SMOKE_BASE_URL ?? 'http://127.0.0.1:3010',
    trace: 'off',
    video: 'off',
    screenshot: 'off',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});