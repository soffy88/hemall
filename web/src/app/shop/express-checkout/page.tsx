'use client';

/**
 * /shop/express-checkout — 极致的转化收口 (Phase 9 SPEC §4 Zero-Fill Checkout)
 *
 * 结算页绝对不允许出现任何键盘输入框：
 *   - 取货点: 读取 LBS 绑定的 node_name (FeedView 进入时写入 sessionStorage)
 *   - 提货时间: 静态文案"立即下楼，即可提货"
 *   - 资产抵扣: user_system_balance > 0 时默认勾选"抵扣"
 *   - 生物识别支付: 唯一【确认支付 ¥XX.XX】按钮 → 微信原生支付 (人脸/指纹)
 *
 * 支付成功后签发加密 JWT 提货码 → Service Worker 硬缓存 → 断网可离线核销。
 */

import { useEffect, useMemo, useState } from 'react';
import { useRouter } from 'next/navigation';
import { api } from '@/lib/api-client';
import { useOptimisticCart, haptic } from '@/lib/cart-store';
import { getCustomerAuth } from '@/lib/customer-auth-store';
import type { PickupTicket } from '@/types/api';

interface CheckoutContext {
  nodeName: string;
  distanceMeters: number | null;
  balanceCents: number;
}

function loadContext(): CheckoutContext {
  if (typeof window === 'undefined') return { nodeName: '附近节点', distanceMeters: null, balanceCents: 0 };
  try {
    const raw = window.sessionStorage.getItem('hemall_feed_context');
    if (raw) return JSON.parse(raw) as CheckoutContext;
  } catch { /* ignore */ }
  return { nodeName: '附近节点', distanceMeters: null, balanceCents: 0 };
}

