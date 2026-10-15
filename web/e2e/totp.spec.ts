/**
 * TOTP 登录链 e2e：绑定（setup）→ 退出重登走动态码 → setup_token 不落持久化。
 *
 * 三种登录结局都兼容（取决于测试栈环境/账号状态）：
 *  - 生产环境特权账号未绑定 → /login/totp-setup 绑定页：抓 base32 密钥、
 *    用 node:crypto 现场算 HMAC-SHA1 动态码（SHA1/6位/30s/window±1，与 app/auth.py 一致）、
 *    绑定 → 进 /admin；再登出重登，直接算码过挑战。
 *  - 已绑定 TOTP（任意环境，totp_enabled=true）→ 停在 /login 出现验证码表单：
 *    需 SMOKE_TOTP_SECRET（预置密钥，例如用 SQL 给测试管理员开 totp_enabled）。
 *  - 未启用 TOTP → 直接进 /admin，本文件 skip 并提示换账号/开 TOTP。
 *
 * 凭据：SMOKE_TOTP_ADMIN_EMAIL / SMOKE_TOTP_ADMIN_PASSWORD / SMOKE_TOTP_SECRET（挑战路径）。
 */
import { createHmac } from 'node:crypto';
import { expect, test } from '@playwright/test';

const TOTP_EMAIL = process.env.SMOKE_TOTP_ADMIN_EMAIL;
const TOTP_PASSWORD = process.env.SMOKE_TOTP_ADMIN_PASSWORD;
const TOTP_SECRET = process.env.SMOKE_TOTP_SECRET;

const B32 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';

function base32Decode(secret: string): Buffer {
  let bits = '';
  for (const ch of secret.toUpperCase().replace(/=+$/, '')) {
    const idx = B32.indexOf(ch);
    if (idx < 0) throw new Error(`非法 base32 字符: ${ch}`);
    bits += idx.toString(2).padStart(5, '0');
  }
  const bytes: number[] = [];
  for (let i = 0; i + 8 <= bits.length; i += 8) {
    bytes.push(parseInt(bits.slice(i, i + 8), 2));
  }
  return Buffer.from(bytes);
}

/** 与 app/auth.py 的 _totp_generate_code 对齐：SHA1、6 位、30s 时间步。 */
function totpCode(secret: string, stepOffset = 0): string {
  const counter = Math.floor(Date.now() / 1000 / 30) + stepOffset;
  const msg = Buffer.alloc(8);
  msg.writeBigUInt64BE(BigInt(counter));
  const digest = createHmac('sha1', base32Decode(secret)).update(msg).digest();
  const offset = (digest[digest.length - 1] as number) & 0x0f;
  const code =
    (((digest[offset] as number) & 0x7f) << 24) |
    ((digest[offset + 1] as number) << 16) |
    ((digest[offset + 2] as number) << 8) |
    (digest[offset + 3] as number);
  return String(code % 1_000_000).padStart(6, '0');
}

async function fillPasswordAndLogin(page: import('@playwright/test').Page) {
  await page.goto('/login');
  await page.locator('input[type="email"]').fill(TOTP_EMAIL as string);
  await page.locator('input[type="password"]').fill(TOTP_PASSWORD as string);
  await page.getByRole('button', { name: '登录' }).click();
}

test.describe('TOTP 两步验证登录链', () => {
  test('绑定 → 退出重登走动态码 → setup_token 不落持久化', async ({ page }) => {
    test.skip(!TOTP_EMAIL || !TOTP_PASSWORD, '未提供 SMOKE_TOTP_ADMIN_EMAIL / SMOKE_TOTP_ADMIN_PASSWORD');

    const errors: string[] = [];
    page.on('pageerror', (e) => errors.push(String(e)));

    await fillPasswordAndLogin(page);

    // 等三种结局之一出现：direct（无 TOTP）会很快落到 /admin；setup/challenge 停在 /login*。
    let mode: 'setup' | 'challenge' | 'direct';
    await page.waitForURL(/\/admin(\/|$)/, { timeout: 5_000 }).catch(() => {});
    if (await page.getByRole('heading', { name: '绑定两步验证' }).isVisible().catch(() => false)) {
      mode = 'setup';
    } else if (await page.getByText('输入动态验证码完成登录').isVisible().catch(() => false)) {
      mode = 'challenge';
    } else {
      await page.waitForURL(/\/admin(\/|$)/, { timeout: 30_000 });
      mode = 'direct';
    }

    test.skip(mode === 'direct', '该账号未启用 TOTP；请提供已绑定 TOTP 的账号（挑战路径）或在生产模式栈上跑（绑定路径）');

    if (mode === 'challenge') {
      test.skip(!TOTP_SECRET, '挑战路径需要 SMOKE_TOTP_SECRET（与后台账号 totp_secret 一致）');
      const codeInput = page.locator('input[autocomplete="one-time-code"]');
      // 错码应被拒绝且不发会话
      await codeInput.fill('000000');
      await page.getByRole('button', { name: '验证并登录' }).click();
      await expect(page.getByText(/(?:无效|错误|失败|invalid)/i)).toBeVisible({ timeout: 15_000 });
      await expect(page).toHaveURL(/\/login/);

      // 正确码（当前时间步；校验失败则试下一时间步应对齐 window±1）
      await codeInput.fill(totpCode(TOTP_SECRET as string));
      await page.getByRole('button', { name: '验证并登录' }).click();
      await page.waitForURL(/\/admin(\/|$)/, { timeout: 30_000 }).catch(async () => {
        await codeInput.fill(totpCode(TOTP_SECRET as string, 1));
        await page.getByRole('button', { name: '验证并登录' }).click();
        await page.waitForURL(/\/admin(\/|$)/, { timeout: 30_000 });
      });
      await expect(page).toHaveURL(/\/admin(\/|$)/);
    } else {
      // setup：抓页面上的 base32 密钥（select-all 段）
      const secret = (await page.locator('.select-all').first().innerText()).trim();
      expect(secret).toMatch(/^[A-Z2-7]{16,}$/);

      await page.locator('input[autocomplete="one-time-code"]').fill(totpCode(secret));
      await page.getByRole('button', { name: '绑定并验证' }).click();
      await expect(page.getByText(/恢复码/)).toBeVisible({ timeout: 30_000 });
      await page.getByRole('button', { name: '我已保存，进入后台' }).click();
      await page.waitForURL(/\/admin(\/|$)/, { timeout: 30_000 });
      await expect(page).toHaveURL(/\/admin(\/|$)/);

      // 硬刷新 setup 页：pendingSetup 在内存，应回 /login
      await page.goto('/login/totp-setup');
      await expect(page).toHaveURL(/\/login(\?|$)/);

      // 退出（HttpOnly cookie 需走 BFF logout）→ 重登走动态码
      await page.context().clearCookies();
      await fillPasswordAndLogin(page);
      await expect(page.getByText('输入动态验证码完成登录')).toBeVisible({ timeout: 30_000 });
      await page.locator('input[autocomplete="one-time-code"]').fill(totpCode(secret));
      await page.getByRole('button', { name: '验证并登录' }).click();
      await page.waitForURL(/\/admin(\/|$)/, { timeout: 30_000 }).catch(async () => {
        await page.locator('input[autocomplete="one-time-code"]').fill(totpCode(secret, 1));
        await page.getByRole('button', { name: '验证并登录' }).click();
        await page.waitForURL(/\/admin(\/|$)/, { timeout: 30_000 });
      });
      await expect(page).toHaveURL(/\/admin(\/|$)/);
    }

    expect(errors, '不应有未捕获的页面异常').toEqual([]);
  });
});
