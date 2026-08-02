'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { api } from '@/lib/api-client';
import { formatMoney } from '@/lib/format';
import { getCustomerAuth, clearCustomerAuth } from '@/lib/customer-auth-store';
import { useToast } from '@/components/Toast';
import type { Customer, CustomerAddress, Order, RmaClaim, SubmitClaimResult } from '@/types/api';

function claimDecisionCopy(decision: RmaClaim['decision']): { label: string; className: string; detail: string } {
  if (decision === 'instant_refund') {
    return { label: '已退款', className: 'bg-emerald-100 text-emerald-700', detail: '系统自动判定属实，退款已原路退回。' };
  }
  if (decision === 'drop_to_bin') {
    return { label: '待验证', className: 'bg-amber-100 text-amber-700', detail: '证据存疑，需要把商品投入回收桶验证后才会退款。' };
  }
  if (decision === 'rejected') {
    return { label: '未通过', className: 'bg-red-100 text-red-700', detail: '本次报案未通过审核，未产生退款。' };
  }
  return { label: '处理中', className: 'bg-gray-100 text-gray-600', detail: '' };
}

export default function AccountPage() {
  const router = useRouter();
  const showToast = useToast();
  const [profile, setProfile] = useState<Customer | null>(null);
  const [addresses, setAddresses] = useState<CustomerAddress[]>([]);
  const [orders, setOrders] = useState<Order[]>([]);
  const [claims, setClaims] = useState<RmaClaim[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  // 报告问题（RMA 客诉自助报案）——一次只展开一个订单行的表单。
  const [claimingLineItemId, setClaimingLineItemId] = useState<string | null>(null);
  const [evidenceUrl, setEvidenceUrl] = useState('');
  const [submittingClaim, setSubmittingClaim] = useState(false);
  const [claimResults, setClaimResults] = useState<Record<string, SubmitClaimResult | { error: string }>>({});

  const [editingProfile, setEditingProfile] = useState(false);
  const [name, setName] = useState('');
  const [phone, setPhone] = useState('');

  const [showAddrForm, setShowAddrForm] = useState(false);
  const [addrRecipient, setAddrRecipient] = useState('');
  const [addrPhone, setAddrPhone] = useState('');
  const [addrLine1, setAddrLine1] = useState('');
  const [addrCity, setAddrCity] = useState('');
  const [addrRegion, setAddrRegion] = useState('');
  const [addrPostal, setAddrPostal] = useState('');

  async function load() {
    if (!getCustomerAuth()) {
      router.push('/shop/login');
      return;
    }
    setLoading(true);
    try {
      const [p, a, o, c] = await Promise.all([
        api.customerMe(),
        api.customerMyAddresses(),
        api.customerMyOrders(),
        api.customerMyClaims(),
      ]);
      setProfile(p);
      setName(p.name || '');
      setPhone(p.phone || '');
      setAddresses(a);
      setOrders(o);
      setClaims(c);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, []);

  function handleLogout() {
    clearCustomerAuth();
    router.push('/shop');
  }

  async function handleSaveProfile(e: React.FormEvent) {
    e.preventDefault();
    try {
      await api.customerUpdateMe({ name, phone });
      setEditingProfile(false);
      load();
    } catch (e: any) {
      showToast(e.message, 'error');
    }
  }

  async function handleAddAddress(e: React.FormEvent) {
    e.preventDefault();
    try {
      await api.customerAddAddress({
        recipient_name: addrRecipient,
        phone: addrPhone,
        address_line1: addrLine1,
        city: addrCity,
        region_code: addrRegion,
        postal_code: addrPostal,
      });
      setAddrRecipient(''); setAddrPhone(''); setAddrLine1(''); setAddrCity(''); setAddrRegion(''); setAddrPostal('');
      setShowAddrForm(false);
      load();
    } catch (e: any) {
      showToast(e.message, 'error');
    }
  }

  async function handleSetDefault(addressId: string) {
    try {
      await api.customerUpdateAddress(addressId, { is_default: true });
      load();
    } catch (e: any) {
      showToast(e.message, 'error');
    }
  }

  async function handleDeleteAddress(addressId: string) {
    if (!confirm('确认删除此地址？')) return;
    try {
      await api.customerDeleteAddress(addressId);
      load();
    } catch (e: any) {
      showToast(e.message, 'error');
    }
  }

  function startClaim(lineItemId: string) {
    setClaimingLineItemId(lineItemId);
    setEvidenceUrl('');
  }

  async function handleSubmitClaim(orderId: string, batchId: string, lineItemId: string) {
    if (!evidenceUrl.trim()) { showToast('请填写证据图片链接', 'error'); return; }
    setSubmittingClaim(true);
    try {
      const result = await api.customerSubmitClaim({
        order_id: orderId,
        batch_id: batchId,
        evidence_image_url: evidenceUrl.trim(),
      });
      setClaimResults((prev) => ({ ...prev, [lineItemId]: result }));
      setClaimingLineItemId(null);
      const c = await api.customerMyClaims();
      setClaims(c);
    } catch (e: any) {
      setClaimResults((prev) => ({ ...prev, [lineItemId]: { error: e.message } }));
    } finally {
      setSubmittingClaim(false);
    }
  }

  if (loading) return <div className="text-center py-12 text-gray-500">加载中...</div>;
  if (error) return <div className="text-center py-12 text-red-600">{error}</div>;
  if (!profile) return null;

  return (
    <div className="max-w-3xl mx-auto space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">我的账号</h1>
        <button onClick={handleLogout} className="text-sm text-gray-500 hover:text-red-600">退出登录</button>
      </div>

      {/* Profile */}
      <div className="bg-white rounded-xl border border-gray-200 p-5">
        <div className="flex items-center justify-between mb-3">
          <h2 className="font-semibold">个人资料</h2>
          <button onClick={() => setEditingProfile(!editingProfile)} className="text-sm text-emerald-600 hover:underline">
            {editingProfile ? '取消' : '编辑'}
          </button>
        </div>
        {editingProfile ? (
          <form onSubmit={handleSaveProfile} className="space-y-3">
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="block text-xs text-gray-500 mb-1">姓名</label>
                <input value={name} onChange={(e) => setName(e.target.value)} className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm" />
              </div>
              <div>
                <label className="block text-xs text-gray-500 mb-1">手机号</label>
                <input value={phone} onChange={(e) => setPhone(e.target.value)} className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm" />
              </div>
            </div>
            <button type="submit" className="px-4 py-2 bg-emerald-600 text-white rounded-lg text-sm hover:bg-emerald-700">保存</button>
          </form>
        ) : (
          <div className="text-sm space-y-1 text-gray-700">
            <div>邮箱：{profile.email}</div>
            <div>姓名：{profile.name || '—'}</div>
            <div>手机号：{profile.phone || '—'}</div>
          </div>
        )}
      </div>

      {/* Addresses */}
      <div className="bg-white rounded-xl border border-gray-200 p-5">
        <div className="flex items-center justify-between mb-3">
          <h2 className="font-semibold">地址簿</h2>
          <button onClick={() => setShowAddrForm(!showAddrForm)} className="text-sm text-emerald-600 hover:underline">
            {showAddrForm ? '取消' : '+ 添加地址'}
          </button>
        </div>
        <div className="space-y-2 mb-3">
          {addresses.map((a) => (
            <div key={a.id} className="flex items-center justify-between text-sm bg-gray-50 rounded-lg px-3 py-2">
              <span>
                {a.recipient_name} · {a.phone} · {a.address_line1} {a.address_line2} · {a.city} {a.postal_code}
                {a.is_default && <span className="ml-2 text-emerald-600">(默认)</span>}
              </span>
              <span className="flex gap-2 shrink-0">
                {!a.is_default && (
                  <button onClick={() => handleSetDefault(a.id)} className="text-xs text-emerald-600 hover:underline">设为默认</button>
                )}
                <button onClick={() => handleDeleteAddress(a.id)} className="text-xs text-red-600 hover:underline">删除</button>
              </span>
            </div>
          ))}
          {addresses.length === 0 && <div className="text-sm text-gray-400">暂无地址</div>}
        </div>
        {showAddrForm && (
          <form onSubmit={handleAddAddress} className="grid grid-cols-2 gap-3 bg-gray-50 p-4 rounded-lg">
            <input placeholder="收件人" value={addrRecipient} onChange={(e) => setAddrRecipient(e.target.value)} required className="px-3 py-2 border rounded-lg text-sm" />
            <input placeholder="电话" value={addrPhone} onChange={(e) => setAddrPhone(e.target.value)} required className="px-3 py-2 border rounded-lg text-sm" />
            <input placeholder="详细地址" value={addrLine1} onChange={(e) => setAddrLine1(e.target.value)} required className="col-span-2 px-3 py-2 border rounded-lg text-sm" />
            <input placeholder="城市" value={addrCity} onChange={(e) => setAddrCity(e.target.value)} required className="px-3 py-2 border rounded-lg text-sm" />
            <input placeholder="邮编" value={addrPostal} onChange={(e) => setAddrPostal(e.target.value)} required className="px-3 py-2 border rounded-lg text-sm" />
            <input placeholder="区域代码（可选）" value={addrRegion} onChange={(e) => setAddrRegion(e.target.value)} className="col-span-2 px-3 py-2 border rounded-lg text-sm" />
            <button type="submit" className="col-span-2 px-4 py-2 bg-emerald-600 text-white rounded-lg text-sm hover:bg-emerald-700">保存地址</button>
          </form>
        )}
      </div>

      {/* Order History */}
      <div className="bg-white rounded-xl border border-gray-200 p-5">
        <h2 className="font-semibold mb-3">订单历史</h2>
        <div className="space-y-3">
          {orders.map((o) => (
            <div key={o.id} className="bg-gray-50 rounded-lg px-3 py-2 text-sm">
              <div className="flex items-center justify-between">
                <span className="font-mono text-xs text-gray-500">{o.id.slice(0, 12)}...</span>
                <span className="text-xs px-2 py-0.5 rounded bg-gray-100">{o.status}</span>
                <span className="font-medium">{formatMoney(o.grand_total_cents, o.currency)}</span>
              </div>
              <div className="text-xs text-gray-400 mt-1 mb-2">{new Date(o.created_at).toLocaleString('zh-CN')}</div>

              {/* 履约 SLA：时效承诺 + 超时赔付 (P1 冲刺) */}
              {o.promised_delivery_at && (
                <div className="flex flex-wrap gap-2 items-center mb-2">
                  <span className="text-xs px-2 py-0.5 rounded bg-blue-50 text-blue-700">
                    承诺送达 {new Date(o.promised_delivery_at).toLocaleString('zh-CN')}
                  </span>
                  {o.sla_compensated_at && (
                    <span className="text-xs px-2 py-0.5 rounded bg-amber-50 text-amber-700">
                      ⏱ 履约超时，已赔付 4 元算力金
                    </span>
                  )}
                </div>
              )}

              <div className="space-y-1.5 border-t border-gray-200 pt-2">
                {(o.line_items || []).map((li) => {
                  const result = claimResults[li.id];
                  return (
                    <div key={li.id} className="text-xs">
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-gray-600">
                          {li.product_title || li.variant_sku || '商品'} × {li.quantity}
                        </span>
                        {li.batch_id && !result && (
                          <button
                            onClick={() => startClaim(li.id)}
                            className="text-red-600 hover:underline shrink-0"
                          >
                            报告问题
                          </button>
                        )}
                      </div>

                      {claimingLineItemId === li.id && li.batch_id && (
                        <div className="mt-1.5 p-2 bg-white rounded border border-gray-200 space-y-1.5">
                          <input
                            value={evidenceUrl}
                            onChange={(e) => setEvidenceUrl(e.target.value)}
                            placeholder="证据图片链接（演示环境暂不支持真实上传/AI 识别）"
                            className="w-full px-2 py-1 border rounded text-xs"
                          />
                          <div className="flex gap-2">
                            <button
                              disabled={submittingClaim}
                              onClick={() => handleSubmitClaim(o.id, li.batch_id as string, li.id)}
                              className="px-3 py-1 bg-red-600 text-white rounded text-xs hover:bg-red-700 disabled:opacity-50"
                            >
                              {submittingClaim ? '提交中...' : '提交报案'}
                            </button>
                            <button
                              onClick={() => setClaimingLineItemId(null)}
                              className="px-3 py-1 text-gray-500 hover:text-gray-700 text-xs"
                            >
                              取消
                            </button>
                          </div>
                        </div>
                      )}

                      {result && (
                        'error' in result ? (
                          <div className="mt-1 text-red-600">提交失败：{result.error}</div>
                        ) : (
                          <div className={`mt-1 px-2 py-1 rounded ${claimDecisionCopy(result.decision).className}`}>
                            {claimDecisionCopy(result.decision).detail}
                            {result.refund_amount_cents ? ` 退款 ${formatMoney(result.refund_amount_cents, o.currency)}。` : ''}
                          </div>
                        )
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          ))}
          {orders.length === 0 && <div className="text-sm text-gray-400">暂无订单</div>}
        </div>
      </div>

      {/* RMA claim history */}
      <div className="bg-white rounded-xl border border-gray-200 p-5">
        <h2 className="font-semibold mb-3">我的售后</h2>
        <div className="space-y-2">
          {claims.map((c) => {
            const copy = claimDecisionCopy(c.decision);
            return (
              <div key={c.id} className="bg-gray-50 rounded-lg px-3 py-2 text-sm">
                <div className="flex items-center justify-between">
                  <span className="font-mono text-xs text-gray-500">订单 {c.order_id.slice(0, 12)}...</span>
                  <span className={`text-xs px-2 py-0.5 rounded ${copy.className}`}>{copy.label}</span>
                </div>
                <div className="text-xs text-gray-500 mt-1">{copy.detail}</div>
                {c.refund_amount_cents ? (
                  <div className="text-xs text-emerald-600 mt-0.5">退款 {formatMoney(c.refund_amount_cents)}</div>
                ) : null}
                <div className="text-xs text-gray-400 mt-1">{new Date(c.created_at).toLocaleString('zh-CN')}</div>
              </div>
            );
          })}
          {claims.length === 0 && <div className="text-sm text-gray-400">暂无售后记录</div>}
        </div>
      </div>
    </div>
  );
}
