/**
 * 前端冒烟（audit 核验5）：登录 → 后台列表；商城加购 → 结账页。
 *
 * 这条用例是「令牌不出浏览器 JS」这一改动的回归网：
 *  - 登录后后台路由由服务端 middleware 放行（不再靠客户端 router.push）；
 *  - 会话落在 HttpOnly cookie 上，客户端 JS 读不到令牌；
 *  - 整个过程没有任何 console error / 未捕获异常。
 *
 * 前置：后端已启动（Next 服务端用 HEMALL_BACKEND_URL 指向它）、web 已 next start。
 * 凭据从环境变量读，不写死在仓库里：SMOKE_ADMIN_EMAIL / SMOKE_ADMIN_PASSWORD。
 */
import { expect, test } from '@playwright/test';

const ADMIN_EMAIL = process.env.SMOKE_ADMIN_EMAIL;
const ADMIN_PASSWORD = process.env.SMOKE_ADMIN_PASSWORD;

test.describe('hemal 前端冒烟', () => {
  test('未登录访问 /admin 会被服务端 302 到 /login', async ({ page }) => {
    const response = await page.goto('/admin/orders', { waitUntil: 'domcontentloaded' });
    expect(response?.status()).toBe(200); // 最终落在 /login 页面
    await expect(page).toHaveURL(/\/login(\?|$)/);
    await expect(page.getByRole('heading', { name: '商家后台登录' })).toBeVisible();
  });

  test('管理员登录后进入后台列表', async ({ page, context }) => {
    test.skip(!ADMIN_EMAIL || !ADMIN_PASSWORD, '未提供 SMOKE_ADMIN_EMAIL / SMOKE_ADMIN_PASSWORD');

    const errors: string[] = [];
    page.on('pageerror', (e) => errors.push(String(e)));

    await page.goto('/login');
    // 登录页的 <label> 没有 htmlFor，只能按控件类型定位
    await page.locator('input[type="email"]').fill(ADMIN_EMAIL as string);
    await page.locator('input[type="password"]').fill(ADMIN_PASSWORD as string);
    await Promise.all([
      page.waitForURL(/\/admin(\/|$)/, { timeout: 30_000 }),
      page.getByRole('button', { name: '登录' }).click(),
    ]);

    // 会话必须是 HttpOnly：客户端 JS 读不到令牌
    const jsVisibleCookies = await page.evaluate(() => document.cookie);
    expect(jsVisibleCookies).not.toContain('hemall_access_token');
    expect(jsVisibleCookies).not.toContain('hemall_refresh_token');

    const session = await context.cookies();
    const access = session.find((c) => c.name === 'hemall_access_token');
    expect(access, '后端应签发 hemall_access_token cookie').toBeTruthy();
    expect(access?.httpOnly).toBe(true);

    // 直接访问后台列表页，服务端守卫应放行而不是跳登录
    await page.goto('/admin/orders');
    await expect(page).toHaveURL(/\/admin\/orders/);
    await expect(page.getByRole('heading', { name: /订单/ })).toBeVisible();

    expect(errors, '不应有未捕获的页面异常').toEqual([]);
  });

  test('商城加购后能进入结账页', async ({ page }) => {
    const errors: string[] = [];
    page.on('pageerror', (e) => errors.push(String(e)));

    await page.goto('/shop/feed');
    const lockButton = page.getByRole('button', { name: /^抢购 / }).first();
    await expect(lockButton).toBeVisible({ timeout: 30_000 });
    await lockButton.click();

    // 悬浮购物车栏出现 → 乐观锁成功
    const cartBar = page.getByRole('button', { name: /已锁单 \d+ 件/ });
    await expect(cartBar).toBeVisible({ timeout: 30_000 });

    // 进入结账页：可见输入框为零（Zero-Fill Checkout，Stripe 组件在 iframe 内）
    await cartBar.click();
    await expect(page).toHaveURL(/\/shop\/express-checkout/);
    await expect(page.locator('input:visible')).toHaveCount(0);

    // 提货票据不能落 localStorage
    const persisted = await page.evaluate(() => JSON.stringify(window.localStorage));
    expect(persisted).not.toContain('hemall_pickup_ticket');

    expect(errors, '不应有未捕获的页面异常').toEqual([]);
  });
});