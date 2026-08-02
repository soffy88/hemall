'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { api } from '@/lib/api-client';
import { getSelectedRegion, setSelectedRegion } from '@/lib/region-store';
import { getCustomerAuth } from '@/lib/customer-auth-store';
import type { Region } from '@/types/api';

export default function ShopLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const isCheckout = pathname.includes('checkout') || pathname.includes('success');
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
        alert(`切换区域失败: ${e.message}`);
      }
    }

    // 商品价格/购物车金额等大量组件都是渲染时直接读 getSelectedRegion()/cart，
    // 不是订阅式状态；全页刷新一次是最简单可靠的让它们全部拿到新值的办法。
    window.location.reload();
  }

  return (
    <div className="min-h-screen bg-gray-50">
      {/* Header */}
      <header className="bg-white border-b border-gray-200 sticky top-0 z-40">
        <div className="max-w-6xl mx-auto px-4 h-14 flex items-center justify-between">
          <Link href="/shop" className="text-lg font-bold text-emerald-700">
            🛍 Hemall 商城
          </Link>
          <nav className="flex items-center gap-4">
            {regions.length > 0 && (
              <select
                value={regionCode}
                onChange={(e) => handleRegionChange(e.target.value)}
                className="text-xs border border-gray-200 rounded px-2 py-1 text-gray-600"
              >
                {regions.map((r) => (
                  <option key={r.code} value={r.code}>{r.name} ({r.currency})</option>
                ))}
              </select>
            )}
            <Link href="/shop" className="text-sm text-gray-600 hover:text-gray-900">
              全部商品
            </Link>
            <Link href="/shop/cart" className="relative text-sm text-gray-600 hover:text-gray-900">
              🛒 购物车
            </Link>
            <Link href="/shop/lookup" className="text-sm text-gray-500 hover:text-gray-700">
              查单
            </Link>
            {customerEmail ? (
              <Link href="/shop/account" className="text-sm text-gray-600 hover:text-gray-900">
                👤 {customerEmail}
              </Link>
            ) : (
              <Link href="/shop/login" className="text-sm text-gray-600 hover:text-gray-900">
                登录/注册
              </Link>
            )}
          </nav>
        </div>
      </header>

      {/* Content */}
      <main className="max-w-6xl mx-auto px-4 py-6">
        {children}
      </main>

      {/* Footer */}
      <footer className="border-t border-gray-200 mt-12 py-6 text-center text-xs text-gray-400">
        Hemall Commerce · Powered by 3O
      </footer>
    </div>
  );
}
