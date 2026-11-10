'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { api } from '@/lib/api-client';
import { errMessage } from '@/lib/errors';
import { formatMoney } from '@/lib/format';
import type { Order } from '@/types/api';

/**
 * 履约运维入口页。
 *
 * 旧的“拣货确认 / 幽灵库存 / 邻居代送”裸 JSON 卡片已下线——门店实际在
 * 订单详情里完成实称、发货、自提核销、创建履约与打印拣货单。这里只做
 * 待处理订单的入口聚合，避免运维页和门店真实操作各走一套。
 */
export default function FulfillmentPage() {
  const [orders, setOrders] = useState<Order[]>([]);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api.adminListOrders({ limit: 100 })
      .then((list) =>
        setOrders(
          list.filter((o) =>
            ['pending', 'paid', 'confirmed', 'processing', 'packed', 'shipped'].includes(o.status),
          ),
        ),
      )
      .catch((e) => setError(errMessage(e)))
      .finally(() => setLoading(false));
  }, []);

  return (
    <div>
      <h1 className="text-2xl font-bold mb-2">履约运维</h1>
      <p className="text-sm text-gray-500 mb-6">
        门店履约统一在订单详情完成：录入实称、创建履约、发货、自提核销、打印拣货单。
        本页只汇总当前待处理的订单，点“订单详情”或“拣货单”进入实际入口。
      </p>

      {error && <div className="mb-4 text-red-600 text-sm">{error}</div>}

      <div className="bg-white border rounded-xl overflow-hidden">
        <div className="px-4 py-3 border-b font-semibold">待处理订单（{orders.length}）</div>
        {loading ? (
          <div className="px-4 py-8 text-center text-gray-400">加载中...</div>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-left text-gray-600">
              <tr>
                <th className="px-4 py-2">订单</th>
                <th className="px-4 py-2">状态</th>
                <th className="px-4 py-2">配送方式</th>
                <th className="px-4 py-2">金额</th>
                <th className="px-4 py-2">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {orders.map((o) => (
                <tr key={o.id}>
                  <td className="px-4 py-2 font-mono text-xs">{o.id.slice(0, 12)}…</td>
                  <td className="px-4 py-2">{o.status}</td>
                  <td className="px-4 py-2">{o.shipping_method_code || 'standard'}</td>
                  <td className="px-4 py-2">{formatMoney(o.grand_total_cents, o.currency)}</td>
                  <td className="px-4 py-2">
                    <div className="flex gap-3">
                      <Link href={`/admin/orders?order=${o.id}`} className="text-blue-600 hover:underline">
                        订单详情
                      </Link>
                      <Link
                        href={`/admin/orders/${o.id}/print`}
                        target="_blank"
                        className="text-emerald-600 hover:underline"
                      >
                        拣货单
                      </Link>
                    </div>
                  </td>
                </tr>
              ))}
              {orders.length === 0 && (
                <tr>
                  <td colSpan={5} className="px-4 py-8 text-center text-gray-400">暂无待处理订单</td>
                </tr>
              )}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
