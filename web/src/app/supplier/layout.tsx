'use client';

import Link from 'next/link';

/**
 * 小型供应商门户 —— 果农/产地供应商自助入驻 + 查询自己的信誉分/质押余额/状态。
 * 跟顾客商城 (/shop) 、商家后台 (/admin) 是三个完全独立的身份体系：供应商
 * 目前没有真正的登录系统 (wallet_account 只是注册时自己填的自由字符串，
 * 不是账号凭据)，这个门户不假装有登录态，纯粹是"填单入驻 + 按 wallet_
 * account 查询"两个公开表单，故意不做成跟 /shop 一样带头像/退出登录的样子。
 */
export default function SupplierLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen bg-gray-50">
      <header className="bg-white border-b border-gray-200 sticky top-0 z-40">
        <div className="max-w-2xl mx-auto px-4 h-14 flex items-center justify-between">
          <Link href="/supplier" className="text-lg font-bold text-amber-700">
            🌾 Hemall 供应商门户
          </Link>
          <nav className="flex items-center gap-4">
            <Link href="/supplier/register" className="text-sm text-gray-600 hover:text-gray-900">
              入驻登记
            </Link>
            <Link href="/supplier/status" className="text-sm text-gray-600 hover:text-gray-900">
              查询状态
            </Link>
          </nav>
        </div>
      </header>

      <main className="max-w-2xl mx-auto px-4 py-8">
        {children}
      </main>

      <footer className="border-t border-gray-200 mt-12 py-6 text-center text-xs text-gray-400">
        Hemall Commerce · Powered by 3O
      </footer>
    </div>
  );
}
