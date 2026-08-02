'use client';

import { useEffect, useState } from 'react';
import { api } from '@/lib/api-client';
import type { DashboardKPIs } from '@/types/api';

function fmt(cents: number): string {
  return `¥${(cents / 100).toFixed(2)}`;
}

export default function DashboardPage() {
  const [kpis, setKpis] = useState<DashboardKPIs | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    api.adminDashboardKPIs().then(setKpis).catch((e) => setError(e.message));
  }, []);

  if (error) return <div className="text-red-600">加载失败: {error}</div>;
  if (!kpis) return <div className="text-gray-500">加载中...</div>;

  const cards = [
    { label: '总订单', value: kpis.total_orders, color: 'bg-blue-50 text-blue-700' },
    { label: '总收入', value: fmt(kpis.total_revenue_cents), color: 'bg-emerald-50 text-emerald-700' },
    { label: '近30天收入', value: fmt(kpis.revenue_30d_cents), color: 'bg-purple-50 text-purple-700' },
    { label: '待处理订单', value: kpis.pending_orders, color: 'bg-amber-50 text-amber-700' },
    { label: '商品总数', value: `${kpis.total_products} (${kpis.published_products} 已发布)`, color: 'bg-indigo-50 text-indigo-700' },
    { label: '客户总数', value: kpis.total_customers, color: 'bg-pink-50 text-pink-700' },
  ];

  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">仪表盘</h1>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
        {cards.map((c) => (
          <div key={c.label} className={`rounded-xl p-5 ${c.color}`}>
            <div className="text-sm font-medium opacity-80">{c.label}</div>
            <div className="text-2xl font-bold mt-1">{c.value}</div>
          </div>
        ))}
      </div>
    </div>
  );
}
