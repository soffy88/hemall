/**
 * 售后退款状态机 e2e：
 *  - admin：/admin/aftersales 面板加载、状态筛选、刷新、无页面异常；
 *  - customer：/shop/account 我的售后区块；有「申请售后」入口时提交一笔待审核申请。
 *
 * 凭据环境变量（可只给其一；缺的一侧 test.skip）：
 *  - SMOKE_ADMIN_EMAIL / SMOKE_ADMIN_PASSWORD
 *  - SMOKE_CUSTOMER_EMAIL / SMOKE_CUSTOMER_PASSWORD
 * 管理员账号若启用 TOTP 会停在 /login 挑战页（无 code 输入），本用例 skip 不接管。
 */
import { expect, test } from '@playwright/test';

const ADMIN_EMAIL = process.env.SMOKE_ADMIN_EMAIL;
const ADMIN_PASSWORD = process.env.SMOKE_ADMIN_PASSWORD;
const CUSTOMER_EMAIL = process.env.SMOKE_CUSTOMER_EMAIL;
const CUSTOMER_PASSWORD = process.env.SMOKE_CUSTOMER_PASSWORD;

test.describe('售后退款（admin 面板 + customer 申请）', () => {
  test('admin：退款状态机面板可筛选可刷新', async ({ page }) => {
    test.skip(!ADMIN_EMAIL || !ADMIN_PASSWORD, '未提供 SMOKE_ADMIN_EMAIL / SMOKE_ADMIN_PASSWORD');

    const errors: string[] = [];
    page.on('pageerror', (e) => errors.push(String(e)));

    await page.goto('/login');
    await page.locator('input[type="email"]').fill(ADMIN_EMAIL as string);
    await page.locator('input[type="password"]').fill(ADMIN_PASSWORD as string);
    await page.getByRole('button', { name: '登录' }).click();
    await page.waitForURL(/\/admin(\/|$)/, { timeout: 30_000 });
    // 若账号开了 TOTP 会留在 /login 出现挑战，此时不接管
    test.skip(await page.getByText('输入动态验证码完成登录').isVisible().catch(() => false), '管理员账号启用 TOTP，本用例不接管验证码');

    await page.goto('/admin/aftersales');
    await expect(page.getByRole('heading', { name: /退款状态机/ })).toBeVisible({ timeout: 15_000 });

    // 状态筛选：label 无 htmlFor，用同级 select 定位
    const statusSelect = page.locator('label:text-is("状态筛选") + select');
    await expect(statusSelect).toBeVisible();
    await statusSelect.selectOption('submitted');
    await expect(statusSelect).toHaveValue('submitted');

    const table = page.locator('table').filter({ hasText: 'refund_id' }).first();
    await expect(table).toBeVisible();
    // 空态或有数据都行：要么出现空态文案，要么出现操作列按钮
    const emptyOrRows = page.getByText('暂无售后申请').or(table.locator('tbody tr').filter({ hasNotText: '暂无售后申请' }));
    await expect(emptyOrRows.first()).toBeVisible({ timeout: 15_000 });

    // 刷新按钮（loading 时文案为「加载中...」）
    await statusSelect.selectOption('');
    const refresh = page.getByRole('button', { name: /刷新|加载中/ });
    await refresh.click();
    await expect(refresh).toHaveText('刷新', { timeout: 15_000 });

    expect(errors, '不应有未捕获的页面异常').toEqual([]);
  });

  test('customer：我的售后区块与申请售后表单', async ({ page }) => {
    test.skip(!CUSTOMER_EMAIL || !CUSTOMER_PASSWORD, '未提供 SMOKE_CUSTOMER_EMAIL / SMOKE_CUSTOMER_PASSWORD');

    const errors: string[] = [];
    page.on('pageerror', (e) => errors.push(String(e)));

    await page.goto('/shop/login');
    await page.locator('input[type="email"]').fill(CUSTOMER_EMAIL as string);
    await page.locator('input[type="password"]').fill(CUSTOMER_PASSWORD as string);
    await page.getByRole('button', { name: '登录' }).click();
    await page.waitForURL(/\/shop\/account/, { timeout: 30_000 });

    await expect(page.getByRole('heading', { name: '我的售后' })).toBeVisible({ timeout: 15_000 });

    const apply = page.getByRole('button', { name: '申请售后' }).first();
    if (await apply.isVisible().catch(() => false)) {
      await apply.click();

      // 行内表单：金额预填为行小计（非空数字）
      const amount = page.getByLabel('退款金额（元）');
      await expect(amount).toBeVisible();
      await expect(amount).not.toHaveValue('');
      const prefill = await amount.inputValue();
      expect(Number(prefill)).toBeGreaterThan(0);

      await page.locator('select').filter({ hasText: '仅退款' }).first().selectOption('refund_only');
      await page.getByRole('button', { name: '提交售后申请' }).click();

      // 提交成功：行状态变「待审核」或出现 toast 文案
      await expect(
        page.getByText('待审核').first().or(page.getByText('售后申请已提交，等待审核')),
      ).toBeVisible({ timeout: 15_000 });
    } else {
      // 无可用入口（订单行已申请过或没有订单）：至少证明区块渲染了空态或列表
      await expect(
        page.getByText('暂无售后记录').or(page.locator('table, ul, div').filter({ hasText: /待审核|已通过|已退款|已驳回/ })).first(),
      ).toBeVisible();
    }

    expect(errors, '不应有未捕获的页面异常').toEqual([]);
  });
});
