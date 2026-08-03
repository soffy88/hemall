'use client';

/**
 * /shop/market — 已合并进 /shop 主页 (整个 shop 就是生鲜日用超市)
 * 保留此路由用于旧链接兼容，直接重定向回 /shop。
 */

import { useEffect } from 'react';
import { useRouter } from 'next/navigation';

export default function MarketRedirect() {
  const router = useRouter();
  useEffect(() => {
    router.replace('/shop');
  }, [router]);
  return (
    <div className="flex min-h-screen items-center justify-center bg-gray-50">
      <p className="text-gray-400">正在跳转到生鲜日用超市...</p>
    </div>
  );
}
