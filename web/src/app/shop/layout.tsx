'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { api } from '@/lib/api-client';
import { getSelectedRegion, setSelectedRegion } from '@/lib/region-store';
import { getCustomerAuth } from '@/lib/customer-auth-store';
import { ToastProvider, useToast } from '@/components/Toast';
import type { Region } from '@/types/api';

function ShopChrome({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const showToast = useToast();
  const [regions, setRegions] = useState<Region[]>([]);
  const [regionCode, setRegionCode] = useState('');
  const [customerEmail, setCustomerEmail] = useState<string | null>(null);

  useEffect(() => {
    setCustomerEmail(getCustomerAuth()?.email || null);
  }, [pathname]);

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
      } catch (e: any) {
        showToast(`切换区域失败: ${e.message}`, 'error');
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
          <Link href="/shop" className="flex items-center gap-2 shrink-0">
            <span className="w-8 h-8 rounded-lg bg-emerald-700 text-white flex items-center justify-center text-base">
              🛍
            </span>
            <span className="text-lg font-bold tracking-tight text-gray-900">Hemall</span>
          </Link>
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