export default function ExpressCheckoutPage() {
  const router = useRouter();
  const cart = useOptimisticCart();
  const ctx = useMemo(loadContext, []);
  const [paying, setPaying] = useState(false);
  const [ticket, setTicket] = useState<PickupTicket | null>(null);
  const [error, setError] = useState('');

  const lockedItems = cart.items.filter((i) => i.status === 'locked');

  // 资产抵扣: balance > 0 时默认勾选 (SPEC §4.1)
  const applyBalance = ctx.balanceCents > 0;
  const subtotalCents = lockedItems.reduce((s, i) => s + i.retail_price * i.qty, 0);
  const discountCents = applyBalance ? Math.min(ctx.balanceCents, subtotalCents) : 0;
  const payCents = Math.max(0, subtotalCents - discountCents);

  // 空购物车守卫
  useEffect(() => {
    if (cart.lockedCount === 0 && !ticket) {
      router.replace('/shop/feed');
    }
  }, [cart.lockedCount, ticket, router]);

  async function handlePay() {
    if (paying) return;
    setPaying(true);
    setError('');
    haptic('tap');
    try {
      // 1. 建草稿订单 (锁定批次 → line items)
      const cust = getCustomerAuth();
      const draft = await api.createDraftOrder({
        customer_id: cust?.customerId,
        region_code: 'cn-east',
        currency: 'CNY',
        line_items: lockedItems.map((i) => ({ batch_id: i.batch_id, quantity: i.qty })),
      });
      const orderId = (draft as any).order_id ?? (draft as any).id;

      // 2. 标记已支付 (manual provider = 模拟微信原生支付成功回调)
      await api.markDraftOrderPaid({
        order_id: orderId,
        payment_provider_name: 'manual',
        payment_intent_id: `wechat-native-${Date.now()}`,
      });

      // 3. 签发离线核销提货码
      const ticketRes = await api.issuePickupTicket(orderId);

      // 4. Service Worker 硬缓存 (断网可离线渲染)
      if (navigator.serviceWorker?.controller) {
        navigator.serviceWorker.controller.postMessage({
          type: 'CACHE_PICKUP_TICKET',
          payload: ticketRes,
        });
      }
      // 同时兜底写 localStorage
      localStorage.setItem(
        'hemall_pickup_ticket',
        JSON.stringify({ ...ticketRes, issued_at: new Date().toISOString() }),
      );

      setTicket(ticketRes);
      cart.clear();
    } catch (e: any) {
      haptic('fail');
      setError(e.message || '支付失败，请重试');
    } finally {
      setPaying(false);
    }
  }

  // ── 支付成功 → 全屏核销凭证视图 ──
  if (ticket) {
    return (
      <main className="flex min-h-screen flex-col items-center justify-center bg-emerald-50 px-6">
        <div className="mb-3 text-5xl">✅</div>
        <h1 className="mb-1 text-2xl font-black text-gray-800">支付成功</h1>
        <p className="mb-6 text-gray-500">立即下楼，即可提货</p>
        <div className="w-full max-w-sm rounded-3xl bg-white p-6 shadow-xl">
          <div className="mb-2 flex justify-between text-sm text-gray-500">
            <span>取货点</span>
            <span className="font-bold text-gray-700">{ticket.node_name}</span>
          </div>
          <div className="mb-2 flex justify-between text-sm text-gray-500">
            <span>实付金额</span>
            <span className="font-black text-emerald-700">¥{(ticket.grand_total_cents / 100).toFixed(2)}</span>
          </div>
          <div className="mb-4 flex justify-between text-sm text-gray-500">
            <span>凭证有效期</span>
            <span className="font-semibold text-gray-700">
              {new Date(ticket.expires_at).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })}
            </span>
          </div>
          {/* 离线核销码: 最大亮度渲染，扫码枪反向读取 */}
          <div
            className="rounded-2xl bg-white p-4 text-center font-black tracking-[0.15em] text-black"
            style={{
              fontSize: 22,
              fontWeight: 800,
              filter: 'brightness(1)',
              border: '2px dashed #059669',
            }}
          >
            {ticket.pickup_code}
          </div>
          <p className="mt-3 text-center text-xs text-gray-400">
            断网也能核销 — 凭证已缓存到本机
          </p>
        </div>
        <button
          onClick={() => router.replace('/shop/feed')}
          className="mt-6 rounded-2xl bg-gray-900 px-8 py-3.5 text-base font-bold text-white active:scale-95"
        >
          返回商城
        </button>
      </main>
    );
  }

  return (
    <main className="mx-auto min-h-screen max-w-md bg-gray-50 pb-28">
      {/* 履约推导: 取货点 + 静态提货时间 */}
      <header className="bg-emerald-800 px-4 py-5 text-white">
        <h1 className="text-xl font-black">确认订单</h1>
        <div className="mt-3 rounded-2xl bg-white/10 px-4 py-3">
          <div className="flex items-center justify-between">
            <span className="text-sm text-emerald-100">取货点</span>
            <span className="font-bold">📍 {ctx.nodeName}</span>
          </div>
          {ctx.distanceMeters != null && (
            <div className="mt-1 flex items-center justify-between">
              <span className="text-sm text-emerald-100">距离</span>
              <span className="font-semibold">距你 {ctx.distanceMeters}m</span>
            </div>
          )}
          <div className="mt-1 flex items-center justify-between">
            <span className="text-sm text-emerald-100">提货时间</span>
            <span className="font-bold text-amber-300">立即下楼，即可提货</span>
          </div>
        </div>
      </header>

      {/* 已锁单商品清单 (无输入框) */}
      <section className="px-4 pt-4">
        <h2 className="mb-2 text-base font-extrabold text-gray-700">已锁单商品</h2>
        <div className="space-y-2">
          {lockedItems.map((i) => (
            <div key={i.batch_id} className="flex items-center justify-between rounded-2xl bg-white p-3.5 shadow-sm">
              <div>
                <div className="font-bold text-gray-800">{i.sku_name}</div>
                <div className="text-xs text-gray-400">×{i.qty}</div>
              </div>
              <div className="font-black text-emerald-700" style={{ fontSize: 24, fontWeight: 800 }}>
                ¥{((i.retail_price * i.qty) / 100).toFixed(2)}
              </div>
            </div>
          ))}
        </div>
      </section>

      {/* 资产抵扣 (自动勾选) */}
      {ctx.balanceCents > 0 && (
        <section className="px-4 pt-3">
          <div className="flex items-center justify-between rounded-2xl bg-emerald-50 p-3.5">
            <div className="flex items-center gap-2">
              <span className="flex h-6 w-6 items-center justify-center rounded-full bg-emerald-600 text-sm text-white">✓</span>
              <span className="font-semibold text-emerald-800">账户资产抵扣</span>
            </div>
            <span className="font-black text-emerald-700" style={{ fontSize: 24, fontWeight: 800 }}>
              -¥{(discountCents / 100).toFixed(2)}
            </span>
          </div>
        </section>
      )}

      {error && (
        <div className="px-4 pt-3">
          <div className="rounded-2xl bg-red-50 p-3 text-center text-sm font-bold text-red-600">
            ❌ {error}
          </div>
        </div>
      )}

      {/* 生物识别支付唯一按钮 */}
      <div className="fixed inset-x-0 bottom-0 z-40 px-3 pb-3">
        <div className="rounded-3xl bg-white p-4 shadow-2xl">
          <div className="mb-3 flex items-center justify-between">
            <span className="text-sm text-gray-500">
              应付 <span className="text-xs text-gray-400">(已含抵扣)</span>
            </span>
            <span className="font-black text-gray-900" style={{ fontSize: 32, fontWeight: 800 }}>
              ¥{(payCents / 100).toFixed(2)}
            </span>
          </div>
          <button
            type="button"
            disabled={paying || lockedItems.length === 0}
            onClick={handlePay}
            className="w-full rounded-2xl bg-emerald-600 py-4 text-white shadow-lg transition-transform active:scale-[0.99] disabled:opacity-50"
          >
            <span className="block text-lg font-black" style={{ fontSize: 24, fontWeight: 800 }}>
              {paying ? '正在拉起微信支付...' : '确认支付'}
            </span>
            <span className="block text-xs font-semibold text-emerald-100">
              {paying ? '指纹/人脸识别中' : '微信原生支付 · 人脸/指纹'}
            </span>
          </button>
        </div>
      </div>
    </main>
  );
}
