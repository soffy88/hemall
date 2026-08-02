'use client';

import { useState } from 'react';
import { api } from '@/lib/api-client';
import { formatMoney } from '@/lib/format';
import type { Order } from '@/types/api';

export default function LookupPage() {
  const [token, setToken] = useState('');
  const [order, setOrder] = useState<Order | null>(null);
  const fmt = (cents: number) => formatMoney(cents, order?.currency);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  async function handleLookup() {
    if (!token.trim()) return;
    setError('');
    setOrder(null);
    setLoading(true);
    try {
      const data = await api.lookupOrder(token.trim());
      setOrder(data);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="max-w-xl mx-auto">
      <h1 className="text-2xl font-bold mb-6 text-center">凭收据查单</h1>

      <div className="bg-white rounded-xl border border-gray-200 p-5 space-y-4">
        <div>
          <label className="block text-sm text-gray-600 mb-1">收据 Token</label>
          <textarea
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder="粘贴下单成功时获得的收据 token..."
            className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm font-mono"
            rows={3}
          />
        </div>
        <button
          onClick={handleLookup}
          disabled={loading || !token.trim()}
          className="w-full py-2 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 disabled:opacity-50 transition text-sm"
        >
          {loading ? '查询中...' : '查询订单'}
        </button>
      </div>

      {error && <div className="mt-4 text-center text-red-600 text-sm">{error}</div>}

      {order && (
        <div className="mt-6 bg-white rounded-xl border border-gray-200 p-5 space-y-3">
          <h2 className="font-semibold">订单详情</h2>
          <div className="text-sm space-y-1">
            <div className="flex justify-between">
              <span className="text-gray-500">订单号</span>
              <span className="font-mono text-xs">{order.id}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-gray-500">状态</span>
              <span>{order.status}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-gray-500">总额</span>
              <span className="font-bold text-emerald-700">{fmt(order.grand_total_cents)}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-gray-500">时间</span>
              <span>{new Date(order.created_at).toLocaleString('zh-CN')}</span>
            </div>
          </div>
          <div className="border-t pt-3">
            <div className="text-xs text-gray-500 mb-2">商品明细</div>
            {(order.line_items || []).map((li) => (
              <div key={li.id} className="flex justify-between text-sm py-1">
                <span>{li.product_title || '—'} × {li.quantity}</span>
                <span>{fmt(li.line_total_cents)}</span>
              </div>
            ))}
          </div>
          {order.shipping_address && (
            <div className="border-t pt-3 text-xs">
              <div className="text-gray-500 mb-1">收货地址</div>
              <div>{order.shipping_address.recipient_name} {order.shipping_address.phone}</div>
              <div>{order.shipping_address.address_line1} {order.shipping_address.city} {order.shipping_address.postal_code}</div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
