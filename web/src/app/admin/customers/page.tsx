'use client';

import { Fragment, useEffect, useState } from 'react';
import { api } from '@/lib/api-client';
import { ActionCard, OpsSection } from '@/components/ops/OpsActionCard';
import type { Customer, CustomerGroup, CustomerAddress } from '@/types/api';

function fmt(cents: number): string {
  return `¥${(cents / 100).toFixed(2)}`;
}

export default function CustomersPage() {
  // hemall 扩展运维（原独立运维页面搬迁过来的裸操作卡片）用的独立 ctx。
  const [opsCtx] = useState<Record<string, string>>({});

  const [customers, setCustomers] = useState<Customer[]>([]);
  const [groups, setGroups] = useState<CustomerGroup[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);

  const [email, setEmail] = useState('');
  const [phone, setPhone] = useState('');
  const [name, setName] = useState('');
  const [custStatus, setCustStatus] = useState('active');

  const [showGroupForm, setShowGroupForm] = useState(false);
  const [groupName, setGroupName] = useState('');

  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [addresses, setAddresses] = useState<CustomerAddress[]>([]);
  const [showAddrForm, setShowAddrForm] = useState(false);
  const [addrRecipient, setAddrRecipient] = useState('');
  const [addrPhone, setAddrPhone] = useState('');
  const [addrLine1, setAddrLine1] = useState('');
  const [addrCity, setAddrCity] = useState('');
  const [addrRegion, setAddrRegion] = useState('');
  const [addrPostal, setAddrPostal] = useState('');

  async function load() {
    setLoading(true);
    try {
      const [c, g] = await Promise.all([api.adminListCustomers(), api.adminListCustomerGroups()]);
      setCustomers(c);
      setGroups(g);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, []);

  function openCreate() {
    setEditingId(null);
    setEmail(''); setPhone(''); setName(''); setCustStatus('active');
    setShowForm(true);
  }

  function openEdit(c: Customer) {
    setEditingId(c.id);
    setEmail(c.email); setPhone(c.phone || ''); setName(c.name || ''); setCustStatus(c.status);
    setShowForm(true);
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    try {
      if (editingId) {
        const res = await api.updateCustomer({ customer_id: editingId, email, phone, name, status: custStatus });
        if (res.status === 'failed') throw new Error((res.error as any)?.message || '更新失败');
      } else {
        const res = await api.createCustomer({ email, phone, name });
        if (res.status === 'failed') throw new Error((res.error as any)?.message || '创建失败');
      }
      setShowForm(false);
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleCreateGroup(e: React.FormEvent) {
    e.preventDefault();
    try {
      const res = await api.createCustomerGroup({ name: groupName });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '创建失败');
      setGroupName('');
      setShowGroupForm(false);
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleAssignGroup(customerId: string, groupId: string) {
    if (!groupId) return;
    try {
      const res = await api.assignCustomerToGroup({ customer_id: customerId, group_id: groupId });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '分组失败');
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function toggleAddresses(customerId: string) {
    if (expandedId === customerId) {
      setExpandedId(null);
      return;
    }
    setExpandedId(customerId);
    setShowAddrForm(false);
    try {
      const data = await api.adminListCustomerAddresses(customerId);
      setAddresses(data);
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleAddAddress(customerId: string, e: React.FormEvent) {
    e.preventDefault();
    try {
      const res = await api.addCustomerAddress({
        customer_id: customerId,
        recipient_name: addrRecipient,
        phone: addrPhone,
        address_line1: addrLine1,
        city: addrCity,
        region_code: addrRegion,
        postal_code: addrPostal,
      });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '添加地址失败');
      setAddrRecipient(''); setAddrPhone(''); setAddrLine1(''); setAddrCity(''); setAddrRegion(''); setAddrPostal('');
      setShowAddrForm(false);
      const data = await api.adminListCustomerAddresses(customerId);
      setAddresses(data);
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleSetDefaultAddress(customerId: string, addressId: string) {
    try {
      const res = await api.updateCustomerAddress({ address_id: addressId, is_default: true });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '设置失败');
      const data = await api.adminListCustomerAddresses(customerId);
      setAddresses(data);
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleDeleteAddress(customerId: string, addressId: string) {
    if (!confirm('确认删除此地址？')) return;
    try {
      const res = await api.deleteCustomerAddress({ address_id: addressId });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '删除失败');
      const data = await api.adminListCustomerAddresses(customerId);
      setAddresses(data);
    } catch (e: any) {
      alert(e.message);
    }
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold">客户管理</h1>
        <button onClick={showForm && !editingId ? () => setShowForm(false) : openCreate} className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 text-sm">
          {showForm && !editingId ? '取消' : '新建客户'}
        </button>
      </div>

      {showForm && (
        <form onSubmit={handleSubmit} className="mb-6 p-4 bg-white rounded-xl border border-gray-200 space-y-3">
          <h2 className="font-semibold">{editingId ? '编辑客户' : '新建客户'}</h2>
          <div className="grid grid-cols-3 gap-3">
            <input placeholder="邮箱" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required className="px-3 py-2 border rounded-lg text-sm" />
            <input placeholder="电话" value={phone} onChange={(e) => setPhone(e.target.value)} className="px-3 py-2 border rounded-lg text-sm" />
            <input placeholder="姓名" value={name} onChange={(e) => setName(e.target.value)} className="px-3 py-2 border rounded-lg text-sm" />
          </div>
          {editingId && (
            <select value={custStatus} onChange={(e) => setCustStatus(e.target.value)} className="px-3 py-2 border rounded-lg text-sm">
              <option value="active">active</option>
              <option value="inactive">inactive</option>
            </select>
          )}
          <div className="flex gap-2">
            <button type="submit" className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm hover:bg-blue-700">
              {editingId ? '保存' : '创建'}
            </button>
            <button type="button" onClick={() => setShowForm(false)} className="px-4 py-2 bg-gray-100 rounded-lg text-sm hover:bg-gray-200">取消</button>
          </div>
        </form>
      )}

      {/* Customer Groups */}
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-lg font-semibold">买家分组</h2>
        <button onClick={() => setShowGroupForm(!showGroupForm)} className="px-4 py-2 bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 text-sm">
          {showGroupForm ? '取消' : '新建分组'}
        </button>
      </div>
      {showGroupForm && (
        <form onSubmit={handleCreateGroup} className="mb-4 p-4 bg-white rounded-xl border border-gray-200 flex gap-3 items-end">
          <div>
            <label className="text-xs text-gray-500">分组名称</label>
            <input value={groupName} onChange={(e) => setGroupName(e.target.value)} required className="px-3 py-2 border rounded-lg text-sm" />
          </div>
          <button type="submit" className="px-4 py-2 bg-indigo-600 text-white rounded-lg text-sm hover:bg-indigo-700">创建</button>
        </form>
      )}
      {groups.length > 0 && (
        <div className="mb-6 flex flex-wrap gap-2 text-xs text-gray-500">
          {groups.map((g) => (
            <span key={g.id} className="px-2 py-1 bg-gray-100 rounded">{g.name} ({g.member_count})</span>
          ))}
        </div>
      )}

      {error && <div className="mb-4 text-red-600">{error}</div>}

      {loading ? (
        <div className="text-gray-500">加载中...</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm bg-white rounded-xl overflow-hidden border border-gray-200">
            <thead className="bg-gray-50 text-left">
              <tr>
                <th className="px-4 py-3 font-medium text-gray-600">姓名</th>
                <th className="px-4 py-3 font-medium text-gray-600">邮箱</th>
                <th className="px-4 py-3 font-medium text-gray-600">电话</th>
                <th className="px-4 py-3 font-medium text-gray-600">分组</th>
                <th className="px-4 py-3 font-medium text-gray-600">状态</th>
                <th className="px-4 py-3 font-medium text-gray-600">订单数</th>
                <th className="px-4 py-3 font-medium text-gray-600">累计消费</th>
                <th className="px-4 py-3 font-medium text-gray-600">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {customers.map((c) => (
                <Fragment key={c.id}>
                  <tr className="hover:bg-gray-50">
                    <td className="px-4 py-3 font-medium">{c.name || '—'}</td>
                    <td className="px-4 py-3">{c.email}</td>
                    <td className="px-4 py-3">{c.phone || '—'}</td>
                    <td className="px-4 py-3">
                      <select
                        value={c.customer_group_id || ''}
                        onChange={(e) => handleAssignGroup(c.id, e.target.value)}
                        className="text-xs border rounded px-1 py-0.5"
                      >
                        <option value="">未分组</option>
                        {groups.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
                      </select>
                    </td>
                    <td className="px-4 py-3">
                      <span className={`px-2 py-0.5 rounded text-xs ${c.status === 'active' ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-600'}`}>{c.status}</span>
                    </td>
                    <td className="px-4 py-3">{c.order_count}</td>
                    <td className="px-4 py-3">{fmt(c.total_spent_cents)}</td>
                    <td className="px-4 py-3">
                      <div className="flex gap-2">
                        <button onClick={() => openEdit(c)} className="text-xs text-blue-600 hover:underline">编辑</button>
                        <button onClick={() => toggleAddresses(c.id)} className="text-xs text-blue-600 hover:underline">
                          {expandedId === c.id ? '收起地址' : '地址'}
                        </button>
                      </div>
                    </td>
                  </tr>
                  {expandedId === c.id && (
                    <tr>
                      <td colSpan={8} className="px-4 py-3 bg-gray-50">
                        <div className="text-xs space-y-2">
                          {addresses.map((a) => (
                            <div key={a.id} className="flex items-center justify-between bg-white rounded px-3 py-2 border border-gray-100">
                              <span>
                                {a.recipient_name} · {a.phone} · {a.address_line1} {a.address_line2} · {a.city} {a.postal_code}
                                {a.is_default && <span className="ml-2 text-emerald-600">(默认)</span>}
                              </span>
                              <span className="flex gap-2 shrink-0">
                                {!a.is_default && (
                                  <button onClick={() => handleSetDefaultAddress(c.id, a.id)} className="text-blue-600 hover:underline">设为默认</button>
                                )}
                                <button onClick={() => handleDeleteAddress(c.id, a.id)} className="text-red-600 hover:underline">删除</button>
                              </span>
                            </div>
                          ))}
                          {addresses.length === 0 && <div className="text-gray-400">暂无地址</div>}

                          {showAddrForm ? (
                            <form onSubmit={(e) => handleAddAddress(c.id, e)} className="grid grid-cols-3 gap-2 bg-white p-3 rounded border border-gray-100">
                              <input placeholder="收件人" value={addrRecipient} onChange={(e) => setAddrRecipient(e.target.value)} required className="px-2 py-1 border rounded" />
                              <input placeholder="电话" value={addrPhone} onChange={(e) => setAddrPhone(e.target.value)} required className="px-2 py-1 border rounded" />
                              <input placeholder="详细地址" value={addrLine1} onChange={(e) => setAddrLine1(e.target.value)} required className="px-2 py-1 border rounded" />
                              <input placeholder="城市" value={addrCity} onChange={(e) => setAddrCity(e.target.value)} required className="px-2 py-1 border rounded" />
                              <input placeholder="区域代码" value={addrRegion} onChange={(e) => setAddrRegion(e.target.value)} className="px-2 py-1 border rounded" />
                              <input placeholder="邮编" value={addrPostal} onChange={(e) => setAddrPostal(e.target.value)} required className="px-2 py-1 border rounded" />
                              <div className="col-span-3 flex gap-2">
                                <button type="submit" className="px-3 py-1 bg-blue-600 text-white rounded hover:bg-blue-700">保存</button>
                                <button type="button" onClick={() => setShowAddrForm(false)} className="px-3 py-1 bg-gray-100 rounded hover:bg-gray-200">取消</button>
                              </div>
                            </form>
                          ) : (
                            <button onClick={() => setShowAddrForm(true)} className="text-blue-600 hover:underline">+ 添加地址</button>
                          )}
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
              {customers.length === 0 && (
                <tr><td colSpan={8} className="px-4 py-8 text-center text-gray-400">暂无客户</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      <div className="mt-10">
        <h2 className="text-lg font-semibold mb-3">hemall 扩展运维 — 会员订阅</h2>
        <OpsSection title="会员">
          <ActionCard
            title="会员订阅 process_subscription"
            badge="public"
            desc="收会员费，开通进场购买权限；重复订阅会被拒绝。"
            fields={[
              { key: 'customer_ref', label: 'customer_ref' },
              { key: 'plan_fee_cents', label: 'plan_fee_cents', type: 'number' },
              { key: 'duration_days', label: 'duration_days (默认 365)', type: 'number' },
            ]}
            defaults={opsCtx}
            onSubmit={(v) => api.hemallProcessSubscription(v as any)}
          />
        </OpsSection>
      </div>
    </div>
  );
}
