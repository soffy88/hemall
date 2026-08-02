'use client';

import { useEffect, useState } from 'react';
import { api } from '@/lib/api-client';
import { formatMoney } from '@/lib/format';
import { ActionCard, OpsSection } from '@/components/ops/OpsActionCard';
import type { Order, ReturnRequest, Swap, Claim } from '@/types/api';

/** 客诉仲裁记录 —— VLM 判损 + 裁决结果，运营核查用，带手动刷新。 */
function RmaClaimsTable({ refreshKey }: { refreshKey: number }) {
  const [rows, setRows] = useState<Awaited<ReturnType<typeof api.adminListClearnodeRmaClaims>>>([]);
  const [loading, setLoading] = useState(true);

  async function load() {
    setLoading(true);
    try {
      setRows(await api.adminListClearnodeRmaClaims());
    } catch {
      // 列表加载失败不阻断整个页面，留空表即可
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshKey]);

  return (
    <div className="overflow-x-auto mb-6">
      <table className="w-full text-xs bg-white rounded-xl overflow-hidden border border-gray-200">
        <thead className="bg-gray-50 text-left">
          <tr>
            <th className="px-3 py-2 font-medium text-gray-600">claim_id</th>
            <th className="px-3 py-2 font-medium text-gray-600">order_id</th>
            <th className="px-3 py-2 font-medium text-gray-600">vlm_damage_type</th>
            <th className="px-3 py-2 font-medium text-gray-600">decision</th>
            <th className="px-3 py-2 font-medium text-gray-600">liable_party</th>
            <th className="px-3 py-2 font-medium text-gray-600">created_at</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100">
          {rows.map((c) => (
            <tr key={c.id} className="hover:bg-gray-50">
              <td className="px-3 py-2 font-mono">{c.id}</td>
              <td className="px-3 py-2 font-mono">{c.order_id}</td>
              <td className="px-3 py-2">{c.vlm_damage_type ?? '—'}</td>
              <td className="px-3 py-2">
                <span
                  className={`px-1.5 py-0.5 rounded ${
                    c.decision === 'instant_refund'
                      ? 'bg-emerald-100 text-emerald-700'
                      : c.decision === 'rejected'
                        ? 'bg-red-100 text-red-700'
                        : 'bg-amber-100 text-amber-700'
                  }`}
                >
                  {c.decision ?? 'pending'}
                </span>
              </td>
              <td className="px-3 py-2">{c.liable_party ?? '—'}</td>
              <td className="px-3 py-2 text-gray-500">{new Date(c.created_at).toLocaleString('zh-CN')}</td>
            </tr>
          ))}
          {!loading && rows.length === 0 && (
            <tr><td colSpan={6} className="px-3 py-6 text-center text-gray-400">暂无客诉记录</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

function parseNewItems(text: string): { batch_id: string; quantity: number }[] {
  return text
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
    .map((pair) => {
      const [batch_id, qty] = pair.split(':').map((x) => x.trim());
      return { batch_id, quantity: Number(qty) || 1 };
    });
}

export default function AftersalesPage() {
  const [orders, setOrders] = useState<Order[]>([]);
  const [orderId, setOrderId] = useState('');
  const [order, setOrder] = useState<Order | null>(null);
  const fmt = (cents: number | null) => formatMoney(cents, order?.currency);
  const [returns, setReturns] = useState<ReturnRequest[]>([]);
  const [swaps, setSwaps] = useState<Swap[]>([]);
  const [claims, setClaims] = useState<Claim[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const [returnQty, setReturnQty] = useState<Record<string, number>>({});
  const [swapReturnQty, setSwapReturnQty] = useState<Record<string, number>>({});
  const [swapNewItems, setSwapNewItems] = useState('');
  const [claimType, setClaimType] = useState('refund');
  const [claimQty, setClaimQty] = useState<Record<string, number>>({});

  // 透仓运维（原 /admin/clearnode 页面搬迁过来的裸操作卡片）用的独立 ctx——
  // 跟上面订单查询/退换货表单是两套不相干的状态，不复用。
  const [opsCtx] = useState<Record<string, string>>({});
  const [opsListRefreshKey, setOpsListRefreshKey] = useState(0);
  const [claimNewItems, setClaimNewItems] = useState('');

  useEffect(() => {
    api.adminListOrders({ limit: 100 }).then(setOrders).catch((e: any) => setError(e.message));
  }, []);

  async function loadOrder() {
    if (!orderId) return;
    setLoading(true);
    try {
      const [o, r, s, c] = await Promise.all([
        api.adminGetOrder(orderId),
        api.adminListReturns(orderId),
        api.adminListSwaps(orderId),
        api.adminListClaims(orderId),
      ]);
      setOrder(o);
      setReturns(r);
      setSwaps(s);
      setClaims(c);
      setReturnQty({});
      setSwapReturnQty({});
      setClaimQty({});
    } catch (e: any) {
      alert(e.message);
    } finally {
      setLoading(false);
    }
  }

  async function handleCreateReturn() {
    if (!order) return;
    const items = Object.entries(returnQty).filter(([, q]) => q > 0).map(([id, q]) => ({ order_line_item_id: id, quantity: q }));
    if (items.length === 0) { alert('请至少选择一项商品数量'); return; }
    try {
      const res = await api.createReturnRequest({ order_id: order.id, items });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '发起退货失败');
      loadOrder();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleReceiveReturn(id: string) {
    try {
      const res = await api.receiveReturn({ return_request_id: id });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '收货失败');
      loadOrder();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleCancelReturn(id: string) {
    if (!confirm('确认取消此退货申请？')) return;
    try {
      const res = await api.cancelReturn({ return_request_id: id });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '取消失败');
      loadOrder();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleCreateSwap() {
    if (!order) return;
    const return_items = Object.entries(swapReturnQty).filter(([, q]) => q > 0).map(([id, q]) => ({ order_line_item_id: id, quantity: q }));
    const new_items = parseNewItems(swapNewItems);
    if (return_items.length === 0 || new_items.length === 0) { alert('请选择退回商品并填写换入商品 (batch_id:数量)'); return; }
    try {
      const res = await api.createSwap({ order_id: order.id, return_items, new_items });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '发起换货失败');
      setSwapNewItems('');
      loadOrder();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleCancelSwap(id: string) {
    if (!confirm('确认取消此换货？')) return;
    try {
      const res = await api.cancelSwap({ swap_id: id });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '取消失败');
      loadOrder();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleFulfillSwap(id: string) {
    try {
      const res = await api.fulfillSwap({ swap_id: id });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '执行换货失败');
      loadOrder();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleProcessSwapPayment(id: string) {
    try {
      const res = await api.processSwapPayment({ swap_id: id });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '处理差价失败');
      loadOrder();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleCreateClaim() {
    if (!order) return;
    const items = Object.entries(claimQty).filter(([, q]) => q > 0).map(([id, q]) => ({ order_line_item_id: id, quantity: q }));
    if (items.length === 0) { alert('请至少选择一项商品数量'); return; }
    const new_items = claimType === 'replace' ? parseNewItems(claimNewItems) : undefined;
    try {
      const res = await api.createClaim({ order_id: order.id, claim_type: claimType, items, new_items });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '发起客诉失败');
      setClaimNewItems('');
      loadOrder();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleCancelClaim(id: string) {
    if (!confirm('确认取消此客诉？')) return;
    try {
      const res = await api.cancelClaim({ claim_id: id });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '取消失败');
      loadOrder();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleFulfillClaim(id: string) {
    try {
      const res = await api.fulfillClaim({ claim_id: id });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '处理客诉失败');
      loadOrder();
    } catch (e: any) {
      alert(e.message);
    }
  }

  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">售后管理</h1>
      {error && <div className="mb-4 text-red-600">{error}</div>}

      <div className="flex gap-2 items-end mb-6">
        <div>
          <label className="text-xs text-gray-500">选择订单</label>
          <select value={orderId} onChange={(e) => setOrderId(e.target.value)} className="px-3 py-2 border rounded-lg text-sm w-96">
            <option value="">选择订单...</option>
            {orders.map((o) => (
              <option key={o.id} value={o.id}>{o.id.slice(0, 12)}... · {o.status} · {formatMoney(o.grand_total_cents, o.currency)}</option>
            ))}
          </select>
        </div>
        <button onClick={loadOrder} className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm hover:bg-blue-700">加载</button>
      </div>

      {loading && <div className="text-gray-500">加载中...</div>}

      {order && (
        <>
          <div className="mb-6 text-sm text-gray-600">
            订单 <span className="font-mono">{order.id}</span> · {order.status} · {fmt(order.grand_total_cents)}
          </div>

          {/* Returns */}
          <div className="mb-8">
            <h2 className="text-lg font-semibold mb-3">退货</h2>
            <div className="mb-3 p-4 bg-white rounded-xl border border-gray-200">
              <div className="text-xs text-gray-500 mb-2">选择退货商品及数量</div>
              <div className="space-y-1 mb-3">
                {(order.line_items || []).map((li) => (
                  <div key={li.id} className="flex items-center gap-3 text-sm">
                    <span className="flex-1">{li.product_title} {li.variant_sku} (共 {li.quantity})</span>
                    <input
                      type="number" min={0} max={li.quantity} value={returnQty[li.id] || 0}
                      onChange={(e) => setReturnQty({ ...returnQty, [li.id]: +e.target.value })}
                      className="w-20 px-2 py-1 border rounded text-sm"
                    />
                  </div>
                ))}
              </div>
              <button onClick={handleCreateReturn} className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm hover:bg-blue-700">发起退货</button>
            </div>
            <table className="w-full text-sm bg-white rounded-xl overflow-hidden border border-gray-200">
              <thead className="bg-gray-50 text-left">
                <tr>
                  <th className="px-4 py-2 font-medium text-gray-600">ID</th>
                  <th className="px-4 py-2 font-medium text-gray-600">状态</th>
                  <th className="px-4 py-2 font-medium text-gray-600">退款金额</th>
                  <th className="px-4 py-2 font-medium text-gray-600">操作</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {returns.map((r) => (
                  <tr key={r.id}>
                    <td className="px-4 py-2 font-mono text-xs">{r.id.slice(0, 12)}...</td>
                    <td className="px-4 py-2">{r.status}</td>
                    <td className="px-4 py-2">{fmt(r.refund_amount_cents)}</td>
                    <td className="px-4 py-2">
                      {r.status === 'requested' && (
                        <div className="flex gap-2">
                          <button onClick={() => handleReceiveReturn(r.id)} className="text-xs text-emerald-600 hover:underline">收货并退款</button>
                          <button onClick={() => handleCancelReturn(r.id)} className="text-xs text-red-600 hover:underline">取消</button>
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
                {returns.length === 0 && <tr><td colSpan={4} className="px-4 py-6 text-center text-gray-400">暂无退货记录</td></tr>}
              </tbody>
            </table>
          </div>

          {/* Swaps */}
          <div className="mb-8">
            <h2 className="text-lg font-semibold mb-3">换货</h2>
            <div className="mb-3 p-4 bg-white rounded-xl border border-gray-200 space-y-3">
              <div className="text-xs text-gray-500">选择退回商品及数量</div>
              <div className="space-y-1">
                {(order.line_items || []).map((li) => (
                  <div key={li.id} className="flex items-center gap-3 text-sm">
                    <span className="flex-1">{li.product_title} {li.variant_sku} (共 {li.quantity})</span>
                    <input
                      type="number" min={0} max={li.quantity} value={swapReturnQty[li.id] || 0}
                      onChange={(e) => setSwapReturnQty({ ...swapReturnQty, [li.id]: +e.target.value })}
                      className="w-20 px-2 py-1 border rounded text-sm"
                    />
                  </div>
                ))}
              </div>
              <div>
                <label className="text-xs text-gray-500">换入商品（batch_id:数量，逗号分隔）</label>
                <input value={swapNewItems} onChange={(e) => setSwapNewItems(e.target.value)} placeholder="019f...:1, 019f...:2" className="w-full px-3 py-2 border rounded-lg text-sm" />
              </div>
              <button onClick={handleCreateSwap} className="px-4 py-2 bg-indigo-600 text-white rounded-lg text-sm hover:bg-indigo-700">发起换货</button>
            </div>
            <table className="w-full text-sm bg-white rounded-xl overflow-hidden border border-gray-200">
              <thead className="bg-gray-50 text-left">
                <tr>
                  <th className="px-4 py-2 font-medium text-gray-600">ID</th>
                  <th className="px-4 py-2 font-medium text-gray-600">状态</th>
                  <th className="px-4 py-2 font-medium text-gray-600">差价</th>
                  <th className="px-4 py-2 font-medium text-gray-600">支付状态</th>
                  <th className="px-4 py-2 font-medium text-gray-600">操作</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {swaps.map((s) => (
                  <tr key={s.id}>
                    <td className="px-4 py-2 font-mono text-xs">{s.id.slice(0, 12)}...</td>
                    <td className="px-4 py-2">{s.status}</td>
                    <td className="px-4 py-2">{fmt(s.price_difference_cents)}</td>
                    <td className="px-4 py-2">{s.payment_status}</td>
                    <td className="px-4 py-2">
                      <div className="flex gap-2">
                        {s.payment_status !== 'paid' && s.price_difference_cents !== 0 && (
                          <button onClick={() => handleProcessSwapPayment(s.id)} className="text-xs text-emerald-600 hover:underline">处理差价</button>
                        )}
                        {s.status === 'requested' && (
                          <>
                            <button onClick={() => handleFulfillSwap(s.id)} className="text-xs text-blue-600 hover:underline">执行换货</button>
                            <button onClick={() => handleCancelSwap(s.id)} className="text-xs text-red-600 hover:underline">取消</button>
                          </>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
                {swaps.length === 0 && <tr><td colSpan={5} className="px-4 py-6 text-center text-gray-400">暂无换货记录</td></tr>}
              </tbody>
            </table>
          </div>

          {/* Claims */}
          <div>
            <h2 className="text-lg font-semibold mb-3">客诉理赔</h2>
            <div className="mb-3 p-4 bg-white rounded-xl border border-gray-200 space-y-3">
              <div>
                <label className="text-xs text-gray-500">类型</label>
                <select value={claimType} onChange={(e) => setClaimType(e.target.value)} className="px-3 py-2 border rounded-lg text-sm">
                  <option value="refund">退款</option>
                  <option value="replace">换新</option>
                </select>
              </div>
              <div className="text-xs text-gray-500">选择涉及商品及数量</div>
              <div className="space-y-1">
                {(order.line_items || []).map((li) => (
                  <div key={li.id} className="flex items-center gap-3 text-sm">
                    <span className="flex-1">{li.product_title} {li.variant_sku} (共 {li.quantity})</span>
                    <input
                      type="number" min={0} max={li.quantity} value={claimQty[li.id] || 0}
                      onChange={(e) => setClaimQty({ ...claimQty, [li.id]: +e.target.value })}
                      className="w-20 px-2 py-1 border rounded text-sm"
                    />
                  </div>
                ))}
              </div>
              {claimType === 'replace' && (
                <div>
                  <label className="text-xs text-gray-500">换新商品（batch_id:数量，逗号分隔）</label>
                  <input value={claimNewItems} onChange={(e) => setClaimNewItems(e.target.value)} placeholder="019f...:1" className="w-full px-3 py-2 border rounded-lg text-sm" />
                </div>
              )}
              <button onClick={handleCreateClaim} className="px-4 py-2 bg-amber-600 text-white rounded-lg text-sm hover:bg-amber-700">发起客诉</button>
            </div>
            <table className="w-full text-sm bg-white rounded-xl overflow-hidden border border-gray-200">
              <thead className="bg-gray-50 text-left">
                <tr>
                  <th className="px-4 py-2 font-medium text-gray-600">ID</th>
                  <th className="px-4 py-2 font-medium text-gray-600">类型</th>
                  <th className="px-4 py-2 font-medium text-gray-600">状态</th>
                  <th className="px-4 py-2 font-medium text-gray-600">退款金额</th>
                  <th className="px-4 py-2 font-medium text-gray-600">操作</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {claims.map((c) => (
                  <tr key={c.id}>
                    <td className="px-4 py-2 font-mono text-xs">{c.id.slice(0, 12)}...</td>
                    <td className="px-4 py-2">{c.claim_type}</td>
                    <td className="px-4 py-2">{c.status}</td>
                    <td className="px-4 py-2">{fmt(c.refund_amount_cents)}</td>
                    <td className="px-4 py-2">
                      {c.status === 'pending' && (
                        <div className="flex gap-2">
                          <button onClick={() => handleFulfillClaim(c.id)} className="text-xs text-emerald-600 hover:underline">处理</button>
                          <button onClick={() => handleCancelClaim(c.id)} className="text-xs text-red-600 hover:underline">取消</button>
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
                {claims.length === 0 && <tr><td colSpan={5} className="px-4 py-6 text-center text-gray-400">暂无客诉记录</td></tr>}
              </tbody>
            </table>
          </div>
        </>
      )}

      <div className="mt-10 mb-3 flex items-center justify-between">
        <h2 className="text-lg font-semibold">透仓运维 — 押金/回收桶/RMA 仲裁</h2>
        <button
          onClick={() => setOpsListRefreshKey((k) => k + 1)}
          className="text-xs text-blue-600 hover:underline"
        >
          刷新客诉记录
        </button>
      </div>
      <RmaClaimsTable refreshKey={opsListRefreshKey} />
      <OpsSection title="押金 / 回收桶 / 全自动仲裁">
        <ActionCard
          title="循环筐押金 tote_deposit_and_refund"
          badge="public"
          desc="扣 10 元押金 (action=charge) / 归还秒退 (action=refund)。"
          fields={[
            { key: 'tote_id', label: 'tote_id' },
            { key: 'action', label: 'action', placeholder: 'charge | refund' },
          ]}
          defaults={opsCtx}
          onSubmit={(v) => api.clearnodeToteDepositAndRefund(v as any)}
        />
        <ActionCard
          title="回收桶退货 process_drop_return"
          badge="public"
          desc="用户扫回收桶投掷坏果，直接秒退，零人工废话。"
          fields={[
            { key: 'order_line_item_id', label: 'order_line_item_id' },
            { key: 'reason', label: 'reason (默认 quality)' },
          ]}
          defaults={opsCtx}
          onSubmit={(v) => api.clearnodeProcessDropReturn(v as any)}
        />
        <ActionCard
          title="🔥 一键提交客诉 submit_rma_claim (全自动仲裁, ~2 秒)"
          badge="public"
          desc="拉起 autonomous_triage_engine：VLM 判损 → 信誉裁决 → 退款/责任方扣款，全程自动。ManualVLMProvider 默认判'完好无损'会被拒赔，这是预期行为 (没接真实视觉模型)。"
          fields={[
            { key: 'order_id', label: 'order_id' },
            { key: 'batch_id', label: 'batch_id' },
            { key: 'user_id', label: 'user_id' },
            { key: 'evidence_image_url', label: 'evidence_image_url' },
            { key: 'user_trust_score', label: 'user_trust_score (0-100)', type: 'number' },
            { key: 'route_risk', label: 'route_risk (0-1, 默认 0)', type: 'number' },
          ]}
          defaults={opsCtx}
          onSubmit={(v) => api.clearnodeSubmitRmaClaim(v as any)}
          onResult={() => setOpsListRefreshKey((k) => k + 1)}
        />
        <ActionCard
          title="轻量客诉 process_credit_gated_rma_workflow"
          badge="public"
          desc="不需要视觉证据的客诉 (缺件/发错/不想要了)，只走信誉裁决，instant 直接秒退。"
          fields={[
            { key: 'order_id', label: 'order_id' },
            { key: 'batch_id', label: 'batch_id' },
            { key: 'user_id', label: 'user_id' },
            { key: 'user_trust_score', label: 'user_trust_score (0-100)', type: 'number' },
            { key: 'batch_anomaly_rate', label: 'batch_anomaly_rate (0-1, 默认 0)', type: 'number' },
            { key: 'route_risk', label: 'route_risk (0-1, 默认 0)', type: 'number' },
          ]}
          defaults={opsCtx}
          onSubmit={(v) => api.clearnodeProcessCreditGatedRma(v as any)}
          onResult={() => setOpsListRefreshKey((k) => k + 1)}
        />
        <ActionCard
          title="仲裁执行末端 execute_liability_routing_workflow (手动/覆核)"
          badge="admin"
          desc="人工覆核工具：给定已经算好的 VLM 判损 + 信誉裁决，直接落地执行 (正常应该走上面的一键提交，由引擎自动算好这些参数)。"
          fields={[
            { key: 'order_id', label: 'order_id' },
            { key: 'batch_id', label: 'batch_id' },
            { key: 'user_id', label: 'user_id' },
            { key: 'evidence_image_url', label: 'evidence_image_url' },
            { key: 'vlm_damage_type', label: 'vlm_damage_type', placeholder: 'spoiled | crushed | ...' },
            { key: 'vlm_severity', label: 'vlm_severity (0-1)', type: 'number' },
            { key: 'fraud_probability', label: 'fraud_probability (0-1)', type: 'number' },
            { key: 'credibility_decision', label: 'credibility_decision', placeholder: 'instant | honeypot' },
          ]}
          defaults={opsCtx}
          onSubmit={(v) => api.clearnodeExecuteLiabilityRouting(v as any)}
          onResult={() => setOpsListRefreshKey((k) => k + 1)}
        />
      </OpsSection>
    </div>
  );
}
