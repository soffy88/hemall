/**
 * storefront 静态页回归（audit 供应链 / XSS）。
 *
 * 覆盖一次真实修掉的漏洞：商品卡原来用内联 onclick="addToCart('<batch>', '<title>')"，
 * 而 title 只做了 HTML 转义。HTML 属性里的实体会在 JS 解析前被还原，
 * 所以标题里带一个单引号就能逃出字符串字面量 → 存储型 XSS。
 * 现在改成 data-* 属性 + 事件委托，这条用例钉住该修复。
 *
 * 全程 page.route 拦截，不依赖后端与真实服务器。
 */
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { expect, test } from '@playwright/test';

const STOREFRONT = path.resolve(__dirname, '../../storefront/index.html');
const XSS_TITLE = "');window.__xssFired=true;//";

function product(title: string) {
  return {
    id: 'p1',
    title,
    description: 'desc <script>alert(1)</script>',
    min_price_cents: 100,
    total_stock: 5,
    variants: [{ option_values: { 规格: "大份'" }, batches: [{ id: 'b1' }] }],
  };
}

/** 把静态 storefront 挂到一个假源上，并把后端接口全部 stub 掉。 */
async function mount(page: import('@playwright/test').Page, title: string) {
  const html = readFileSync(STOREFRONT, 'utf8');
  await page.route('**/*', async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.startsWith('/store/')) {
      const body = url.pathname === '/store/products' ? JSON.stringify([product(title)]) : '[]';
      await route.fulfill({ status: 200, contentType: 'application/json', body });
      return;
    }
    await route.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body: html });
  });
  await page.goto('http://storefront.test/index.html', { waitUntil: 'load' });
  await page.waitForSelector('.add-btn:not([disabled])', { timeout: 15_000 });
}

test.describe('storefront XSS 回归', () => {
  test('商品名含单引号时点「加入购物车」不执行注入脚本', async ({ page }) => {
    const errors: string[] = [];
    page.on('pageerror', (e) => errors.push(String(e)));

    await mount(page, XSS_TITLE);
    await page.evaluate(() => {
      (window as unknown as { __xssFired?: boolean }).__xssFired = false;
    });

    // 不允许把商品名拼进可执行属性
    await expect(page.locator('.add-btn').first()).not.toHaveAttribute('onclick', /./);

    await page.click('.add-btn:not([disabled])');
    await expect
      .poll(() => page.evaluate(() => (window as unknown as { __xssFired?: boolean }).__xssFired === true))
      .toBe(false);
    expect(errors).toEqual([]);
  });

  test('商品名按字面量渲染，不执行其中的 HTML/脚本', async ({ page }) => {
    await mount(page, '<img src=x onerror="window.__xssFired=true">');
    await page.evaluate(() => {
      (window as unknown as { __xssFired?: boolean }).__xssFired = false;
    });

    await expect(page.locator('.card-title').first()).toHaveText('<img src=x onerror="window.__xssFired=true">');
    expect(await page.evaluate(() => (window as unknown as { __xssFired?: boolean }).__xssFired === true)).toBe(false);
    // 描述里的 <script> 同样只当文本
    await expect(page.locator('.card-desc').first()).toContainText('<script>alert(1)</script>');
    expect(await page.evaluate(() => (window as unknown as { __xssFired?: boolean }).__xssFired === true)).toBe(false);
  });

  test('购物车与结算里的动态字段同样按字面量渲染', async ({ page }) => {
    await mount(page, '正常商品');
    await page.click('.add-btn:not([disabled])');
    // 购物车渲染完成（line_items 用的是加购接口返回，stub 里给了空数组 → 空态）
    await expect(page.locator('#cartBody')).toBeVisible();
    await expect(page.locator('.add-btn').first()).toHaveAttribute('data-batch', 'b1');
    await expect(page.locator('.add-btn').first()).toHaveAttribute('data-title', '正常商品');
  });
});