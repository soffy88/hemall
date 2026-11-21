'use client';

/**
 * /admin/preorders — 预订配货
 *
 * 客户（含商家）缺货预订的订单行汇总：显示每行还差多少待配货、
 * 当前可用库存；点「配货」按现有库存给该行配货，配完自动刷新。
 */

import { useEffect, useState } from 'react';
import { api } from '@/lib/api-client';
import type { AdminPreorder } from '@/types/api';
import { errMessage } from '@/lib/errors';

export default function AdminPreordersPage() {
  const [rows, setRows] = useState<AdminPreorder[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [allocatingId, setAllocatingId] = useState<string | null>(null);

  function load() {
    api.adminListPreorders()
      .then((data) => {
        setRows(data);
        setError('');
      })
      .catch((e) => setError(errMessage(e)))
      .finally(() => setLoading(false));
  }

  useEffect(() => { load(); }, []);

  async function handleAllocate(row: AdminPreorder) {
    setAllocatingId(row.line_id);
    setNotice('');
    try {
      const res = await api.adminAllocatePreorder(row.line_id);
      setNotice(
        `「${row.title}」本次配货 ${res.allocated} 件，剩余待配货 ${res.preorder_qty_left} 件。`,
      );
      setLoading(true);
      load();
    } catch (e) {
      alert(errMessage(e));
    } finally {
      setAllocatingId(null);
    }
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold">预订配货</h1>
        <button
          onClick={() => { setLoading(true); load(); }}
          className="px-4 py-2 bg-gray-100 rounded-lg hover:bg-gray-200 text-sm"
        >
          刷新
        </button>
      </div>

      {error && <div className="mb-4 text-red-600">{error}</div>}
      {notice && (
        <div className="mb-4 px-4 py-3 bg-emerald-50 border border-emerald-200 rounded-lg text-sm text-emerald-700">
          {notice}
        </div>
      )}

      {loading ? (
        <div className="text-gray-500">加载中...</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm bg-white rounded-xl overflow-hidden border border-gray-200">
            <thead className="bg-gray-50 text-left">
              <tr>
                <th className="px-4 py-3 font-medium text-gray-600">客户</th>
                <th className="px-4 py-3 font-medium text-gray-600">商品</th>
                <th className="px-4 py-3 font-medium text-gray-600">订单数量</th>
                <th className="px-4 py-3 font-medium text-gray-600">待配货</th>
                <th className="px-4 py-3 font-medium text-gray-600">现可用库存</th>
                <th className="px-4 py-3 font-medium text-gray-600">下单时间</th>
                <th className="px-4 py-3 font-medium text-gray-600">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {rows.map((r) => (
                <tr key={r.line_id} className="hover:bg-gray-50">
                  <td className="px-4 py-3">
                    <div className="font-medium">{r.customer_name || '—'}</div>
                    <div className="text-xs text-gray-400 font-mono">{r.customer_id.slice(0, 8)}…</div>
                  </td>
                  <td className="px-4 py-3">{r.title}</td>
                  <td className="px-4 py-3">{r.quantity}</td>
                  <td className="px-4 py-3">
                    <span className="px-2 py-0.5 rounded text-xs bg-amber-100 text-amber-700">{r.preorder_qty}</span>
                  </td>
                  <td className="px-4 py-3">
                    {r.available_qty_now > 0
                      ? <span className="text-emerald-700">{r.available_qty_now} 件</span>
                      : <span className="text-gray-400">0 件</span>}
                  </td>
                  <td className="px-4 py-3 text-gray-500">
                    {new Date(r.created_at).toLocaleString('zh-CN')}
                  </td>
                  <td className="px-4 py-3">
                    <button
                      onClick={() => handleAllocate(r)}
                      disabled={allocatingId === r.line_id || r.available_qty_now <= 0}
                      className="px-3 py-1.5 bg-blue-600 text-white rounded-lg text-xs hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      {allocatingId === r.line_id ? '配货中...' : '配货'}
                    </button>
                  </td>
                </tr>
              ))}
              {rows.length === 0 && (
                <tr>
                  <td colSpan={7} className="px-4 py-8 text-center text-gray-400">
                    暂无待配货的预订
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
