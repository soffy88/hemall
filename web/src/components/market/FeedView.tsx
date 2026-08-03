'use client';

/**
 * <FeedView> — BFF v9.0 做市 Feed 编排器 (Phase 9 SPEC §1)
 *
 * 根据 location_context 渲染顶部资产外显，根据 tag_type 在流 (HeroCard)
 * 和网格 (GridItem) 之间自动切换渲染引擎，底部悬浮全局倒计时购物车栏。
 */

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { api } from '@/lib/api-client';
import { useOptimisticCart, formatCountdown, haptic } from '@/lib/cart-store';
import { getCustomerAuth } from '@/lib/customer-auth-store';
import type { FeedItem, NearbyFeedResponse } from '@/types/api';
import MarketMakerHeroCard from './MarketMakerHeroCard';
import StandardGridItem from './StandardGridItem';

export default function FeedView() {
  const router = useRouter();
  const [feed, setFeed] = useState<NearbyFeedResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [toast, setToast] = useState('');

  const cart = useOptimisticCart();

  async function load() {
    setLoading(true);
    setError('');
    try {
      // 浏览器定位 → 失败回退北京
      let lat = 39.9042;
      let lon = 116.4074;
      if (navigator.geolocation) {
        try {
          const pos = await new Promise<GeolocationPosition>((resolve, reject) => {
            navigator.geolocation.getCurrentPosition(resolve, reject, { timeout: 3000 });
          });
          lat = pos.coords.latitude;
          lon = pos.coords.longitude;
        } catch {
          /* 定位被拒 → 默认坐标 */
        }
      }
      const cust = getCustomerAuth();
      const data = await api.getNearbyFeed(lat, lon, { limit: 40, customerId: cust?.customerId });
      setFeed(data);
    } catch (e: any) {
      setError(e.message || 'Feed 加载失败');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleLock(item: FeedItem) {
    try {
      await cart.lockItem(item);
      setToast(`✅ 已锁单: ${item.sku_name}`);
      setTimeout(() => setToast(''), 2200);
    } catch (e: any) {
      haptic('fail');
      setToast(`❌ ${e.message}`);
      setTimeout(() => setToast(''), 3200);
    }
  }

  const heroItems = feed?.feed_items.filter(
    (i) => i.tag_type === 'clearance' || i.tag_type === 'fresh',
  ) ?? [];
  const gridItems = feed?.feed_items.filter((i) => i.tag_type === 'standard') ?? [];

  if (loading) {
    return (
      <div className="flex min-h-[60vh] items-center justify-center">
        <div className="text-center">
          <div className="mx-auto mb-3 h-10 w-10 animate-spin rounded-full border-4 border-emerald-600 border-t-transparent" />
          <p className="text-gray-500">正在扫描附近节点...</p>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex min-h-[60vh] items-center justify-center px-6">
        <div className="text-center">
          <div className="mb-3 text-5xl">📡</div>
          <p className="mb-4 text-gray-600">{error}</p>
          <button
            onClick={load}
            className="rounded-xl bg-emerald-600 px-6 py-3 text-white font-semibold active:scale-95"
          >
            重试
          </button>
        </div>
      </div>
    );
  }

  if (!feed || feed.feed_items.length === 0) {
    return (
      <div className="flex min-h-[60vh] items-center justify-center px-6">
        <div className="text-center">
          <div className="mb-3 text-5xl">🏪</div>
          <p className="mb-1 text-lg font-semibold text-gray-700">附近节点暂无商品</p>
          <p className="text-sm text-gray-500">稍后再来看看，新批次正在补货</p>
        </div>
      </div>
    );
  }

  const balanceYuan = (feed.location_context.user_system_balance ?? 0) / 100;

  return (
    <div className="pb-28">
      {/* 顶部资产外显 (location_context) */}
      <header className="sticky top-0 z-20 bg-gradient-to-b from-emerald-800 to-emerald-700 text-white shadow-lg">
        <div className="flex items-center justify-between px-4 py-3">
          <div>
            <div className="text-sm font-semibold text-emerald-100/90">
              📍 {feed.location_context.node_name ?? '附近节点'}
              {feed.location_context.distance_meters != null && (
                <span className="ml-1.5 text-emerald-200/70">
                  距你 {feed.location_context.distance_meters}m
                </span>
              )}
            </div>
            <div className="mt-0.5 flex items-baseline gap-1.5">
              <span className="text-emerald-200/80 text-sm">可用资产</span>
              <span className="font-black text-white" style={{ fontSize: 24, fontWeight: 800 }}>
                ¥{balanceYuan.toFixed(2)}
              </span>
            </div>
          </div>
          <span className="rounded-full bg-white/15 px-3 py-1 text-xs font-bold">极速抢购模式</span>
        </div>
      </header>

      {/* 暴降/溯源大卡流 (HeroCard) */}
      <div className="space-y-4 px-3 pt-4 snap-y snap-mandatory overflow-y-auto">
        {heroItems.map((item, i) => (
          <MarketMakerHeroCard
            key={item.batch_id}
            item={item}
            index={i}
            onLock={handleLock}
            locked={cart.items.some((c) => c.batch_id === item.batch_id && c.status !== 'failed')}
          />
        ))}
      </div>

      {/* 刚需生鲜高密网格 (StandardGridItem) */}
      {gridItems.length > 0 && (
        <section className="mt-5 px-3">
          <h2 className="mb-2.5 text-base font-extrabold text-gray-700">刚需生鲜</h2>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
            {gridItems.map((item) => (
              <StandardGridItem
                key={item.batch_id}
                item={item}
                onLock={handleLock}
                locked={cart.items.some((c) => c.batch_id === item.batch_id && c.status !== 'failed')}
              />
            ))}
          </div>
        </section>
      )}

      {/* Toast */}
      {toast && (
        <div className="pointer-events-none fixed inset-x-0 top-20 z-50 flex justify-center px-4">
          <div className="rounded-2xl bg-black/80 px-5 py-3 text-center text-base font-bold text-white shadow-2xl backdrop-blur">
            {toast}
          </div>
        </div>
      )}

      {/* 悬浮购物车栏 (全局倒计时) */}
      {cart.lockedCount > 0 && (
        <div className="fixed inset-x-0 bottom-0 z-40 px-3 pb-3">
          <button
            type="button"
            onClick={() => {
              // 传递结算上下文: 取货点 + 资产余额 (Zero-Fill Checkout 读取)
              try {
                window.sessionStorage.setItem(
                  'hemall_feed_context',
                  JSON.stringify({
                    nodeName: feed.location_context.node_name ?? '附近节点',
                    distanceMeters: feed.location_context.distance_meters,
                    balanceCents: feed.location_context.user_system_balance ?? 0,
                  }),
                );
              } catch { /* ignore */ }
              router.push('/shop/express-checkout');
            }}
            className="flex w-full items-center justify-between rounded-3xl bg-emerald-700 px-5 py-4 text-white shadow-2xl active:scale-[0.99] transition-transform"
          >
            <div className="text-left">
              <div className="text-sm font-bold text-emerald-100">已锁单 {cart.lockedCount} 件</div>
              <div className="font-black" style={{ fontSize: 24, fontWeight: 800 }}>
                ¥{(cart.totalCents / 100).toFixed(2)}
              </div>
            </div>
            <div className="text-center">
              <div className="text-[11px] font-semibold text-emerald-200">库存锁释放倒计时</div>
              <div
                className="font-black tabular-nums text-amber-300"
                style={{ fontSize: 28, fontWeight: 800 }}
              >
                {formatCountdown(cart.remainingMs)}
              </div>
            </div>
            <div className="rounded-2xl bg-white px-4 py-3 text-base font-extrabold text-emerald-700">
              去结算 →
            </div>
          </button>
        </div>
      )}

      {/* 空态时的浏览链接 */}
      {cart.lockedCount === 0 && (
        <div className="mt-6 text-center">
          <Link href="/shop" className="text-sm text-emerald-600 hover:underline">
            浏览全部商品 →
          </Link>
        </div>
      )}
    </div>
  );
}
