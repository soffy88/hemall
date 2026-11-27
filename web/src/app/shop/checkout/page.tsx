'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { api } from '@/lib/api-client';
import { formatMoney } from '@/lib/format';
import { getCustomerAuth, initCustomerAuth } from '@/lib/customer-auth-store';
import type { Cart, Address } from '@/types/api';
import { errMessage } from '@/lib/errors';

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
  const [privacyConsent, setPrivacyConsent] = useState(false);
  const [termsConsent, setTermsConsent] = useState(false);
  const [consentVersion, setConsentVersion] = useState('');
  const [siteNotice, setSiteNotice] = useState('');
  const [isPickup, setIsPickup] = useState(false);
  const [paymentOptions, setPaymentOptions] = useState<{
    online_payment_available: boolean;
    manual_payment_enabled: boolean;
    methods: { code: string; name: string; mode: 'online' | 'offline' }[];
  } | null>(null);

  useEffect(() => {
    const cartId = sessionStorage.getItem('hemall_cart_id');
    if (!cartId) {
      router.push('/shop/cart');
      return;
    }

    async function init() {
      // cookie 会话：先拉一次会话再读，避免刷新页面时误判未登录。
      await initCustomerAuth();
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
        const [data, options, compliance] = await Promise.all([
          api.getCart(cartId!),
          api.storePaymentOptions().catch(() => null),
          api.storeComplianceStatus().catch(() => null),
        ]);
        if (!data.line_items || data.line_items.length === 0) {
          router.push('/shop/cart');
          return;
        }
        setCart(data);
        if (data.shipping_method_code === 'pickup') {
          setIsPickup(true);
          setShipping((prev) => ({
            ...prev,
            city: prev.city || '惠州市',
            address_line1: prev.address_line1 || '惠州市仲恺高新区惠环社区店（到店自提）',
            region_code: prev.region_code || 'cn-south',
            postal_code: prev.postal_code || '516000',
          }));
        }
        setPaymentOptions(options);
        if (compliance) {
          setConsentVersion(compliance.consent_version);
          setSiteNotice(compliance.site_notice);
        }
      } catch (e) {
        setError(errMessage(e));
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
    if (!privacyConsent || !termsConsent || !consentVersion) {
      setError('请先阅读并同意隐私政策与用户协议');
      return;
    }
    setSubmitting(true);
    try {
      await initCustomerAuth();
      const customerAuth = getCustomerAuth();
      if (!customerAuth) throw new Error('请先登录顾客账号后再支付');
      const result = await api.checkout({
        cart_id: cart.id,
        shipping_address: shipping,
        billing_address: sameAddress ? shipping : billing,
        customer_id: customerAuth.customerId,
        privacy_consent: privacyConsent,
        terms_consent: termsConsent,
        consent_version: consentVersion,
      });
      // 结账接口只返回待支付 intent；success 页面还要轮询 verified PAID。
      sessionStorage.setItem('hemall_receipt_token', result.receipt_token);
      sessionStorage.setItem('hemall_order_id', result.order_id);
      sessionStorage.setItem('hemall_payment_id', result.payment.payment_id);
      sessionStorage.setItem('hemall_payment', JSON.stringify(result.payment));
      sessionStorage.removeItem('hemall_cart_id');
      router.push('/shop/success');
    } catch (e) {
      setError(errMessage(e));
    } finally {
      setSubmitting(false);
    }
  }

  if (loading) return <div className="text-center py-12 text-gray-500">加载中...</div>;

  return (
    <div className="max-w-2xl mx-auto">
      <h1 className="text-2xl font-bold mb-6">结账</h1>

      {siteNotice && (
        <div className="mb-4 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800">
          {siteNotice}
        </div>
      )}

      {error && (
        <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded-lg text-sm text-red-700">
          {error}
        </div>
      )}

      <form onSubmit={handleSubmit} className="space-y-6">
        {/* Shipping Address */}
        <div className="bg-white rounded-xl border border-gray-200 p-5">
          <h2 className="font-semibold mb-4">{isPickup ? '取货人信息（到店自提）' : '收货地址'}</h2>
          {isPickup && (
            <div className="mb-3 rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-800">
              到店自提：不需要配送地址，请填写取货人姓名与手机号；门店地址为惠州市仲恺高新区惠环社区店。
            </div>
          )}
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
        <div className={`rounded-xl border p-4 text-sm ${
          paymentOptions?.online_payment_available
            ? 'bg-blue-50 border-blue-200 text-blue-700'
            : 'bg-amber-50 border-amber-200 text-amber-800'
        }`}>
          {paymentOptions?.online_payment_available ? (
            <>💳 在线支付已配置：{paymentOptions.methods.find((m) => m.mode === 'online')?.name}。订单金额只由服务端计算，支付成功也必须经过 provider 回调验证。</>
          ) : paymentOptions?.manual_payment_enabled ? (
            <>当前未配置真实在线支付，只支持“人工确认收款”。下单后不会显示支付成功，需工作人员线下确认。</>
          ) : (
            <>当前未配置可用的支付通道，暂不能在线下单支付。</>
          )}
        </div>

        <div className="space-y-2 rounded-xl border border-gray-200 bg-white p-4 text-sm text-gray-600">
          <label className="flex items-start gap-2">
            <input
              type="checkbox"
              checked={privacyConsent}
              onChange={(e) => setPrivacyConsent(e.target.checked)}
              className="mt-0.5"
              required
            />
            <span>
              我已阅读并同意
              <a href="/privacy" target="_blank" className="mx-1 text-emerald-700 underline">
                隐私政策
              </a>
              （版本 {consentVersion || '加载中'}）
            </span>
          </label>
          <label className="flex items-start gap-2">
            <input
              type="checkbox"
              checked={termsConsent}
              onChange={(e) => setTermsConsent(e.target.checked)}
              className="mt-0.5"
              required
            />
            <span>
              我已阅读并同意
              <a href="/terms" target="_blank" className="mx-1 text-emerald-700 underline">
                用户协议
              </a>
            </span>
          </label>
        </div>

        <button
          type="submit"
          disabled={
            submitting
            || !privacyConsent
            || !termsConsent
            || !consentVersion
            || (paymentOptions != null && paymentOptions.methods.length === 0)
          }
          className="w-full py-3 bg-emerald-600 text-white rounded-xl hover:bg-emerald-700 disabled:opacity-50 transition font-medium text-lg"
        >
          {submitting ? '处理中...' : '确认下单'}
        </button>
      </form>
    </div>
  );
}
