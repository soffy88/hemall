'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { api } from '@/lib/api-client';
import { formatMoney } from '@/lib/format';
import { getCustomerAuth, initCustomerAuth, customerLogout } from '@/lib/customer-auth-store';
import { useToast } from '@/components/Toast';
import type {
  Customer,
  CustomerAddress,
  CustomerPreorder,
  Order,
  RmaClaim,
  RefundReason,
  RefundRequest,
  RefundType,
  SubmitClaimResult,
} from '@/types/api';
import { errMessage } from '@/lib/errors';

const REFUND_REASON_LABEL: Record<RefundReason, string> = {
  buyer_remorse: '不想要了/后悔购买',
  product_issue: '商品质量问题',
  not_as_described: '与描述不符',
  shipping_delay: '物流太慢',
  other: '其他',
};

const REFUND_STATUS_LABEL: Record<RefundRequest['status'], string> = {
  submitted: '待审核',
  approved: '已通过，退款中',
  refunding: '退款处理中',
  succeeded: '退款成功',
  failed: '退款失败，待重试',
  rejected: '已驳回',
};

const REFUND_STATUS_STYLE: Record<RefundRequest['status'], string> = {
  submitted: 'bg-amber-100 text-amber-700',
  approved: 'bg-blue-100 text-blue-700',
  refunding: 'bg-blue-100 text-blue-700',
  succeeded: 'bg-emerald-100 text-emerald-700',
  failed: 'bg-red-100 text-red-700',
  rejected: 'bg-red-100 text-red-700',
};

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
  const [preorders, setPreorders] = useState<CustomerPreorder[]>([]);
  const [claims, setClaims] = useState<RmaClaim[]>([]);
  const [refunds, setRefunds] = useState<RefundRequest[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  // 申请售后（退款状态机入口）——一次只展开一个订单行的表单。
  const [refundingLineItemId, setRefundingLineItemId] = useState<string | null>(null);
  const [refundType, setRefundType] = useState<RefundType>('refund_only');
  const [refundReason, setRefundReason] = useState<RefundReason>('buyer_remorse');
  const [refundDetail, setRefundDetail] = useState('');
  const [refundAmountYuan, setRefundAmountYuan] = useState('');
  const [submittingRefund, setSubmittingRefund] = useState(false);

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
    // cookie 会话下没有可读 token：先向后端拉一次会话，未登录再跳转。
    await initCustomerAuth();
    if (!getCustomerAuth()) {
      router.push('/shop/login');
      return;
    }
    setLoading(true);
    try {
      const [p, a, o, po, c, r] = await Promise.all([
        api.customerMe(),
        api.customerMyAddresses(),
        api.customerMyOrders(),
        api.customerMyPreorders(),
        api.customerMyClaims(),
        api.customerMyRefunds(),
      ]);
      setProfile(p);
      setName(p.name || '');
      setPhone(p.phone || '');
      setAddresses(a);
      setOrders(o);
      setPreorders(po);
      setClaims(c);
      setRefunds(r);
    } catch (e) {
      setError(errMessage(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, []);

  async function handleLogout() {
    await customerLogout();
    router.push('/shop');
  }

  async function handleSaveProfile(e: React.FormEvent) {
    e.preventDefault();
    try {
      await api.customerUpdateMe({ name, phone });
      setEditingProfile(false);
      load();
    } catch (e) {
      showToast(errMessage(e), 'error');
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
    } catch (e) {
      showToast(errMessage(e), 'error');
    }
  }

  async function handleSetDefault(addressId: string) {
    try {
      await api.customerUpdateAddress(addressId, { is_default: true });
      load();
    } catch (e) {
      showToast(errMessage(e), 'error');
    }
  }

  async function handleDeleteAddress(addressId: string) {
    if (!confirm('确认删除此地址？')) return;
    try {
      await api.customerDeleteAddress(addressId);
      load();
    } catch (e) {
      showToast(errMessage(e), 'error');
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
    } catch (e) {
      setClaimResults((prev) => ({ ...prev, [lineItemId]: { error: errMessage(e) } }));
    } finally {
      setSubmittingClaim(false);
    }
  }

  function startRefund(lineItemId: string, defaultCents: number) {
    setRefundingLineItemId(lineItemId);
    setRefundType('refund_only');
    setRefundReason('buyer_remorse');
    setRefundDetail('');
    setRefundAmountYuan((defaultCents / 100).toFixed(2));
  }

  async function handleSubmitRefund(orderId: string, lineItemId: string) {
    const cents = Math.round(parseFloat(refundAmountYuan) * 100);
    if (!Number.isFinite(cents) || cents <= 0) {
      showToast('请填写正确的退款金额', 'error');
      return;
    }
    setSubmittingRefund(true);
    try {
      await api.createRefund({
        order_id: orderId,
        order_line_id: lineItemId,
        type: refundType,
        reason: refundReason,
        reason_detail: refundDetail.trim() || null,
        requested_amount: cents,
      });
      setRefundingLineItemId(null);
      showToast('售后申请已提交，等待审核', 'info');
      const r = await api.customerMyRefunds();
      setRefunds(r);
    } catch (e) {
      showToast(errMessage(e), 'error');
    } finally {
      setSubmittingRefund(false);
    }
  }

  if (loading) return <div className="text-center py-12 text-gray-500">加载中...</div>;  if (error) return <div className="text-center py-12 text-red-600">{error}</div>;
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
                  const lineRefund = refunds.find((r) => r.order_line_id === li.id);
                  return (
                    <div key={li.id} className="text-xs">
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-gray-600">
                          {li.product_title || li.variant_sku || '商品'} × {li.quantity}
                          {(li.preorder_qty ?? 0) > 0 && (
                            <span className="ml-2 px-1.5 py-0.5 rounded bg-amber-100 text-amber-700">
                              预订 ×{li.preorder_qty} 待配货
                            </span>
                          )}
                        </span>
                        <span className="flex gap-2 shrink-0">
                          {li.batch_id && !result && (
                            <button
                              onClick={() => startClaim(li.id)}
                              className="text-red-600 hover:underline shrink-0"
                            >
                              报告问题
                            </button>
                          )}
                          {!lineRefund && refundingLineItemId !== li.id && (
                            <button
                              onClick={() => startRefund(li.id, li.line_total_cents)}
                              className="text-amber-600 hover:underline shrink-0"
                            >
                              申请售后
                            </button>
                          )}
                        </span>
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

                      {refundingLineItemId === li.id && (
                        <div className="mt-1.5 p-2 bg-white rounded border border-gray-200 space-y-1.5">
                          <select
                            value={refundType}
                            onChange={(e) => setRefundType(e.target.value as RefundType)}
                            className="w-full px-2 py-1 border rounded text-xs"
                          >
                            <option value="refund_only">仅退款（无需寄回）</option>
                            <option value="return_refund">退货退款（需寄回商品）</option>
                          </select>
                          <select
                            value={refundReason}
                            onChange={(e) => setRefundReason(e.target.value as RefundReason)}
                            className="w-full px-2 py-1 border rounded text-xs"
                          >
                            {(Object.keys(REFUND_REASON_LABEL) as RefundReason[]).map((r) => (
                              <option key={r} value={r}>{REFUND_REASON_LABEL[r]}</option>
                            ))}
                          </select>
                          <input
                            value={refundDetail}
                            onChange={(e) => setRefundDetail(e.target.value)}
                            placeholder="补充说明（可选）"
                            className="w-full px-2 py-1 border rounded text-xs"
                          />
                          <div className="flex items-center gap-2">
                            <input
                              type="number"
                              min="0.01"
                              step="0.01"
                              value={refundAmountYuan}
                              onChange={(e) => setRefundAmountYuan(e.target.value)}
                              aria-label="退款金额（元）"
                              className="w-28 px-2 py-1 border rounded text-xs"
                            />
                            <span className="text-[10px] text-gray-400">
                              元 · 本行最高 {formatMoney(li.line_total_cents, o.currency)}
                            </span>
                          </div>
                          <div className="flex gap-2">
                            <button
                              disabled={submittingRefund}
                              onClick={() => handleSubmitRefund(o.id, li.id)}
                              className="px-3 py-1 bg-amber-600 text-white rounded text-xs hover:bg-amber-700 disabled:opacity-50"
                            >
                              {submittingRefund ? '提交中...' : '提交售后申请'}
                            </button>
                            <button
                              onClick={() => setRefundingLineItemId(null)}
                              className="px-3 py-1 text-gray-500 hover:text-gray-700 text-xs"
                            >
                              取消
                            </button>
                          </div>
                        </div>
                      )}

                      {lineRefund && (
                        <div className="mt-1 flex flex-wrap items-center gap-2">
                          <span className={`px-2 py-0.5 rounded ${REFUND_STATUS_STYLE[lineRefund.status]}`}>
                            {REFUND_STATUS_LABEL[lineRefund.status]}
                          </span>
                          <span className="text-gray-500">
                            售后申请 {formatMoney(lineRefund.requested_amount, o.currency)} ·{' '}
                            {REFUND_REASON_LABEL[lineRefund.reason]}
                          </span>
                          {lineRefund.reject_reason && (
                            <span className="text-red-500">驳回原因：{lineRefund.reject_reason}</span>
                          )}
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

      {/* 我的预订：缺货预订的待配货进度 */}
      <div className="bg-white rounded-xl border border-gray-200 p-5">
        <h2 className="font-semibold mb-3">我的预订</h2>
        <div className="space-y-2">
          {preorders.map((po) => (
            <div key={po.line_id} className="flex items-center gap-3 bg-gray-50 rounded-lg px-3 py-2 text-sm">
              {po.image_url ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img src={po.image_url} alt={po.title} className="w-11 h-11 rounded-lg object-cover bg-white shrink-0" />
              ) : (
                <div className="w-11 h-11 rounded-lg flex items-center justify-center shrink-0 bg-gray-100">
                  <span className="text-xl">📦</span>
                </div>
              )}
              <div className="flex-1 min-w-0">
                <div className="truncate text-gray-800">{po.title}</div>
                <div className="text-xs text-gray-400 mt-0.5">
                  {new Date(po.created_at).toLocaleString('zh-CN')} · 订单状态 {po.order_status}
                </div>
              </div>
              <span className="text-gray-500 shrink-0">× {po.quantity}</span>
              {po.preorder_qty > 0 ? (
                <span className="text-xs px-2 py-0.5 rounded bg-amber-100 text-amber-700 shrink-0">
                  待配货 ×{po.preorder_qty}
                </span>
              ) : (
                <span className="text-xs px-2 py-0.5 rounded bg-emerald-100 text-emerald-700 shrink-0">已配货</span>
              )}
            </div>
          ))}
          {preorders.length === 0 && <div className="text-sm text-gray-400">暂无预订，缺货商品也可直接下单预订。</div>}
        </div>
      </div>

      {/* RMA claim history + 退款申请记录 */}
      <div className="bg-white rounded-xl border border-gray-200 p-5">
        <h2 className="font-semibold mb-3">我的售后</h2>

        {refunds.length > 0 && (
          <div className="space-y-2 mb-4">
            <div className="text-xs font-medium text-gray-500">退款申请</div>
            {refunds.map((r) => (
              <div key={r.id} className="bg-gray-50 rounded-lg px-3 py-2 text-sm">
                <div className="flex items-center justify-between">
                  <span className="font-mono text-xs text-gray-500">订单 {r.order_id.slice(0, 12)}...</span>
                  <span className={`text-xs px-2 py-0.5 rounded ${REFUND_STATUS_STYLE[r.status]}`}>
                    {REFUND_STATUS_LABEL[r.status]}
                  </span>
                </div>
                <div className="text-xs text-gray-500 mt-1">
                  {r.type === 'return_refund' ? '退货退款' : '仅退款'} · {REFUND_REASON_LABEL[r.reason]}
                  {r.reason_detail ? ` · ${r.reason_detail}` : ''}
                </div>
                <div className="text-xs mt-0.5">
                  <span className="text-gray-500">申请 {formatMoney(r.requested_amount)}</span>
                  {r.approved_amount != null && (
                    <span className="ml-2 text-emerald-600">批准 {formatMoney(r.approved_amount)}</span>
                  )}
                  {r.actual_refunded_amount != null && (
                    <span className="ml-2 text-emerald-600">已退 {formatMoney(r.actual_refunded_amount)}</span>
                  )}
                </div>
                {r.reject_reason && (
                  <div className="text-xs text-red-500 mt-0.5">驳回原因：{r.reject_reason}</div>
                )}
                <div className="text-xs text-gray-400 mt-1">{new Date(r.created_at).toLocaleString('zh-CN')}</div>
              </div>
            ))}
          </div>
        )}

        <div className="text-xs font-medium text-gray-500 mb-2">极速报案（RMA）</div>
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
            {claims.length === 0 && refunds.length === 0 && (
              <div className="text-sm text-gray-400">暂无售后记录</div>
            )}
            {claims.length === 0 && refunds.length > 0 && (
              <div className="text-sm text-gray-400">暂无报案记录</div>
            )}
        </div>
      </div>
    </div>
  );
}
