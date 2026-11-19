'use client';

import { useEffect, useState } from 'react';
import { useRouter, usePathname } from 'next/navigation';
import { OAppShell, OSideBar } from '@helios/oui';
import type { SideBarNavItem } from '@helios/oui';
import { logout } from '@/lib/auth-store';
import { api } from '@/lib/api-client';
import { ToastProvider, useToast } from '@/components/Toast';

// 禁止静态生成：需要认证检查
export const dynamic = 'force-dynamic';

const navItems: SideBarNavItem[] = [
  { id: 'pos', label: '收银台', href: '/store/pos' },
  { id: 'pick-weigh', label: '拣货称重', href: '/store/pick-weigh' },
  { id: 'scales', label: '秤设备管理', href: '/store/scales' },
  { id: 'vision', label: '识别模型管理', href: '/store/vision' },
];

function LayoutContent({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [collapsed, setCollapsed] = useState(false);
  const [isStaff, setIsStaff] = useState(false);
  const showToast = useToast();

  const activeId = navItems.find(
    (item) => item.href === pathname || (item.href !== '/store' && pathname.startsWith(item.href!))
  )?.id || 'pos';

  async function checkStaff() {
    try {
      const me = await api.customerMe();
      setIsStaff(!!me.is_staff);
    } catch {
      setIsStaff(false);
    }
  }

  useEffect(() => {
    checkStaff();
  }, []);

  function handleItemClick(item: SideBarNavItem) {
    if (item.href) router.push(item.href);
  }

  async function handleLogout() {
    await logout();
    router.push('/login');
  }

  if (!isStaff) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-50">
        <div className="text-center p-8">
          <h1 className="text-2xl font-bold text-gray-900 mb-4">权限不足</h1>
          <p className="text-gray-600 mb-6">此页面仅限店员/管理员访问</p>
          <button
            onClick={handleLogout}
            className="px-4 py-2 bg-emerald-700 text-white rounded-lg hover:bg-emerald-800 transition"
          >
            返回登录
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="h-screen flex flex-col">
      <OAppShell
        topbar={
          <div className="flex items-center justify-between w-full px-4 h-14 bg-white border-b border-gray-200">
            <div className="flex items-center gap-3">
              <span className="text-lg font-semibold text-gray-900">宋鲜生 门店工作台</span>
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

export default function StoreLayout({ children }: { children: React.ReactNode }) {
  return (
    <ToastProvider>
      <LayoutContent children={children} />
    </ToastProvider>
  );
}