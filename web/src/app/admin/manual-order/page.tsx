'use client';

import { useEffect, useState } from 'react';
import { api } from '@/lib/api-client';
import { formatMoney } from '@/lib/format';
import type { Cart, Customer, Product, Region, Address } from '@/types/api';

const emptyAddress: Address = {
  recipient_name: '',
  phone: '',
  address_line1: '',
  address_line2: '',
  city: '',
  region_code: '',
  postal_code: '',
};

/**
 * 管理员代客下单 — 走 /cart/* 和 /checkout/* 的裸 omodul 端点（逐步建购物车 →
 * 逐步走完整支付流程），跟顾客商城端的 /store/checkout 一体化接口是两条独立
 * 路径。适合电话/线下下单场景：草稿订单 (draft order) 跳过真实支付，这个工具
 * 走真实的 create_payment_sessions → authorize → complete 全流程。
 */
export default function ManualOrderPage() {
  const [regions, setRegions] = useState<Region[]>([]);
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [products, setProducts] = useState<Product[]>([]);

  const [regionCode, setRegionCode] = useState('');
  const [currency, setCurrency] = useState('CNY');
  const [cart, setCart] = useState<Cart | null>(null);
  const fmt = (cents: number) => formatMoney(cents, cart?.currency);

  const [customerId, setCustomerId] = useState('');
  const [batchId, setBatchId] = useState('');
  const [qty, setQty] = useState(1);
  const [shipping, setShipping] = useState<Address>({ ...emptyAddress });
  const [billing, setBilling] = useState<Address>({ ...emptyAddress });
  const [sameAddress, setSameAddress] = useState(true);
  const [discountCode, setDiscountCode] = useState('');
  const [giftCardCode, setGiftCardCode] = useState('');
  const [shippingMethod, setShippingMethod] = useState('标准配送');
  const [shippingPrice, setShippingPrice] = useState(0);

  const [orderResult, setOrderResult] = useState<{ order_id: string; grand_total_cents: number; currency: string } | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.storeRegions().then((data) => {
      setRegions(data);
      if (data[0]) { setRegionCode(data[0].code); setCurrency(data[0].currency); }
    }).catch(() => {});
    api.adminListCustomers({ limit: 100 }).then(setCustomers).catch(() => {});
    api.adminListProducts({ limit: 100 }).then(setProducts).catch(() => {});
  }, []);

  async function refreshCart(id: string) {
    try {
      const data = await api.getCart(id);
      setCart(data);
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleCreateCart() {
    setBusy(true);
    try {
      const res = await api.createCart(regionCode, currency);
      await refreshCart(res.cart_id);
    } catch (e: any) {
      alert(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleBindCustomer() {
    if (!cart || !customerId) return;
    setBusy(true);
    try {
      await api.setCartCustomer(cart.id, customerId);
      await refreshCart(cart.id);
    } catch (e: any) {
      alert(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleAddItem() {
    if (!cart || !batchId) return;
    setBusy(true);
    try {
      await api.addLineItem(cart.id, batchId, qty);
      await refreshCart(cart.id);
    } catch (e: any) {
      alert(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleSetAddresses() {
    if (!cart) return;
    setBusy(true);
    try {
      await api.setCartShippingAddress(cart.id, shipping);
      await api.setCartBillingAddress(cart.id, sameAddress ? shipping : billing);
      await refreshCart(cart.id);
    } catch (e: any) {
      alert(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleApplyDiscount() {
    if (!cart || !discountCode.trim()) return;
    setBusy(true);
    try {
      const res = await api.applyDiscountToCart({ cart_id: cart.id, code: discountCode.trim() });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '应用失败');
      setDiscountCode('');
      await refreshCart(cart.id);
    } catch (e: any) {
      alert(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleApplyGiftCard() {
    if (!cart || !giftCardCode.trim()) return;
    setBusy(true);
    try {
      const res = await api.applyGiftCardToCart({ cart_id: cart.id, code: giftCardCode.trim() });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '应用失败');
      setGiftCardCode('');
      await refreshCart(cart.id);
    } catch (e: any) {
      alert(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleSetShippingMethod() {
    if (!cart) return;
    setBusy(true);
    try {
      const res = await api.addShippingMethodToCart({
        cart_id: cart.id,
        method_name: shippingMethod,
        price_cents: shippingPrice,
      });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '设置失败');
      await refreshCart(cart.id);
    } catch (e: any) {
      alert(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleCompletePayment() {
    if (!cart) return;
    setBusy(true);
    try {
      const sessions = await api.createPaymentSessions({ cart_id: cart.id, provider_names: ['manual'] });
      if (sessions.status === 'failed') throw new Error((sessions.error as any)?.message || '建支付会话失败');

      const selected = await api.setPaymentSession({ cart_id: cart.id, provider_name: 'manual' });
      if (selected.status === 'failed') throw new Error((selected.error as any)?.message || '选定支付会话失败');

      const authorized = await api.authorizePaymentForCart({ cart_id: cart.id });
      if (authorized.status === 'failed') throw new Error((authorized.error as any)?.message || '授权支付失败');

      const completed = await api.completeCheckoutRaw({ cart_id: cart.id });
      if (completed.status === 'failed') throw new Error((completed.error as any)?.message || '完成结账失败');

      setOrderResult({
        order_id: String(completed.order_id),
        grand_total_cents: Number(completed.grand_total_cents),
        currency: cart.currency,
      });
    } catch (e: any) {
      alert(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <h1 className="text-2xl font-bold mb-2">管理员代客下单</h1>
      <p className="text-sm text-gray-500 mb-6">
        走 /cart/* + /checkout/* 裸端点的完整支付流程（建购物车 → 加商品 → 优惠/礼品卡/配送 →
        建支付会话 → 授权 → 完成），适合电话/线下下单；跟草稿订单不同，这里真实走支付授权。
      </p>

      {orderResult ? (
        <div className="bg-emerald-50 border border-emerald-200 rounded-xl p-6 text-center">
          <div className="text-emerald-700 font-semibold text-lg mb-2">下单成功</div>
          <div className="text-sm text-gray-600">订单号: <span className="font-mono">{orderResult.order_id}</span></div>
          <div className="text-sm text-gray-600">总额: {formatMoney(orderResult.grand_total_cents, orderResult.currency)}</div>
          <button onClick={() => window.location.reload()} className="mt-4 px-4 py-2 bg-emerald-600 text-white rounded-lg text-sm hover:bg-emerald-700">
            再下一单
          </button>
        </div>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          <div className="lg:col-span-2 space-y-4">
            {/* Step 1: create cart */}
            <div className="bg-white rounded-xl border border-gray-200 p-4">
              <h2 className="font-semibold mb-3">1. 建购物车</h2>
              {!cart ? (
                <div className="flex gap-3 items-end">
                  <div>
                    <label className="text-xs text-gray-500">区域</label>
                    <select
                      value={regionCode}
                      onChange={(e) => {
                        const r = regions.find((x) => x.code === e.target.value);
                        setRegionCode(e.target.value);
                        if (r) setCurrency(r.currency);
                      }}
                      className="px-3 py-2 border rounded-lg text-sm"
                    >
                      {regions.map((r) => <option key={r.code} value={r.code}>{r.name} ({r.currency})</option>)}
                    </select>
                  </div>
                  <button disabled={busy} onClick={handleCreateCart} className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm hover:bg-blue-700 disabled:opacity-50">
                    创建
                  </button>
                </div>
              ) : (
                <div className="text-sm text-gray-500">购物车 <span className="font-mono">{cart.id}</span></div>
              )}
            </div>

            {cart && (
              <>
                {/* Step 2: bind customer */}
                <div className="bg-white rounded-xl border border-gray-200 p-4">
                  <h2 className="font-semibold mb-3">2. 关联客户（可选）</h2>
                  <div className="flex gap-3 items-end">
                    <select value={customerId} onChange={(e) => setCustomerId(e.target.value)} className="flex-1 px-3 py-2 border rounded-lg text-sm">
                      <option value="">访客（不关联）</option>
                      {customers.map((c) => <option key={c.id} value={c.id}>{c.name || c.email} ({c.email})</option>)}
                    </select>
                    <button disabled={busy || !customerId} onClick={handleBindCustomer} className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm hover:bg-blue-700 disabled:opacity-50">
                      关联
                    </button>
                  </div>
                </div>

                {/* Step 3: add items */}
                <div className="bg-white rounded-xl border border-gray-200 p-4">
                  <h2 className="font-semibold mb-3">3. 添加商品</h2>
                  <div className="flex gap-3 items-end flex-wrap">
                    <select value={batchId} onChange={(e) => setBatchId(e.target.value)} className="flex-1 min-w-[240px] px-3 py-2 border rounded-lg text-sm">
                      <option value="">选择批次...</option>
                      {products.flatMap((p) => (p.variants || []).flatMap((v) => (v.batches || []).map((b) => (
                        <option key={b.id} value={b.id}>{p.title} / {v.sku_code} / {b.batch_no}（可售{b.available_qty}, {formatMoney(b.retail_price_cents, b.currency)}）</option>
                      ))))}
                    </select>
                    <input type="number" min={1} value={qty} onChange={(e) => setQty(+e.target.value)} className="w-20 px-3 py-2 border rounded-lg text-sm" />
                    <button disabled={busy || !batchId} onClick={handleAddItem} className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm hover:bg-blue-700 disabled:opacity-50">
                      添加
                    </button>
                  </div>
                </div>

                {/* Step 4: addresses */}
                <div className="bg-white rounded-xl border border-gray-200 p-4">
                  <h2 className="font-semibold mb-3">4. 收货地址</h2>
                  <div className="grid grid-cols-2 gap-3 mb-3">
                    <input placeholder="收件人" value={shipping.recipient_name} onChange={(e) => setShipping({ ...shipping, recipient_name: e.target.value })} className="px-3 py-2 border rounded-lg text-sm" />
                    <input placeholder="电话" value={shipping.phone} onChange={(e) => setShipping({ ...shipping, phone: e.target.value })} className="px-3 py-2 border rounded-lg text-sm" />
                    <input placeholder="详细地址" value={shipping.address_line1} onChange={(e) => setShipping({ ...shipping, address_line1: e.target.value })} className="col-span-2 px-3 py-2 border rounded-lg text-sm" />
                    <input placeholder="城市" value={shipping.city} onChange={(e) => setShipping({ ...shipping, city: e.target.value })} className="px-3 py-2 border rounded-lg text-sm" />
                    <input placeholder="邮编" value={shipping.postal_code} onChange={(e) => setShipping({ ...shipping, postal_code: e.target.value })} className="px-3 py-2 border rounded-lg text-sm" />
                  </div>
                  <button disabled={busy} onClick={handleSetAddresses} className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm hover:bg-blue-700 disabled:opacity-50">
                    保存地址
                  </button>
                </div>

                {/* Step 5: discount / gift card / shipping method */}
                <div className="bg-white rounded-xl border border-gray-200 p-4 space-y-3">
                  <h2 className="font-semibold">5. 优惠 / 礼品卡 / 配送</h2>
                  <div className="flex gap-2">
                    <input placeholder="优惠码" value={discountCode} onChange={(e) => setDiscountCode(e.target.value)} className="flex-1 px-3 py-2 border rounded-lg text-sm" />
                    <button disabled={busy || !discountCode.trim()} onClick={handleApplyDiscount} className="px-4 py-2 bg-gray-900 text-white rounded-lg text-sm hover:bg-gray-800 disabled:opacity-50">应用</button>
                  </div>
                  <div className="flex gap-2">
                    <input placeholder="礼品卡卡号" value={giftCardCode} onChange={(e) => setGiftCardCode(e.target.value)} className="flex-1 px-3 py-2 border rounded-lg text-sm" />
                    <button disabled={busy || !giftCardCode.trim()} onClick={handleApplyGiftCard} className="px-4 py-2 bg-gray-900 text-white rounded-lg text-sm hover:bg-gray-800 disabled:opacity-50">应用</button>
                  </div>
                  <div className="flex gap-2 items-end">
                    <div className="flex-1">
                      <label className="text-xs text-gray-500">配送方式</label>
                      <input value={shippingMethod} onChange={(e) => setShippingMethod(e.target.value)} className="w-full px-3 py-2 border rounded-lg text-sm" />
                    </div>
                    <div>
                      <label className="text-xs text-gray-500">运费（分）</label>
                      <input type="number" value={shippingPrice} onChange={(e) => setShippingPrice(+e.target.value)} className="w-28 px-3 py-2 border rounded-lg text-sm" />
                    </div>
                    <button disabled={busy} onClick={handleSetShippingMethod} className="px-4 py-2 bg-gray-900 text-white rounded-lg text-sm hover:bg-gray-800 disabled:opacity-50">设置</button>
                  </div>
                </div>

                {/* Step 6: payment */}
                <div className="bg-white rounded-xl border border-gray-200 p-4">
                  <h2 className="font-semibold mb-3">6. 建支付会话 → 授权 → 完成结账</h2>
                  <button
                    disabled={busy || !cart.line_items || cart.line_items.length === 0}
                    onClick={handleCompletePayment}
                    className="w-full py-3 bg-emerald-600 text-white rounded-xl hover:bg-emerald-700 disabled:opacity-50 font-medium"
                  >
                    {busy ? '处理中...' : '完成下单'}
                  </button>
                </div>
              </>
            )}
          </div>

          {/* Live cart summary */}
          <div className="lg:col-span-1">
            {cart ? (
              <div className="bg-white rounded-xl border border-gray-200 p-4 space-y-3 sticky top-4">
                <h2 className="font-semibold">购物车状态</h2>
                <div className="text-xs space-y-1">
                  {(cart.line_items || []).map((li) => (
                    <div key={li.id} className="flex justify-between">
                      <span>{li.product_title} {li.variant_sku} × {li.quantity}</span>
                      <span>{fmt(li.line_total_cents)}</span>
                    </div>
                  ))}
                  {(cart.line_items || []).length === 0 && <div className="text-gray-400">暂无商品</div>}
                </div>
                <div className="border-t pt-2 text-sm space-y-1">
                  <div className="flex justify-between"><span className="text-gray-500">小计</span><span>{fmt(cart.subtotal_cents)}</span></div>
                  <div className="flex justify-between"><span className="text-gray-500">折扣</span><span>-{fmt(cart.discount_cents)}</span></div>
                  <div className="flex justify-between"><span className="text-gray-500">运费</span><span>{fmt(cart.shipping_cents)}</span></div>
                  <div className="flex justify-between font-bold border-t pt-1"><span>合计</span><span>{fmt(cart.grand_total_cents)}</span></div>
                </div>
                {cart.discounts.length > 0 && (
                  <div className="text-xs text-emerald-700">{cart.discounts.map((d) => `${d.code} -${fmt(d.applied_amount_cents)}`).join(', ')}</div>
                )}
                {cart.gift_cards.length > 0 && (
                  <div className="text-xs text-amber-700">{cart.gift_cards.map((g) => `${g.code} -${fmt(g.applied_amount_cents)}`).join(', ')}</div>
                )}
              </div>
            ) : (
              <div className="bg-gray-50 rounded-xl border border-dashed border-gray-300 p-8 text-center text-gray-400">
                先创建购物车
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
