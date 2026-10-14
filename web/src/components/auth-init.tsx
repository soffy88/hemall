'use client';

import { useEffect } from 'react';
import { usePathname, useRouter } from 'next/navigation';
import { initAuth } from '@/lib/auth-store';
import { initCustomerAuth } from '@/lib/customer-auth-store';

/**
 * 根会话引导：应用启动时按 HttpOnly cookie 各拉一次 staff / 顾客会话
 * （initAuth/initCustomerAuth 内部去重，其他页面拿到的是缓存），并统一处理
 * 401 刷新失败后的跳转。
 */
export default function AuthInit() {
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    void initAuth();
    void initCustomerAuth();
  }, []);

  useEffect(() => {
    const onUnauthorized = (e: Event) => {
      const kind = (e as CustomEvent<{ kind?: string }>).detail?.kind;
      const target = kind === 'customer' ? '/shop/login' : '/login';
      // 只在各自区域内跳转，避免 storefront 的 401 把用户甩去后台登录页。
      if (pathname === target || pathname.startsWith(`${target}/`)) return;
      if (kind === 'customer' ? !pathname.startsWith('/shop') : !pathname.startsWith('/admin')) return;
      router.replace(target);
    };
    window.addEventListener('hemall:unauthorized', onUnauthorized);
    return () => window.removeEventListener('hemall:unauthorized', onUnauthorized);
  }, [pathname, router]);

  return null;
}
