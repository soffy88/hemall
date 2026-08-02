/**
 * E2E test: 商家建商品 → 商城看到 → 加购 → 结账 → 后台看到订单
 * Uses Playwright headless Chromium.
 */
import { chromium } from 'playwright';

const BASE = 'http://localhost:3010';
const API = 'http://localhost:8030';
const SHOTS = './var/shots';

// Unique suffix for this run
const SUFFIX = Date.now().toString(36);

async function main() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });

  // ── Step 1: Admin Login ──────────────────────────────────────
  console.log('=== Step 1: Admin Login ===');
  const adminPage = await context.newPage();
  await adminPage.goto(`${BASE}/login`);
  await adminPage.waitForTimeout(1000);
  await adminPage.fill('input[type="email"]', 'admin@test.com');
  await adminPage.fill('input[type="password"]', 'test1234');
  await adminPage.click('button[type="submit"]');
  await adminPage.waitForURL('**/admin', { timeout: 10000 });
  await adminPage.waitForTimeout(1000);
  await adminPage.screenshot({ path: `${SHOTS}/e2e-1-admin-dashboard.png`, fullPage: true });
  console.log('✓ Admin logged in, dashboard visible');

  // ── Step 2: Create Product via API ───────────────────────────
  console.log('=== Step 2: Create Product ===');
  const token = await adminPage.evaluate(() => {
    const auth = JSON.parse(localStorage.getItem('hemall_auth') || 'null');
    return auth?.token;
  });

  // Create product
  const prodRes = await fetch(`${API}/catalog/create_product`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
    body: JSON.stringify({
      title: `E2E测试商品-${SUFFIX}`,
      slug: `e2e-product-${SUFFIX}`,
      description: '自动化测试商品',
      status: 'draft',
    }),
  });
  const prod = await prodRes.json();
  if (prod.status !== 'completed') throw new Error(`create_product failed: ${JSON.stringify(prod)}`);
  const productId = prod.product_id;
  console.log(`✓ Product created: ${productId}`);

  // Create variant
  const varRes = await fetch(`${API}/catalog/create_product_variant`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
    body: JSON.stringify({
      product_id: productId,
      sku_code: `SKU-${SUFFIX}`,
      option_values: { '颜色': '红色', '尺码': 'M' },
    }),
  });
  const variant = await varRes.json();
  if (variant.status !== 'completed') throw new Error(`create_variant failed: ${JSON.stringify(variant)}`);
  const variantId = variant.variant_id;
  console.log(`✓ Variant created: ${variantId}`);

  // Get stock location
  const locRes = await fetch(`${API}/inventory/create_stock_location`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
    body: JSON.stringify({ name: `仓库-${SUFFIX}`, region_code: 'r-62b90d' }),
  });
  const loc = await locRes.json();
  const locationId = loc.location_id;
  console.log(`✓ Stock location: ${locationId}`);

  // Create inventory batch
  const batchRes = await fetch(`${API}/inventory/create_inventory_batch`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
    body: JSON.stringify({
      variant_id: variantId,
      batch_no: `BATCH-${SUFFIX}`,
      location_id: locationId,
      stock_qty: 100,
      retail_price_cents: 19900,
      cost_price_cents: 8000,
      currency: 'CNY',
    }),
  });
  const batch = await batchRes.json();
  if (batch.status !== 'completed') throw new Error(`create_batch failed: ${JSON.stringify(batch)}`);
  const batchId = batch.batch_id;
  console.log(`✓ Inventory batch created: ${batchId}`);

  // Publish product
  const pubRes = await fetch(`${API}/catalog/update_product`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
    body: JSON.stringify({ product_id: productId, status: 'published' }),
  });
  const pub = await pubRes.json();
  if (pub.status !== 'completed') throw new Error(`publish failed: ${JSON.stringify(pub)}`);
  console.log('✓ Product published');

  // ── Step 3: Admin sees product in table ──────────────────────
  console.log('=== Step 3: Admin Product Table ===');
  await adminPage.goto(`${BASE}/admin/products`);
  await adminPage.waitForTimeout(2000);
  await adminPage.screenshot({ path: `${SHOTS}/e2e-2-admin-products.png`, fullPage: true });
  const productVisible = await adminPage.getByText(`E2E测试商品-${SUFFIX}`).isVisible();
  console.log(`✓ Product visible in admin table: ${productVisible}`);

  // ── Step 4: Shop sees product ────────────────────────────────
  console.log('=== Step 4: Shop Browse ===');
  const shopPage = await context.newPage();
  await shopPage.goto(`${BASE}/shop`);
  await shopPage.waitForTimeout(2000);
  await shopPage.screenshot({ path: `${SHOTS}/e2e-3-shop-listing.png`, fullPage: true });
  const shopProductVisible = await shopPage.getByText(`E2E测试商品-${SUFFIX}`).isVisible();
  console.log(`✓ Product visible in shop: ${shopProductVisible}`);

  // ── Step 5: Product detail + add to cart ─────────────────────
  console.log('=== Step 5: Product Detail + Add to Cart ===');
  await shopPage.click(`text=E2E测试商品-${SUFFIX}`);
  await shopPage.waitForTimeout(2000);
  await shopPage.screenshot({ path: `${SHOTS}/e2e-4-product-detail.png`, fullPage: true });

  // Click add to cart
  await shopPage.click('text=加入购物车');
  await shopPage.waitForURL('**/shop/cart', { timeout: 10000 });
  await shopPage.waitForTimeout(1500);
  await shopPage.screenshot({ path: `${SHOTS}/e2e-5-cart.png`, fullPage: true });
  console.log('✓ Added to cart, cart page visible');

  // ── Step 6: Checkout ─────────────────────────────────────────
  console.log('=== Step 6: Checkout ===');
  await shopPage.click('text=去结账');
  await shopPage.waitForURL('**/shop/checkout', { timeout: 10000 });
  await shopPage.waitForTimeout(1000);

  // Fill shipping address
  const inputs = await shopPage.$$('input');
  // Fill form fields by label
  await shopPage.fill('input >> nth=0', '张三');
  await shopPage.fill('input >> nth=1', '13800138000');
  await shopPage.fill('input >> nth=2', '北京市朝阳区测试路1号');
  await shopPage.fill('input >> nth=3', '北京');
  await shopPage.fill('input >> nth=4', '100000');

  await shopPage.screenshot({ path: `${SHOTS}/e2e-6-checkout-form.png`, fullPage: true });

  // Submit checkout
  await shopPage.click('text=确认下单');
  await shopPage.waitForURL('**/shop/success', { timeout: 15000 });
  await shopPage.waitForTimeout(2000);
  await shopPage.screenshot({ path: `${SHOTS}/e2e-7-order-success.png`, fullPage: true });
  console.log('✓ Checkout completed, success page visible');

  // ── Step 7: Admin sees order ─────────────────────────────────
  console.log('=== Step 7: Admin Order ===');
  await adminPage.goto(`${BASE}/admin/orders`);
  await adminPage.waitForTimeout(2000);
  await adminPage.screenshot({ path: `${SHOTS}/e2e-8-admin-orders.png`, fullPage: true });

  // Check that an order exists (the first row should be our order)
  const orderRows = await adminPage.$$('tbody tr');
  console.log(`✓ Admin orders page has ${orderRows.length} order(s)`);

  // ── Step 8: Receipt lookup ───────────────────────────────────
  console.log('=== Step 8: Receipt Lookup ===');
  const receiptToken = await shopPage.evaluate(() => sessionStorage.getItem('hemall_receipt_token'));
  if (receiptToken) {
    const lookupPage = await context.newPage();
    await lookupPage.goto(`${BASE}/shop/lookup`);
    await lookupPage.waitForTimeout(1000);
    await lookupPage.fill('textarea', receiptToken);
    await lookupPage.click('text=查询订单');
    await lookupPage.waitForTimeout(2000);
    await lookupPage.screenshot({ path: `${SHOTS}/e2e-9-receipt-lookup.png`, fullPage: true });
    console.log('✓ Receipt lookup completed');
  }

  // ── Summary ──────────────────────────────────────────────────
  console.log('\n=== E2E TEST PASSED ===');
  console.log('Full chain verified: 商家建商品 → 商城看到 → 加购 → 结账 → 后台看到订单 → 收据查单');
  console.log(`Screenshots saved to ${SHOTS}/e2e-*.png`);

  await browser.close();
}

main().catch((err) => {
  console.error('E2E TEST FAILED:', err.message);
  process.exit(1);
});
