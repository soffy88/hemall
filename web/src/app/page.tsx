import Link from 'next/link';

export default function HomePage() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-gradient-to-br from-gray-50 to-gray-100">
      <div className="text-center space-y-8">
        <h1 className="text-4xl font-bold tracking-tight text-gray-900">
          Hemall Commerce
        </h1>
        <p className="text-lg text-gray-600 max-w-md mx-auto">
          3O 范式电商平台 — 商家后台管理与买家商城一体化
        </p>
        <div className="flex gap-4 justify-center">
          <Link
            href="/admin"
            className="px-6 py-3 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition font-medium"
          >
            商家后台
          </Link>
          <Link
            href="/shop"
            className="px-6 py-3 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 transition font-medium"
          >
            商城入口
          </Link>
        </div>
      </div>
    </div>
  );
}
