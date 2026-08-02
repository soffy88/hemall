'use client';

import { useEffect, useState } from 'react';
import { useRouter, usePathname } from 'next/navigation';
import { OAppShell, OSideBar } from '@helios/oui';
import type { SideBarNavItem } from '@helios/oui';
import { getAuth, clearAuth } from '@/lib/auth-store';

const navItems: SideBarNavItem[] = [
  { id: 'dashboard', label: '仪表盘', href: '/admin' },
  { id: 'products', label: '商品管理', href: '/admin/products' },
  { id: 'catalog', label: '分类与合集', href: '/admin/catalog' },
  { id: 'orders', label: '订单管理', href: '/admin/orders' },
  { id: 'draft-orders', label: '草稿订单', href: '/admin/draft-orders' },
  { id: 'manual-order', label: '代客下单', href: '/admin/manual-order' },
  { id: 'customers', label: '客户管理', href: '/admin/customers' },
  { id: 'marketing', label: '营销管理', href: '/admin/marketing' },
  { id: 'aftersales', label: '售后管理', href: '/admin/aftersales' },
  { id: 'inventory', label: '库存与渠道', href: '/admin/inventory' },
  { id: 'supply-chain', label: '供应链运维', href: '/admin/supply-chain' },
  { id: 'fulfillment', label: '履约运维', href: '/admin/fulfillment' },
  { id: 'settlement', label: '分润结算运维', href: '/admin/settlement' },
  { id: 'growth', label: '增长运维', href: '/admin/growth' },
  { id: 'settings', label: '区域与税率', href: '/admin/settings' },
  { id: 'batch-jobs', label: '批处理任务', href: '/admin/batch-jobs' },
  { id: 'users', label: '管理员账号', href: '/admin/users' },
];

export default function AdminLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [ready, setReady] = useState(false);
  const [collapsed, setCollapsed] = useState(false);

  useEffect(() => {
    const auth = getAuth();
    if (!auth) {
      router.push('/login');
    } else {
      setReady(true);
    }
  }, [router]);

  const activeId = navItems.find(
    (item) => item.href === pathname || (item.href !== '/admin' && pathname.startsWith(item.href!))
  )?.id || 'dashboard';

  function handleItemClick(item: SideBarNavItem) {
    if (item.href) router.push(item.href);
  }

  function handleLogout() {
    clearAuth();
    router.push('/login');
  }

  if (!ready) {
    return <div className="flex min-h-screen items-center justify-center"><span className="text-gray-500">加载中...</span></div>;
  }

  return (
    <div className="h-screen flex flex-col">
      <OAppShell
        topbar={
          <div className="flex items-center justify-between w-full px-4 h-14 bg-white border-b border-gray-200">
            <div className="flex items-center gap-3">
              <span className="text-lg font-semibold text-gray-900">Hemall 商家后台</span>
            </div>
            <div className="flex items-center gap-3">
              <a href="/shop" target="_blank" className="text-sm text-blue-600 hover:underline">访问商城</a>
              <button onClick={handleLogout} className="text-sm text-gray-500 hover:text-red-600">退出</button>
            </div>
          </div>
        }
        sidebar={
          <OSideBar
            items={navItems}
            activeId={activeId}
            collapsed={collapsed}
            onCollapsedChange={setCollapsed}
            onItemClick={handleItemClick}
          />
        }
        sidebarCollapsed={collapsed}
        onSidebarCollapsedChange={setCollapsed}
      >
        <div className="p-6 overflow-auto">
          {children}
        </div>
      </OAppShell>
    </div>
  );
}
