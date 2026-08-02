'use client';

import { Fragment, useEffect, useState } from 'react';
import { api } from '@/lib/api-client';
import { formatMoney } from '@/lib/format';
import type { Order, Address } from '@/types/api';

const emptyAddress: Address = {
  recipient_name: '',
  phone: '',
  address_line1: '',
  address_line2: '',
  city: '',
  region_code: '',
  postal_code: '',
};

function parseLineItems(text: string): { batch_id: string; quantity: number }[] {
  return text
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
    .map((pair) => {
      const [batch_id, qty] = pair.split(':').map((x) => x.trim());
      return { batch_id, quantity: Number(qty) || 1 };
    });
}

export default function DraftOrdersPage() {
  const [orders, setOrders] = useState<Order[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [showForm, setShowForm] = useState(false);

  const [customerId, setCustomerId] = useState('');
  const [regionCode, setRegionCode] = useState('');
  const [currency, setCurrency] = useState('CNY');
  const [lineItemsText, setLineItemsText] = useState('');
  const [address, setAddress] = useState<Address>({ ...emptyAddress });

  const [editingAddressId, setEditingAddressId] = useState<string | null>(null);
  const [editAddress, setEditAddress] = useState<Address>({ ...emptyAddress });

  async function load() {
    setLoading(true);
    try {
      const data = await api.adminListOrders({ status: 'draft', limit: 100 });
      setOrders(data);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, []);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    const line_items = parseLineItems(lineItemsText);
    if (line_items.length === 0) { alert('请填写至少一项商品 (batch_id:数量)'); return; }
    const hasAddress = address.recipient_name && address.address_line1;
    try {
      const res = await api.createDraftOrder({
        customer_id: customerId || undefined,
        region_code: regionCode || undefined,
        currency: currency || undefined,
        line_items,
        billing_address: hasAddress ? { ...address } as Record<string, unknown> : undefined,
        shipping_address: hasAddress ? { ...address } as Record<string, unknown> : undefined,
      });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '创建失败');
      setCustomerId(''); setRegionCode(''); setLineItemsText(''); setAddress({ ...emptyAddress });
      setShowForm(false);
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  function openEditAddress(o: Order) {
    setEditingAddressId(editingAddressId === o.id ? null : o.id);
    setEditAddress(o.shipping_address || { ...emptyAddress });
  }

  async function handleSaveAddress(o: Order, e: React.FormEvent) {
    e.preventDefault();
    try {
      const res = await api.updateDraftOrder({
        order_id: o.id,
        billing_address: { ...editAddress } as Record<string, unknown>,
        shipping_address: { ...editAddress } as Record<string, unknown>,
      });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '更新地址失败');
      setEditingAddressId(null);
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleEditCustomer(o: Order) {
    const customer_id = prompt('客户 ID：', o.customer_id || '');
    if (customer_id == null) return;
    try {
      const res = await api.updateDraftOrder({ order_id: o.id, customer_id });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '更新失败');
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleMarkPaid(o: Order) {
    const provider = prompt('支付方式：', 'manual') || 'manual';
    try {
      const res = await api.markDraftOrderPaid({ order_id: o.id, payment_provider_name: provider });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '标记失败');
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleDelete(id: string) {
    if (!confirm('确认删除此草稿订单？将释放已预留库存。')) return;
    try {
      const res = await api.deleteDraftOrder({ order_id: id });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '删除失败');
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold">草稿订单</h1>
        <button onClick={() => setShowForm(!showForm)} className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 text-sm">
          {showForm ? '取消' : '新建草稿订单'}
        </button>
      </div>

      {showForm && (
        <form onSubmit={handleCreate} className="mb-6 p-4 bg-white rounded-xl border border-gray-200 space-y-3">
          <div className="grid grid-cols-3 gap-3">
            <div>
              <label className="text-xs text-gray-500">客户 ID（可选）</label>
              <input value={customerId} onChange={(e) => setCustomerId(e.target.value)} className="w-full px-3 py-2 border rounded-lg text-sm" />
            </div>
            <div>
              <label className="text-xs text-gray-500">区域代码</label>
              <input value={regionCode} onChange={(e) => setRegionCode(e.target.value)} placeholder="r-62b90d" className="w-full px-3 py-2 border rounded-lg text-sm" />
            </div>
            <div>
              <label className="text-xs text-gray-500">币种</label>
              <input value={currency} onChange={(e) => setCurrency(e.target.value)} className="w-full px-3 py-2 border rounded-lg text-sm" />
            </div>
          </div>
          <div>
            <label className="text-xs text-gray-500">商品（batch_id:数量，逗号分隔）</label>
            <input value={lineItemsText} onChange={(e) => setLineItemsText(e.target.value)} placeholder="019f...:1, 019f...:2" required className="w-full px-3 py-2 border rounded-lg text-sm" />
          </div>
          <div>
            <label className="text-xs text-gray-500">收货地址（可选；账单地址同收货地址）</label>
            <div className="grid grid-cols-3 gap-2 mt-1">
              <input placeholder="收件人" value={address.recipient_name} onChange={(e) => setAddress({ ...address, recipient_name: e.target.value })} className="px-3 py-2 border rounded-lg text-sm" />
              <input placeholder="电话" value={address.phone} onChange={(e) => setAddress({ ...address, phone: e.target.value })} className="px-3 py-2 border rounded-lg text-sm" />
              <input placeholder="详细地址" value={address.address_line1} onChange={(e) => setAddress({ ...address, address_line1: e.target.value })} className="px-3 py-2 border rounded-lg text-sm" />
              <input placeholder="城市" value={address.city} onChange={(e) => setAddress({ ...address, city: e.target.value })} className="px-3 py-2 border rounded-lg text-sm" />
              <input placeholder="邮编" value={address.postal_code} onChange={(e) => setAddress({ ...address, postal_code: e.target.value })} className="px-3 py-2 border rounded-lg text-sm" />
              <input placeholder="区域代码" value={address.region_code} onChange={(e) => setAddress({ ...address, region_code: e.target.value })} className="px-3 py-2 border rounded-lg text-sm" />
            </div>
          </div>
          <button type="submit" className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm hover:bg-blue-700">创建</button>
        </form>
      )}

      {error && <div className="mb-4 text-red-600">{error}</div>}

      {loading ? (
        <div className="text-gray-500">加载中...</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm bg-white rounded-xl overflow-hidden border border-gray-200">
            <thead className="bg-gray-50 text-left">
              <tr>
                <th className="px-4 py-3 font-medium text-gray-600">订单号</th>
                <th className="px-4 py-3 font-medium text-gray-600">客户</th>
                <th className="px-4 py-3 font-medium text-gray-600">金额</th>
                <th className="px-4 py-3 font-medium text-gray-600">商品明细</th>
                <th className="px-4 py-3 font-medium text-gray-600">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {orders.map((o) => (
                <Fragment key={o.id}>
                  <tr className="hover:bg-gray-50">
                    <td className="px-4 py-3 font-mono text-xs">{o.id.slice(0, 12)}...</td>
                    <td className="px-4 py-3 text-xs">{o.customer_id ? o.customer_id.slice(0, 12) + '...' : '—'}</td>
                    <td className="px-4 py-3 font-medium">{formatMoney(o.grand_total_cents, o.currency)}</td>
                    <td className="px-4 py-3 text-xs text-gray-500">
                      {(o.line_items || []).map((li) => (
                        <div key={li.id}>{li.product_title || li.variant_sku} × {li.quantity}</div>
                      ))}
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex gap-2">
                        <button onClick={() => handleEditCustomer(o)} className="text-xs text-blue-600 hover:underline">改客户</button>
                        <button onClick={() => openEditAddress(o)} className="text-xs text-blue-600 hover:underline">
                          {editingAddressId === o.id ? '收起' : '改地址'}
                        </button>
                        <button onClick={() => handleMarkPaid(o)} className="text-xs text-emerald-600 hover:underline">标记已付款</button>
                        <button onClick={() => handleDelete(o.id)} className="text-xs text-red-600 hover:underline">删除</button>
                      </div>
                    </td>
                  </tr>
                  {editingAddressId === o.id && (
                    <tr>
                      <td colSpan={5} className="px-4 py-3 bg-gray-50">
                        <form onSubmit={(e) => handleSaveAddress(o, e)} className="grid grid-cols-3 gap-2">
                          <input placeholder="收件人" value={editAddress.recipient_name} onChange={(e) => setEditAddress({ ...editAddress, recipient_name: e.target.value })} required className="px-2 py-1 border rounded text-sm" />
                          <input placeholder="电话" value={editAddress.phone} onChange={(e) => setEditAddress({ ...editAddress, phone: e.target.value })} required className="px-2 py-1 border rounded text-sm" />
                          <input placeholder="详细地址" value={editAddress.address_line1} onChange={(e) => setEditAddress({ ...editAddress, address_line1: e.target.value })} required className="px-2 py-1 border rounded text-sm" />
                          <input placeholder="城市" value={editAddress.city} onChange={(e) => setEditAddress({ ...editAddress, city: e.target.value })} required className="px-2 py-1 border rounded text-sm" />
                          <input placeholder="邮编" value={editAddress.postal_code} onChange={(e) => setEditAddress({ ...editAddress, postal_code: e.target.value })} required className="px-2 py-1 border rounded text-sm" />
                          <input placeholder="区域代码" value={editAddress.region_code} onChange={(e) => setEditAddress({ ...editAddress, region_code: e.target.value })} className="px-2 py-1 border rounded text-sm" />
                          <button type="submit" className="col-span-3 px-3 py-1 bg-blue-600 text-white rounded text-sm hover:bg-blue-700">保存地址</button>
                        </form>
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
              {orders.length === 0 && (
                <tr><td colSpan={5} className="px-4 py-8 text-center text-gray-400">暂无草稿订单</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
