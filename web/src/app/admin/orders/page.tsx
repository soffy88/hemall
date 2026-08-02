'use client';

import { useEffect, useState } from 'react';
import { api } from '@/lib/api-client';
import { formatMoney } from '@/lib/format';
import type { Order, Fulfillment } from '@/types/api';

function fmt(cents: number, currency?: string): string {
  return formatMoney(cents, currency);
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleString('zh-CN');
}

export default function OrdersPage() {
  const [orders, setOrders] = useState<Order[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState<Order | null>(null);
  const [statusFilter, setStatusFilter] = useState('');
  const [fulfillments, setFulfillments] = useState<Fulfillment[]>([]);

  async function load() {
    setLoading(true);
    try {
      const data = await api.adminListOrders({ status: statusFilter || undefined });
      setOrders(data);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, []);

  async function loadDetail(id: string) {
    try {
      const [order, fulfillmentList] = await Promise.all([
        api.adminGetOrder(id),
        api.adminListFulfillments(id),
      ]);
      setSelected(order);
      setFulfillments(fulfillmentList);
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function refreshSelected() {
    if (selected) await loadDetail(selected.id);
    load();
  }

  async function handleUpdateStatus() {
    if (!selected) return;
    const s = prompt('新状态 (如 pending / processing / completed / canceled)：', selected.status);
    if (!s) return;
    try {
      const res = await api.updateOrder({ order_id: selected.id, status: s });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '更新失败');
      refreshSelected();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleCancelOrder() {
    if (!selected || !confirm('确认取消此订单？将释放库存并按需退款。')) return;
    try {
      const res = await api.cancelOrder({ order_id: selected.id });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '取消失败');
      refreshSelected();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleArchiveOrder() {
    if (!selected || !confirm('确认归档此订单？')) return;
    try {
      const res = await api.archiveOrder({ order_id: selected.id });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '归档失败');
      refreshSelected();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleCapturePayment() {
    if (!selected) return;
    try {
      const res = await api.capturePayment({ order_id: selected.id });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '捕获支付失败');
      alert('支付已捕获');
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleRefund() {
    if (!selected) return;
    const amountStr = prompt(`退款金额（分），订单总额 ${selected.grand_total_cents}：`);
    if (!amountStr) return;
    const amount = Number(amountStr);
    if (!Number.isFinite(amount) || amount <= 0) {
      alert('请输入有效金额');
      return;
    }
    try {
      const res = await api.refundPayment({ order_id: selected.id, amount_cents: amount });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '退款失败');
      alert('退款成功');
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleCreateFulfillment() {
    if (!selected) return;
    const items = (selected.line_items || []).map((li) => ({ order_line_item_id: li.id, quantity: li.quantity }));
    if (items.length === 0) {
      alert('无商品明细');
      return;
    }
    try {
      const res = await api.createFulfillment({ order_id: selected.id, items });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '创建履约失败');
      loadDetail(selected.id);
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleShipFulfillment(id: string) {
    const provider = prompt('物流公司名称：', 'manual');
    if (!provider) return;
    const trackingNo = prompt('运单号：') || '';
    try {
      const res = await api.shipFulfillment({
        fulfillment_id: id,
        provider_name: provider,
        shipment_info: { tracking_no: trackingNo },
      });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '发货失败');
      if (selected) loadDetail(selected.id);
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleCancelFulfillment(id: string) {
    if (!confirm('确认取消此履约单？')) return;
    try {
      const res = await api.cancelFulfillment({ fulfillment_id: id });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '取消履约失败');
      if (selected) loadDetail(selected.id);
    } catch (e: any) {
      alert(e.message);
    }
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold">订单管理</h1>
        <select
          value={statusFilter}
          onChange={(e) => { setStatusFilter(e.target.value); }}
          className="px-3 py-2 border rounded-lg text-sm"
        >
          <option value="">全部状态</option>
          <option value="draft">草稿</option>
          <option value="pending">待处理</option>
          <option value="processing">处理中</option>
          <option value="completed">已完成</option>
          <option value="canceled">已取消</option>
        </select>
      </div>

      {error && <div className="mb-4 text-red-600">{error}</div>}

      {loading ? (
        <div className="text-gray-500">加载中...</div>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          {/* Order List */}
          <div className="lg:col-span-2 overflow-x-auto">
            <table className="w-full text-sm bg-white rounded-xl overflow-hidden border border-gray-200">
              <thead className="bg-gray-50 text-left">
                <tr>
                  <th className="px-4 py-3 font-medium text-gray-600">订单号</th>
                  <th className="px-4 py-3 font-medium text-gray-600">状态</th>
                  <th className="px-4 py-3 font-medium text-gray-600">金额</th>
                  <th className="px-4 py-3 font-medium text-gray-600">时间</th>
                  <th className="px-4 py-3 font-medium text-gray-600">操作</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {orders.map((o) => (
                  <tr key={o.id} className="hover:bg-gray-50 cursor-pointer" onClick={() => loadDetail(o.id)}>
                    <td className="px-4 py-3 font-mono text-xs">{o.id.slice(0, 12)}...</td>
                    <td className="px-4 py-3">
                      <span className={`px-2 py-0.5 rounded text-xs ${
                        o.status === 'pending' ? 'bg-amber-100 text-amber-700' :
                        o.status === 'completed' ? 'bg-green-100 text-green-700' :
                        o.status === 'canceled' ? 'bg-red-100 text-red-700' :
                        'bg-gray-100 text-gray-600'
                      }`}>
                        {o.status}
                      </span>
                    </td>
                    <td className="px-4 py-3 font-medium">{fmt(o.grand_total_cents, o.currency)}</td>
                    <td className="px-4 py-3 text-gray-500 text-xs">{formatDate(o.created_at)}</td>
                    <td className="px-4 py-3">
                      <button onClick={(e) => { e.stopPropagation(); loadDetail(o.id); }} className="text-xs text-blue-600 hover:underline">
                        详情
                      </button>
                    </td>
                  </tr>
                ))}
                {orders.length === 0 && (
                  <tr><td colSpan={5} className="px-4 py-8 text-center text-gray-400">暂无订单</td></tr>
                )}
              </tbody>
            </table>
          </div>

          {/* Order Detail Panel */}
          <div className="lg:col-span-1">
            {selected ? (
              <div className="bg-white rounded-xl border border-gray-200 p-4 space-y-4 sticky top-4">
                <div className="flex items-center justify-between">
                  <h2 className="font-semibold">订单详情</h2>
                  <button onClick={() => setSelected(null)} className="text-gray-400 hover:text-gray-600 text-sm">✕</button>
                </div>
                <div className="text-xs space-y-1">
                  <div><span className="text-gray-500">ID:</span> <span className="font-mono">{selected.id}</span></div>
                  <div><span className="text-gray-500">状态:</span> {selected.status}</div>
                  <div><span className="text-gray-500">币种:</span> {selected.currency}</div>
                  <div><span className="text-gray-500">区域:</span> {selected.region_code || '—'}</div>
                </div>
                <div className="border-t pt-3 space-y-1 text-sm">
                  <div className="flex justify-between"><span>小计</span><span>{fmt(selected.subtotal_cents, selected.currency)}</span></div>
                  <div className="flex justify-between"><span>折扣</span><span>-{fmt(selected.discount_cents, selected.currency)}</span></div>
                  <div className="flex justify-between"><span>税</span><span>{fmt(selected.tax_cents, selected.currency)}</span></div>
                  <div className="flex justify-between"><span>运费</span><span>{fmt(selected.shipping_cents, selected.currency)}</span></div>
                  <div className="flex justify-between font-bold border-t pt-1"><span>总计</span><span>{fmt(selected.grand_total_cents, selected.currency)}</span></div>
                </div>
                {selected.shipping_address && (
                  <div className="border-t pt-3 text-xs">
                    <div className="text-gray-500 mb-1">收货地址</div>
                    <div>{selected.shipping_address.recipient_name}</div>
                    <div>{selected.shipping_address.phone}</div>
                    <div>{selected.shipping_address.address_line1} {selected.shipping_address.address_line2}</div>
                    <div>{selected.shipping_address.city} {selected.shipping_address.postal_code}</div>
                  </div>
                )}
                <div className="border-t pt-3">
                  <div className="text-gray-500 text-xs mb-2">商品明细</div>
                  {(selected.line_items || []).map((li) => (
                    <div key={li.id} className="flex justify-between text-xs py-1 border-b border-gray-50">
                      <div>
                        <span className="font-medium">{li.product_title || '—'}</span>
                        <span className="text-gray-400 ml-1">{li.variant_sku}</span>
                        <span className="text-gray-400 ml-1">×{li.quantity}</span>
                      </div>
                      <span>{fmt(li.line_total_cents, selected.currency)}</span>
                    </div>
                  ))}
                </div>

                {/* Actions */}
                <div className="border-t pt-3 space-y-2">
                  <div className="text-gray-500 text-xs mb-1">操作</div>
                  <div className="flex flex-wrap gap-2">
                    <button onClick={handleUpdateStatus} className="text-xs px-2 py-1 bg-gray-100 rounded hover:bg-gray-200">更新状态</button>
                    <button onClick={handleCancelOrder} className="text-xs px-2 py-1 bg-red-50 text-red-700 rounded hover:bg-red-100">取消订单</button>
                    <button onClick={handleArchiveOrder} className="text-xs px-2 py-1 bg-gray-100 rounded hover:bg-gray-200">归档</button>
                    <button onClick={handleCapturePayment} className="text-xs px-2 py-1 bg-emerald-50 text-emerald-700 rounded hover:bg-emerald-100">捕获支付</button>
                    <button onClick={handleRefund} className="text-xs px-2 py-1 bg-amber-50 text-amber-700 rounded hover:bg-amber-100">退款</button>
                    <button onClick={handleCreateFulfillment} className="text-xs px-2 py-1 bg-blue-50 text-blue-700 rounded hover:bg-blue-100">创建履约</button>
                  </div>
                  {fulfillments.length > 0 && (
                    <div className="text-xs space-y-1 pt-1">
                      {fulfillments.map((f) => (
                        <div key={f.id} className="flex items-center justify-between bg-gray-50 rounded px-2 py-1">
                          <span className="font-mono text-gray-500">{f.id.slice(0, 12)}... · {f.status}</span>
                          {f.status === 'canceled' ? (
                            <span className="text-gray-400">已取消</span>
                          ) : f.status === 'shipped' ? (
                            <span className="text-green-600">已发货{f.tracking_number ? ` (${f.tracking_number})` : ''}</span>
                          ) : (
                            <span className="flex gap-2">
                              <button onClick={() => handleShipFulfillment(f.id)} className="text-blue-600 hover:underline">发货</button>
                              <button onClick={() => handleCancelFulfillment(f.id)} className="text-red-600 hover:underline">取消</button>
                            </span>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            ) : (
              <div className="bg-gray-50 rounded-xl border border-dashed border-gray-300 p-8 text-center text-gray-400">
                点击订单查看详情
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
