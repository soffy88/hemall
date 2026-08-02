import Link from 'next/link';

export default function SupplierHomePage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">供应商门户</h1>
        <p className="mt-2 text-sm text-gray-500">
          产地/果农自助录入原产地信息即可入驻，无需人工审核；入驻后每笔通过验收的批次
          都会计入你的质押余额，信誉分决定平台对你的信任等级。
        </p>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <Link
          href="/supplier/register"
          className="block bg-white rounded-xl border border-gray-200 p-5 hover:border-amber-400 transition"
        >
          <div className="text-2xl mb-2">📝</div>
          <div className="font-semibold mb-1">入驻登记</div>
          <div className="text-xs text-gray-500">填写钱包账号 + 产地位置，立即获得供应商账号</div>
        </Link>
        <Link
          href="/supplier/status"
          className="block bg-white rounded-xl border border-gray-200 p-5 hover:border-amber-400 transition"
        >
          <div className="text-2xl mb-2">🔍</div>
          <div className="font-semibold mb-1">查询我的状态</div>
          <div className="text-xs text-gray-500">按钱包账号查看信誉分 / 质押余额 / 账号状态</div>
        </Link>
      </div>

      <div className="text-xs text-gray-400 bg-gray-100 rounded-lg p-3">
        目前还没有真正的登录体系——钱包账号只是入驻时自己填的标识，请自行妥善保存，
        平台暂不提供找回。
      </div>
    </div>
  );
}
