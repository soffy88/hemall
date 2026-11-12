'use client';

import { useEffect } from 'react';
import { usePathname } from 'next/navigation';
import { getCustomerAuth, subscribeCustomerAuth } from '@/lib/customer-auth-store';
import { bootChatwoot, getChatwootConfig, shouldShowChatwoot, syncChatwootUser } from '@/lib/chatwoot';

/** 商城右下角的 Chatwoot 客服窗口。
 *  只在 NEXT_PUBLIC_CHATWOOT_* 配置齐全时加载；后台/管理页不渲染。
 *  登录顾客按用户 ID + 昵称识别，未登录则重置为匿名访客。
 */
export default function ChatwootWidget() {
  const pathname = usePathname();

  useEffect(() => {
    const config = getChatwootConfig();
    if (!config || !shouldShowChatwoot(pathname)) return;

    let cancelled = false;
    let attempts = 0;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const sync = () => {
      if (cancelled) return;
      if (syncChatwootUser(getCustomerAuth())) return;
      // $chatwoot 由 SDK 异步挂载，最多重试 20 次（约 5 秒）。
      if (attempts < 20) {
        attempts += 1;
        timer = setTimeout(sync, 250);
      }
    };

    void bootChatwoot(config).then(sync);
    const unsubscribe = subscribeCustomerAuth(sync);

    return () => {
      cancelled = true;
      unsubscribe();
      if (timer) clearTimeout(timer);
    };
  }, [pathname]);

  return null;
}
