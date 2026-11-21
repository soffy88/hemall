/** vitest 配置：只跑纯逻辑单测（src/lib、src/middleware、src/app/api 路由）。
 *
 * 环境用 jsdom：auth-store / customer-auth-store 依赖 document.cookie 与
 * window 事件，middleware 依赖 next/server 的 cookie 读写。测试不触网，
 * 后端交互全部用 fetch stub。
 */
import { defineConfig } from 'vitest/config';
import { fileURLToPath } from 'node:url';

export default defineConfig({
  test: {
    environment: 'jsdom',
    globals: false,
    include: ['src/**/*.test.ts'],
    setupFiles: ['./vitest.setup.ts'],
    restoreMocks: true,
    unstubEnvs: true,
    unstubGlobals: true,
  },
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
});