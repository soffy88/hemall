'use client';

/**
 * /shop/feed — 极速商城 Feed (Phase 9 SPEC v9.0 客户端展示层)
 *
 * 定位: 扫码即买的零登录入口。页面启动即注册 Service Worker (弱网离线
 * 提货码硬缓存)，随后挂载 FeedView 渲染做市大卡 + 高密网格。
 */

import { useEffect } from 'react';
import FeedView from '@/components/market/FeedView';

export default function ShopFeedPage() {
  // PWA Service Worker 注册 (Phase 9 SPEC §5: 弱网离线核销凭证)
  useEffect(() => {
    if ('serviceWorker' in navigator && typeof window !== 'undefined') {
      navigator.serviceWorker.register('/sw.js').catch(() => {
        /* SW 注册失败不阻断页面 */
      });
    }
  }, []);

  return (
    <main className="mx-auto max-w-md min-h-screen bg-gray-50 pb-safe">
      <FeedView />
    </main>
  );
}
