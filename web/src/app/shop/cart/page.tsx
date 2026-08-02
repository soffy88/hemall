'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { api } from '@/lib/api-client';
import { formatMoney } from '@/lib/format';
import type { Cart } from '@/types/api';

const SHIPPING_METHODS = [
  { name: '标准配送', price_cents: 0 },
  { name: '快速配送', price_cents: 1500 },
  { name: '次日达', price_cents: 3000 },
];

export default function CartPage() {
  const router = useRouter();
  const [cart, setCart] = useState<Cart | null>(null);
  const fmt = (cents: number) => formatMoney(cents, cart?.currency);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [discountCode, setDiscountCode] = useState('');
  const [giftCardCode, setGiftCardCode] = useState('');
  const [applying, setApplying] = useState(false);

  async function loadCart() {
    const cartId = sessionStorage.getItem('hemall_cart_id');
    if (!cartId) {
      setLoading(false);
      return;
    }
    try {
      const data = await api.getCart(cartId);
      setCart(data);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { loadCart(); }, []);

  async function handleUpdateQty(lineItemId: string, newQty: number) {
    if (!cart) return;
    try {
      if (newQty <= 0) {
        await api.deleteLineItem(cart.id, lineItemId);
      } else {
        await api.updateLineItem(cart.id, lineItemId, newQty);
      }
      loadCart();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleRemove(lineItemId: string) {
    if (!cart) return;
    try {
      await api.deleteLineItem(cart.id, lineItemId);
      loadCart();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleApplyDiscount(e: React.FormEvent) {
    e.preventDefault();
    if (!cart || !discountCode.trim()) return;
    setApplying(true);
    try {
      await api.storeApplyDiscount(cart.id, discountCode.trim());
      setDiscountCode('');
      loadCart();
    } catch (e: any) {
      alert(e.message);
    } finally {
      setApplying(false);
    }
  }

  async function handleRemoveDiscount(discountId: string) {
    if (!cart) return;
    try {
      await api.storeRemoveDiscount(cart.id, discountId);
      loadCart();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleApplyGiftCard(e: React.FormEvent) {
    e.preventDefault();
    if (!cart || !giftCardCode.trim()) return;
    setApplying(true);
    try {
      await api.storeApplyGiftCard(cart.id, giftCardCode.trim());
      setGiftCardCode('');
      loadCart();
    } catch (e: any) {
      alert(e.message);
    } finally {
      setApplying(false);
    }
  }

  async function handleRemoveGiftCard(giftCardId: string) {
    if (!cart) return;
    try {
      await api.storeRemoveGiftCard(cart.id, giftCardId);
      loadCart();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleSelectShipping(methodName: string, priceCents: number) {
    if (!cart) return;
    try {
      await api.storeAddShippingMethod(cart.id, methodName, priceCents);
      loadCart();
    } catch (e: any) {
      alert(e.message);
    }
  }

  if (loading) return <div className="text-center py-12 text-gray-500">加载中...</div>;
  if (error) return <div className="text-center py-12 text-red-600">{error}</div>;

  if (!cart || cart.line_items.length === 0) {
    return (
      <div className="text-center py-12">
        <div className="text-6xl mb-4">🛒</div>
        <h2 className="text-xl font-semibold text-gray-700 mb-2">购物车是空的</h2>
        <Link href="/shop" className="text-emerald-600 hover:underline">去逛逛 →</Link>
      </div>
    );
  }

  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">购物车</h1>
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Cart Items */}
        <div className="lg:col-span-2 space-y-3">
          {cart.line_items.map((item) => (
            <div key={item.id} className="flex items-center gap-4 bg-white rounded-xl p-4 border border-gray-200">
              <div className="w-16 h-16 bg-emerald-50 rounded-lg flex items-center justify-center flex-shrink-0">
                <span className="text-2xl">📦</span>
              </div>
              <div className="flex-1 min-w-0">
                <h3 className="font-medium text-gray-900 truncate">{item.product_title || '—'}</h3>
                <p className="text-xs text-gray-500">
                  {item.variant_sku}
                  {item.option_values && Object.values(item.option_values).length > 0 && (
                    <> · {Object.values(item.option_values).join(' / ')}</>
                  )}
                </p>
                <p className="text-sm text-emerald-700 font-medium mt-1">{fmt(item.unit_price_cents)}</p>
              </div>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => handleUpdateQty(item.id, item.quantity - 1)}
                  className="w-8 h-8 border border-gray-300 rounded-lg flex items-center justify-center text-gray-600 hover:border-gray-400"
                >
                  −
                </button>
                <span className="w-8 text-center font-medium">{item.quantity}</span>
                <button
                  onClick={() => handleUpdateQty(item.id, item.quantity + 1)}
                  className="w-8 h-8 border border-gray-300 rounded-lg flex items-center justify-center text-gray-600 hover:border-gray-400"
                >
                  +
                </button>
              </div>
              <div className="text-right">
                <div className="font-medium">{fmt(item.line_total_cents)}</div>
                <button onClick={() => handleRemove(item.id)} className="text-xs text-red-500 hover:underline mt-1">
                  删除
                </button>
              </div>
            </div>
          ))}

          {/* Shipping Method */}
          <div className="bg-white rounded-xl p-4 border border-gray-200">
            <h3 className="font-medium text-gray-900 mb-3">配送方式</h3>
            <div className="space-y-2">
              {SHIPPING_METHODS.map((m) => (
                <label key={m.name} className="flex items-center justify-between text-sm cursor-pointer">
                  <span className="flex items-center gap-2">
                    <input
                      type="radio"
                      name="shipping-method"
                      checked={cart.shipping_cents === m.price_cents}
                      onChange={() => handleSelectShipping(m.name, m.price_cents)}
                    />
                    {m.name}
                  </span>
                  <span className="text-gray-500">{m.price_cents === 0 ? '免费' : fmt(m.price_cents)}</span>
                </label>
              ))}
            </div>
          </div>

          {/* Discount Code */}
          <div className="bg-white rounded-xl p-4 border border-gray-200">
            <h3 className="font-medium text-gray-900 mb-3">优惠码</h3>
            {cart.discounts.length > 0 && (
              <div className="space-y-1 mb-3">
                {cart.discounts.map((d) => (
                  <div key={d.id} className="flex items-center justify-between text-sm bg-emerald-50 text-emerald-700 rounded-lg px-3 py-2">
                    <span>{d.code} · -{fmt(d.applied_amount_cents)}</span>
                    <button onClick={() => handleRemoveDiscount(d.discount_id)} className="text-xs hover:underline">移除</button>
                  </div>
                ))}
              </div>
            )}
            <form onSubmit={handleApplyDiscount} className="flex gap-2">
              <input
                value={discountCode}
                onChange={(e) => setDiscountCode(e.target.value)}
                placeholder="输入优惠码"
                className="flex-1 px-3 py-2 border border-gray-300 rounded-lg text-sm"
              />
              <button type="submit" disabled={applying || !discountCode.trim()} className="px-4 py-2 bg-gray-900 text-white rounded-lg text-sm hover:bg-gray-800 disabled:opacity-50">
                应用
              </button>
            </form>
          </div>

          {/* Gift Card */}
          <div className="bg-white rounded-xl p-4 border border-gray-200">
            <h3 className="font-medium text-gray-900 mb-3">礼品卡</h3>
            {cart.gift_cards.length > 0 && (
              <div className="space-y-1 mb-3">
                {cart.gift_cards.map((g) => (
                  <div key={g.id} className="flex items-center justify-between text-sm bg-amber-50 text-amber-700 rounded-lg px-3 py-2">
                    <span>{g.code} · -{fmt(g.applied_amount_cents)}</span>
                    <button onClick={() => handleRemoveGiftCard(g.gift_card_id)} className="text-xs hover:underline">移除</button>
                  </div>
                ))}
              </div>
            )}
            <form onSubmit={handleApplyGiftCard} className="flex gap-2">
              <input
                value={giftCardCode}
                onChange={(e) => setGiftCardCode(e.target.value)}
                placeholder="输入礼品卡卡号"
                className="flex-1 px-3 py-2 border border-gray-300 rounded-lg text-sm"
              />
              <button type="submit" disabled={applying || !giftCardCode.trim()} className="px-4 py-2 bg-gray-900 text-white rounded-lg text-sm hover:bg-gray-800 disabled:opacity-50">
                应用
              </button>
            </form>
          </div>
        </div>

        {/* Summary */}
        <div className="bg-white rounded-xl border border-gray-200 p-5 h-fit sticky top-20">
          <h2 className="font-semibold mb-4">订单摘要</h2>
          <div className="space-y-2 text-sm">
            <div className="flex justify-between"><span className="text-gray-500">小计</span><span>{fmt(cart.subtotal_cents)}</span></div>
            <div className="flex justify-between"><span className="text-gray-500">折扣</span><span>-{fmt(cart.discount_cents)}</span></div>
            <div className="flex justify-between"><span className="text-gray-500">税费</span><span>{fmt(cart.tax_cents)}</span></div>
            <div className="flex justify-between"><span className="text-gray-500">运费</span><span>{fmt(cart.shipping_cents)}</span></div>
            <div className="border-t pt-2 flex justify-between font-bold text-base">
              <span>合计</span>
              <span className="text-emerald-700">{fmt(cart.grand_total_cents)}</span>
            </div>
            {cart.gift_cards.length > 0 && (
              <>
                <div className="flex justify-between text-amber-700">
                  <span>礼品卡抵扣</span>
                  <span>-{fmt(cart.gift_cards.reduce((sum, g) => sum + g.applied_amount_cents, 0))}</span>
                </div>
                <div className="flex justify-between font-bold text-base border-t pt-2">
                  <span>应付金额</span>
                  <span className="text-emerald-700">
                    {fmt(Math.max(0, cart.grand_total_cents - cart.gift_cards.reduce((sum, g) => sum + g.applied_amount_cents, 0)))}
                  </span>
                </div>
              </>
            )}
          </div>
          <button
            onClick={() => router.push('/shop/checkout')}
            className="w-full mt-4 py-3 bg-emerald-600 text-white rounded-xl hover:bg-emerald-700 transition font-medium"
          >
            去结账
          </button>
          <Link href="/shop" className="block text-center text-sm text-gray-500 hover:text-emerald-600 mt-3">
            继续购物
          </Link>
        </div>
      </div>
    </div>
  );
}
