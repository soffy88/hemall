'use client';

import { useEffect, useState } from 'react';
import { api } from '@/lib/api-client';
import { errMessage } from '@/lib/errors';

function yuan(cents: number): string {
  return `¥${(cents / 100).toFixed(2)}`;
}

function today(): string {
  const d = new Date();
  const local = new Date(d.getTime() - d.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 10);
}

type Report = Awaited<ReturnType<typeof api.storeOpsDailyReport>>;

export default function DailyReportPage() {
  const [day, setDay] = useState(today());
  const [data, setData] = useState<Report | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  async function load(date: string) {
    setLoading(true);
    setError('');
    try {
      setData(await api.storeOpsDailyReport(date));
    } catch (e) {
      setError(errMessage(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { void load(day); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const k = data?.kpis;

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <div>
          <h1 className="text-2xl font-bold">每日报表</h1>
          <p className="text-sm text-gray-500">按日期看销售额、订单数、客单价、称重多退少补、退款、缺货预订与 Top 20 SKU。</p>
        </div>
        <div className="flex gap-2">
          <input type="date" value={day} onChange={(e) => setDay(e.target.value)}
            className="px-3 py-2 border rounded-lg text-sm" />
          <button onClick={() => load(day)} disabled={loading}
            className="px-4 py-2 bg-emerald-600 text-white rounded-lg text-sm disabled:opacity-50">
            {loading ? '查询中...' : '查询'}
          </button>
          <a href={`/api/admin/store-ops/daily-report.csv?date=${day}`}
            className="px-4 py-2 border rounded-lg text-sm hover:bg-gray-50">导出 CSV</a>
        </div>
      </div>

      {error && <div className="mb-4 text-red-600 text-sm">{error}</div>}

      {k && (
        <div className="grid grid-cols-2 md:grid-cols-4 xl:grid-cols-7 gap-3 mb-6">
          {[
            ['销售额', yuan(k.sales_cents)],
            ['订单数', String(k.order_count)],
            ['客单价', yuan(k.aov_cents)],
            ['称重退款', yuan(k.weigh_refund_cents)],
            ['称重补收', yuan(k.weigh_charge_cents)],
            ['退款', yuan(k.refund_cents)],
            ['缺货预订', String(k.preorder_qty)],
          ].map(([label, value]) => (
            <div key={label} className="rounded-xl border bg-white p-4">
              <div className="text-xs text-gray-500">{label}</div>
              <div className="text-xl font-bold mt-1">{value}</div>
            </div>
          ))}
        </div>
      )}

      <div className="bg-white border rounded-xl overflow-hidden">
        <div className="px-4 py-3 border-b font-semibold">Top 20 SKU（{data?.date || day}）</div>
        <table className="w-full text-sm">
          <thead className="bg-gray-50 text-left text-gray-600">
            <tr>
              <th className="px-4 py-2">#</th>
              <th className="px-4 py-2">商品</th>
              <th className="px-4 py-2">销量</th>
              <th className="px-4 py-2">销售额</th>
            </tr>
          </thead>
          <tbody className="divide-y">
            {(data?.top_skus || []).map((s, i) => (
              <tr key={`${s.title}-${i}`}>
                <td className="px-4 py-2 text-gray-400">{i + 1}</td>
                <td className="px-4 py-2">{s.title}</td>
                <td className="px-4 py-2">{s.qty} {s.unit}</td>
                <td className="px-4 py-2">{yuan(s.amount_cents)}</td>
              </tr>
            ))}
            {(data?.top_skus || []).length === 0 && (
              <tr><td colSpan={4} className="px-4 py-8 text-center text-gray-400">当日暂无销售</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
