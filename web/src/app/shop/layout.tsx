'use client';

import { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { api } from '@/lib/api-client';
import { getSelectedRegion, setSelectedRegion } from '@/lib/region-store';
import { getCustomerAuth, initCustomerAuth, subscribeCustomerAuth } from '@/lib/customer-auth-store';
import { ToastProvider, useToast } from '@/components/Toast';
import type { Region } from '@/types/api';
import { errMessage } from '@/lib/errors';

// 分类图标映射 (与 /shop 胶囊条一致, 后端只存 name/slug)。
const CATEGORY_ICONS: Record<string, string> = {
  vegetables: '🥬', fruits: '🍎', 'meat-eggs': '🥩', 'grain-oil': '🌾',
  'dairy-bakery': '🥛', frozen: '🧊', snacks: '🍫', drinks: '🥤', daily: '🧻',
  seafood: '🦐', electronics: '📱',
};

type StoreCategory = { id: string; name: string; slug: string; count: number };

function ShopChrome({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const showToast = useToast();
  const [regions, setRegions] = useState<Region[]>([]);
  const [regionCode, setRegionCode] = useState('');
  const [customerEmail, setCustomerEmail] = useState<string | null>(null);
  const [isMerchant, setIsMerchant] = useState(false);
  const [categories, setCategories] = useState<StoreCategory[]>([]);
  const [catOpen, setCatOpen] = useState(false);
  const [activeCatId, setActiveCatId] = useState('all');
  const catRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let alive = true;
    const sync = () => {
      if (alive) setCustomerEmail(getCustomerAuth()?.email || null);
    };
    // cookie 会话：等会话拉取完成再读，避免首帧把已登录用户当成未登录；之后订阅变更。
    void initCustomerAuth().then(sync);
    const unsubscribe = subscribeCustomerAuth(sync);
    return () => {
      alive = false;
      unsubscribe();
    };
  }, [pathname]);

  // 商家账号才显示「商家预订」入口：登录态确定后拉一次 me 看 is_merchant。
  useEffect(() => {
    if (!customerEmail) {
      setIsMerchant(false);
      return;
    }
    let alive = true;
    api.customerMe()
      .then((me) => { if (alive) setIsMerchant(!!me.is_merchant); })
      .catch(() => { if (alive) setIsMerchant(false); });
    return () => { alive = false; };
  }, [customerEmail]);

  // 菜单栏分类下拉: 分类列表 + 当前选中分类 (读 ?cat=)。
  useEffect(() => {
    api.storeCategories().then(setCategories).catch(() => {});
  }, []);

  useEffect(() => {
    setActiveCatId(new URLSearchParams(window.location.search).get('cat') || 'all');
    setCatOpen(false);
  }, [pathname]);

  useEffect(() => {
    if (!catOpen) return;
    const onDoc = (e: MouseEvent) => {
      if (catRef.current && !catRef.current.contains(e.target as Node)) setCatOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setCatOpen(false);
    };
    document.addEventListener('mousedown', onDoc);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDoc);
      document.removeEventListener('keydown', onKey);
    };
  }, [catOpen]);

  function goCategory(id: string) {
    setCatOpen(false);
    window.location.assign(id === 'all' ? '/shop' : `/shop?cat=${encodeURIComponent(id)}`);
  }

  useEffect(() => {
    api.storeRegions().then((data) => {
      setRegions(data);
      const saved = getSelectedRegion();
      const initial = data.find((r) => r.code === saved?.code) || data[0];
      if (initial) {
        setRegionCode(initial.code);
        setSelectedRegion({ code: initial.code, currency: initial.currency });
      }
    }).catch(() => {});
  }, []);

  async function handleRegionChange(code: string) {
    const region = regions.find((r) => r.code === code);
    if (!region) return;
    setSelectedRegion({ code: region.code, currency: region.currency });

    const cartId = typeof window !== 'undefined' ? sessionStorage.getItem('hemall_cart_id') : null;
    if (cartId) {
      try {
        await api.storeSetCartRegion(cartId, region.code, region.currency);
      } catch (e) {
        showToast(`切换区域失败: ${errMessage(e)}`, 'error');
        return;
      }
    }

    // 商品价格/购物车金额等大量组件都是渲染时直接读 getSelectedRegion()/cart，
    // 不是订阅式状态；全页刷新一次是最简单可靠的让它们全部拿到新值的办法。
    window.location.reload();
  }

  return (
    <div className="min-h-screen bg-gradient-to-b from-gray-50 to-white">
      {/* Header */}
      <header className="bg-white/90 backdrop-blur border-b border-gray-200/80 sticky top-0 z-40">
        <div className="max-w-6xl mx-auto px-4 h-16 flex items-center justify-between gap-4">
          <div className="flex items-center gap-2 shrink-0">
            <Link href="/shop" className="flex items-center gap-2 shrink-0">
              <span className="w-8 h-8 rounded-lg bg-emerald-700 text-white flex items-center justify-center text-base">
                🛍
              </span>
              <span className="text-lg font-bold tracking-tight text-gray-900">Hemall</span>
            </Link>
            {/* 商品分类下拉: 菜单栏最左侧, 点击弹出分类列表 */}
            <div className="relative" ref={catRef}>
              <button
                type="button"
                onClick={() => setCatOpen((v) => !v)}
                aria-expanded={catOpen}
                className="flex items-center gap-1 rounded-full bg-emerald-700 px-3 py-1.5 text-sm font-bold text-white hover:bg-emerald-800 transition"
              >
                🗂 商品分类 {catOpen ? '▴' : '▾'}
              </button>
              {catOpen && (
                <div className="absolute left-0 top-full z-50 mt-2 w-60 rounded-xl border border-gray-200 bg-white py-2 shadow-xl">
                  <button
                    type="button"
                    onClick={() => goCategory('all')}
                    className={`flex w-full items-center gap-2 px-4 py-2 text-left text-sm transition hover:bg-emerald-50 ${activeCatId === 'all' ? 'font-bold text-emerald-700 bg-emerald-50' : 'text-gray-700'}`}
                  >
                    <span className="w-5 text-center">🛒</span>
                    <span className="flex-1">全部商品</span>
                    <span className="text-xs text-gray-400">{categories.reduce((n, c) => n + c.count, 0)}</span>
                  </button>
                  {categories.map((c) => (
                    <button
                      key={c.id}
                      type="button"
                      onClick={() => goCategory(c.id)}
                      className={`flex w-full items-center gap-2 px-4 py-2 text-left text-sm transition hover:bg-emerald-50 ${activeCatId === c.id ? 'font-bold text-emerald-700 bg-emerald-50' : 'text-gray-700'}`}
                    >
                      <span className="w-5 text-center">{CATEGORY_ICONS[c.slug] ?? '🛒'}</span>
                      <span className="flex-1">{c.name}</span>
                      <span className="text-xs text-gray-400">{c.count}</span>
                    </button>
                  ))}
                </div>
              )}
            </div>
          </div>
          <nav className="flex items-center gap-5 text-sm">
            {regions.length > 0 && (
              <select
                value={regionCode}
                onChange={(e) => handleRegionChange(e.target.value)}
                className="text-xs border border-gray-200 rounded-full px-3 py-1.5 text-gray-600 bg-gray-50 hover:bg-gray-100 transition"
              >
                {regions.map((r) => (
                  <option key={r.code} value={r.code}>{r.name} ({r.currency})</option>
                ))}
              </select>
            )}
            <Link href="/shop" className="text-gray-600 hover:text-emerald-700 transition font-medium">
              全部商品
            </Link>
            <Link href="/shop/cart" className="text-gray-600 hover:text-emerald-700 transition font-medium">
              🛒 购物车
            </Link>
            {isMerchant && (
              <Link href="/shop/merchant" className="text-gray-600 hover:text-emerald-700 transition font-medium">
                🏪 商家预订
              </Link>
            )}
            <Link href="/shop/lookup" className="text-gray-500 hover:text-emerald-700 transition">
              查单
            </Link>
            {customerEmail ? (
              <Link
                href="/shop/account"
                className="text-gray-700 hover:text-emerald-700 transition font-medium bg-emerald-50 rounded-full px-3 py-1.5"
              >
                👤 {customerEmail}
              </Link>
            ) : (
              <Link
                href="/shop/login"
                className="text-white bg-emerald-700 hover:bg-emerald-800 transition font-medium rounded-full px-4 py-1.5"
              >
                登录/注册
              </Link>
            )}
          </nav>
        </div>
      </header>

      {/* Content */}
      <main className="max-w-6xl mx-auto px-4 py-8">
        {children}
      </main>

      {/* Footer */}
      <footer className="border-t border-gray-200 mt-16 py-8 text-center text-xs text-gray-400">
        Hemall Commerce · Powered by 3O
      </footer>
    </div>
  );
}

export default function ShopLayout({ children }: { children: React.ReactNode }) {
  return (
    <ToastProvider>
      <ShopChrome>{children}</ShopChrome>
    </ToastProvider>
  );
}
