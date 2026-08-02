'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { api } from '@/lib/api-client';
import { formatMoney } from '@/lib/format';
import { getCustomerAuth } from '@/lib/customer-auth-store';
import type { Cart, Address } from '@/types/api';

const emptyAddress: Address = {
  recipient_name: '',
  phone: '',
  address_line1: '',
  address_line2: '',
  city: '',
  region_code: '',
  postal_code: '',
};

export default function CheckoutPage() {
  const router = useRouter();
  const [cart, setCart] = useState<Cart | null>(null);
  const fmt = (cents: number) => formatMoney(cents, cart?.currency);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const [shipping, setShipping] = useState<Address>({ ...emptyAddress });
  const [billing, setBilling] = useState<Address>({ ...emptyAddress });
  const [sameAddress, setSameAddress] = useState(true);

  useEffect(() => {
    const cartId = sessionStorage.getItem('hemall_cart_id');
    if (!cartId) {
      router.push('/shop/cart');
      return;
    }

    async function init() {
      const customerAuth = getCustomerAuth();
      if (customerAuth) {
        // 购物车可能是登录之前创建的，这里补绑一次；同时拉默认地址预填表单。
        await api.setCartCustomer(cartId!, customerAuth.customerId).catch(() => {});
        api.customerMyAddresses().then((addrs) => {
          const def = addrs.find((a) => a.is_default) || addrs[0];
          if (def) {
            const filled: Address = {
              recipient_name: def.recipient_name,
              phone: def.phone,
              address_line1: def.address_line1,
              address_line2: def.address_line2,
              city: def.city,
              region_code: def.region_code,
              postal_code: def.postal_code,
            };
            setShipping(filled);
            setBilling(filled);
          }
        }).catch(() => {});
      }

      try {
        const data = await api.getCart(cartId!);
        if (!data.line_items || data.line_items.length === 0) {
          router.push('/shop/cart');
          return;
        }
        setCart(data);
      } catch (e: any) {
        setError(e.message);
      } finally {
        setLoading(false);
      }
    }

    init();
  }, [router]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!cart) return;
    setError('');
    setSubmitting(true);
    try {
      const result = await api.checkout({
        cart_id: cart.id,
        shipping_address: shipping,
        billing_address: sameAddress ? shipping : billing,
      });
      // Store receipt token and redirect to success
      sessionStorage.setItem('hemall_receipt_token', result.receipt_token);
      sessionStorage.setItem('hemall_order_id', result.order_id);
      sessionStorage.removeItem('hemall_cart_id');
      router.push('/shop/success');
    } catch (e: any) {
      setError(e.message);
    } finally {
      setSubmitting(false);
    }
  }

  if (loading) return <div className="text-center py-12 text-gray-500">加载中...</div>;

  return (
    <div className="max-w-2xl mx-auto">
      <h1 className="text-2xl font-bold mb-6">结账</h1>

      {error && (
        <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded-lg text-sm text-red-700">
          {error}
        </div>
      )}

      <form onSubmit={handleSubmit} className="space-y-6">
        {/* Shipping Address */}
        <div className="bg-white rounded-xl border border-gray-200 p-5">
          <h2 className="font-semibold mb-4">收货地址</h2>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs text-gray-500 mb-1">收件人</label>
              <input
                required
                value={shipping.recipient_name}
                onChange={(e) => setShipping({ ...shipping, recipient_name: e.target.value })}
                className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm"
              />
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">手机号</label>
              <input
                required
                value={shipping.phone}
                onChange={(e) => setShipping({ ...shipping, phone: e.target.value })}
                className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm"
              />
            </div>
            <div className="col-span-2">
              <label className="block text-xs text-gray-500 mb-1">详细地址</label>
              <input
                required
                value={shipping.address_line1}
                onChange={(e) => setShipping({ ...shipping, address_line1: e.target.value })}
                className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm"
              />
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">城市</label>
              <input
                required
                value={shipping.city}
                onChange={(e) => setShipping({ ...shipping, city: e.target.value })}
                className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm"
              />
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">邮编</label>
              <input
                required
                value={shipping.postal_code}
                onChange={(e) => setShipping({ ...shipping, postal_code: e.target.value })}
                className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm"
              />
            </div>
          </div>
        </div>

        {/* Billing Address */}
        <div className="flex items-center gap-2">
          <input
            type="checkbox"
            id="same-address"
            checked={sameAddress}
            onChange={(e) => setSameAddress(e.target.checked)}
          />
          <label htmlFor="same-address" className="text-sm text-gray-600">账单地址同收货地址</label>
        </div>

        {!sameAddress && (
          <div className="bg-white rounded-xl border border-gray-200 p-5">
            <h2 className="font-semibold mb-4">账单地址</h2>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="block text-xs text-gray-500 mb-1">收件人</label>
                <input
                  required
                  value={billing.recipient_name}
                  onChange={(e) => setBilling({ ...billing, recipient_name: e.target.value })}
                  className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm"
                />
              </div>
              <div>
                <label className="block text-xs text-gray-500 mb-1">手机号</label>
                <input
                  required
                  value={billing.phone}
                  onChange={(e) => setBilling({ ...billing, phone: e.target.value })}
                  className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm"
                />
              </div>
              <div className="col-span-2">
                <label className="block text-xs text-gray-500 mb-1">详细地址</label>
                <input
                  required
                  value={billing.address_line1}
                  onChange={(e) => setBilling({ ...billing, address_line1: e.target.value })}
                  className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm"
                />
              </div>
              <div>
                <label className="block text-xs text-gray-500 mb-1">城市</label>
                <input
                  required
                  value={billing.city}
                  onChange={(e) => setBilling({ ...billing, city: e.target.value })}
                  className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm"
                />
              </div>
              <div>
                <label className="block text-xs text-gray-500 mb-1">邮编</label>
                <input
                  required
                  value={billing.postal_code}
                  onChange={(e) => setBilling({ ...billing, postal_code: e.target.value })}
                  className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm"
                />
              </div>
            </div>
          </div>
        )}

        {/* Order Summary */}
        {cart && (
          <div className="bg-white rounded-xl border border-gray-200 p-5">
            <h2 className="font-semibold mb-3">订单摘要</h2>
            <div className="space-y-2 text-sm">
              {cart.line_items.map((item) => (
                <div key={item.id} className="flex justify-between">
                  <span className="text-gray-600">{item.product_title} × {item.quantity}</span>
                  <span>{fmt(item.line_total_cents)}</span>
                </div>
              ))}
              <div className="border-t pt-2 space-y-1">
                <div className="flex justify-between text-gray-500"><span>小计</span><span>{fmt(cart.subtotal_cents)}</span></div>
                <div className="flex justify-between text-gray-500"><span>税费</span><span>{fmt(cart.tax_cents)}</span></div>
                <div className="flex justify-between text-gray-500"><span>运费</span><span>{fmt(cart.shipping_cents)}</span></div>
                <div className="flex justify-between font-bold text-base border-t pt-2">
                  <span>合计</span>
                  <span className="text-emerald-700">{fmt(cart.grand_total_cents)}</span>
                </div>
              </div>
            </div>
          </div>
        )}

        {/* Payment Info */}
        <div className="bg-blue-50 rounded-xl border border-blue-200 p-4 text-sm text-blue-700">
          💳 当前支付方式：手动支付 (manual) — 演示模式，无需实际付款
        </div>

        <button
          type="submit"
          disabled={submitting}
          className="w-full py-3 bg-emerald-600 text-white rounded-xl hover:bg-emerald-700 disabled:opacity-50 transition font-medium text-lg"
        >
          {submitting ? '处理中...' : '确认下单'}
        </button>
      </form>
    </div>
  );
}
