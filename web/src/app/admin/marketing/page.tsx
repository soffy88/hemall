'use client';

import { useEffect, useState } from 'react';
import { api } from '@/lib/api-client';
import { formatMoney } from '@/lib/format';
import { ActionCard, OpsSection } from '@/components/ops/OpsActionCard';
import type { Discount, GiftCard } from '@/types/api';

function fmt(cents: number | null, currency?: string): string {
  return formatMoney(cents, currency);
}

function ruleSummary(rule: Discount['rule']): string {
  if (!rule) return '未设置规则';
  if (rule.rule_type === 'percentage') return `${rule.percent ?? '?'}% 折扣`;
  if (rule.rule_type === 'fixed') return `立减 ${fmt(rule.amount_cents)}`;
  if (rule.rule_type === 'free_shipping') return '免运费';
  return rule.rule_type;
}

/** 微信视频号播报记录 —— 文案 + 发布状态，运营核查用，带手动刷新。 */
function BroadcastLogsTable({ refreshKey }: { refreshKey: number }) {
  const [rows, setRows] = useState<Awaited<ReturnType<typeof api.adminListHemallBroadcastLogs>>>([]);
  const [loading, setLoading] = useState(true);

  async function load() {
    setLoading(true);
    try {
      setRows(await api.adminListHemallBroadcastLogs());
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
            <th className="px-3 py-2 font-medium text-gray-600">log_id</th>
            <th className="px-3 py-2 font-medium text-gray-600">batch_id</th>
            <th className="px-3 py-2 font-medium text-gray-600">broadcast_type</th>
            <th className="px-3 py-2 font-medium text-gray-600">llm_copywriting</th>
            <th className="px-3 py-2 font-medium text-gray-600">status</th>
            <th className="px-3 py-2 font-medium text-gray-600">created_at</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100">
          {rows.map((l) => (
            <tr key={l.id} className="hover:bg-gray-50">
              <td className="px-3 py-2 font-mono">{l.id}</td>
              <td className="px-3 py-2 font-mono">{l.batch_id}</td>
              <td className="px-3 py-2">
                <span
                  className={`px-1.5 py-0.5 rounded ${
                    l.broadcast_type === 'clearance'
                      ? 'bg-amber-100 text-amber-700'
                      : 'bg-blue-100 text-blue-700'
                  }`}
                >
                  {l.broadcast_type}
                </span>
              </td>
              <td className="px-3 py-2 max-w-xs truncate" title={l.llm_copywriting}>
                {l.llm_copywriting}
              </td>
              <td className="px-3 py-2">
                <span
                  className={`px-1.5 py-0.5 rounded ${
                    l.status === 'published'
                      ? 'bg-emerald-100 text-emerald-700'
                      : l.status === 'failed'
                        ? 'bg-red-100 text-red-700'
                        : 'bg-gray-100 text-gray-600'
                  }`}
                >
                  {l.status}
                </span>
              </td>
              <td className="px-3 py-2 text-gray-500">{new Date(l.created_at).toLocaleString('zh-CN')}</td>
            </tr>
          ))}
          {!loading && rows.length === 0 && (
            <tr><td colSpan={6} className="px-3 py-6 text-center text-gray-400">暂无播报记录</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

/** 价格基线记录 —— 爬虫/众包小票来源的归一化单价，运营核查定价依据用，带手动刷新。 */
function PriceBenchmarksTable({ refreshKey }: { refreshKey: number }) {
  const [rows, setRows] = useState<Awaited<ReturnType<typeof api.adminListHemallPriceBenchmarks>>>([]);
  const [loading, setLoading] = useState(true);

  async function load() {
    setLoading(true);
    try {
      setRows(await api.adminListHemallPriceBenchmarks());
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
    <div className="overflow-x-auto mb-4">
      <table className="w-full text-xs bg-white rounded-xl overflow-hidden border border-gray-200">
        <thead className="bg-gray-50 text-left">
          <tr>
            <th className="px-3 py-2 font-medium text-gray-600">benchmark_id</th>
            <th className="px-3 py-2 font-medium text-gray-600">variant_id / raw_item_name</th>
            <th className="px-3 py-2 font-medium text-gray-600">source_type</th>
            <th className="px-3 py-2 font-medium text-gray-600">raw_price_cents / raw_unit</th>
            <th className="px-3 py-2 font-medium text-gray-600">normalized_price_per_unit</th>
            <th className="px-3 py-2 font-medium text-gray-600">captured_at</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100">
          {rows.map((b) => (
            <tr key={b.id} className="hover:bg-gray-50">
              <td className="px-3 py-2 font-mono">{b.id}</td>
              <td className="px-3 py-2 font-mono truncate max-w-[10rem]" title={b.variant_id ?? b.raw_item_name ?? ''}>{b.variant_id ?? b.raw_item_name ?? '—'}</td>
              <td className="px-3 py-2">
                <span
                  className={`px-1.5 py-0.5 rounded ${
                    b.source_type === 'spider' ? 'bg-blue-100 text-blue-700' : 'bg-violet-100 text-violet-700'
                  }`}
                >
                  {b.source_type}
                </span>
              </td>
              <td className="px-3 py-2">{b.raw_price_cents} / {b.raw_unit}</td>
              <td className="px-3 py-2">{b.normalized_price_per_unit ?? '—'}</td>
              <td className="px-3 py-2 text-gray-500">{new Date(b.captured_at).toLocaleString('zh-CN')}</td>
            </tr>
          ))}
          {!loading && rows.length === 0 && (
            <tr><td colSpan={6} className="px-3 py-6 text-center text-gray-400">暂无价格基线记录</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

/** 试探单做市日志 —— 试探价 + 观测销售速度 + 状态，运营核查定价引擎用，带手动刷新。 */
function ProbeLogsTable({ refreshKey }: { refreshKey: number }) {
  const [rows, setRows] = useState<Awaited<ReturnType<typeof api.adminListHemallProbeLogs>>>([]);
  const [loading, setLoading] = useState(true);

  async function load() {
    setLoading(true);
    try {
      setRows(await api.adminListHemallProbeLogs());
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
            <th className="px-3 py-2 font-medium text-gray-600">probe_id</th>
            <th className="px-3 py-2 font-medium text-gray-600">batch_id</th>
            <th className="px-3 py-2 font-medium text-gray-600">probe_price_cents</th>
            <th className="px-3 py-2 font-medium text-gray-600">observed_sales_velocity</th>
            <th className="px-3 py-2 font-medium text-gray-600">status</th>
            <th className="px-3 py-2 font-medium text-gray-600">created_at</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100">
          {rows.map((p) => (
            <tr key={p.id} className="hover:bg-gray-50">
              <td className="px-3 py-2 font-mono">{p.id}</td>
              <td className="px-3 py-2 font-mono">{p.batch_id}</td>
              <td className="px-3 py-2">{p.probe_price_cents}</td>
              <td className="px-3 py-2">{p.observed_sales_velocity ?? '—'}</td>
              <td className="px-3 py-2">
                <span
                  className={`px-1.5 py-0.5 rounded ${
                    p.status === 'cleared'
                      ? 'bg-emerald-100 text-emerald-700'
                      : p.status === 'failed'
                        ? 'bg-red-100 text-red-700'
                        : 'bg-gray-100 text-gray-600'
                  }`}
                >
                  {p.status}
                </span>
              </td>
              <td className="px-3 py-2 text-gray-500">{new Date(p.created_at).toLocaleString('zh-CN')}</td>
            </tr>
          ))}
          {!loading && rows.length === 0 && (
            <tr><td colSpan={6} className="px-3 py-6 text-center text-gray-400">暂无试探单记录</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

export default function MarketingPage() {
  // hemall 扩展运维（原独立运维页面搬迁过来的裸操作卡片）用的独立 ctx——
  // 跟上面折扣/礼品卡管理是两套不相干的状态，不复用。
  const [opsCtx, setOpsCtx] = useState<Record<string, string>>({});
  const [opsListRefreshKey, setOpsListRefreshKey] = useState(0);
  function mergeOpsCtx(patch: Record<string, unknown>) {
    const next: Record<string, string> = {};
    for (const [k, v] of Object.entries(patch)) {
      if (typeof v === 'string' || typeof v === 'number') next[k] = String(v);
    }
    setOpsCtx((c) => ({ ...c, ...next }));
  }

  const [discounts, setDiscounts] = useState<Discount[]>([]);
  const [giftCards, setGiftCards] = useState<GiftCard[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const [showDiscountForm, setShowDiscountForm] = useState(false);
  const [discountCode, setDiscountCode] = useState('');

  const [showGiftForm, setShowGiftForm] = useState(false);
  const [gcCode, setGcCode] = useState('');
  const [gcBalance, setGcBalance] = useState(10000);
  const [gcCurrency, setGcCurrency] = useState('CNY');
  const [gcExpires, setGcExpires] = useState('');

  async function load() {
    setLoading(true);
    try {
      const [d, g] = await Promise.all([api.adminListDiscounts(), api.adminListGiftCards()]);
      setDiscounts(d);
      setGiftCards(g);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, []);

  async function handleCreateDiscount(e: React.FormEvent) {
    e.preventDefault();
    try {
      const res = await api.createDiscount({ code: discountCode });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '创建失败');
      setDiscountCode('');
      setShowDiscountForm(false);
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleToggleDiscount(d: Discount) {
    try {
      const newStatus = d.status === 'active' ? 'inactive' : 'active';
      const res = await api.updateDiscount({ discount_id: d.id, status: newStatus });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '更新失败');
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleDeleteDiscount(id: string) {
    if (!confirm('确认删除此折扣？')) return;
    try {
      const res = await api.deleteDiscount({ discount_id: id });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '删除失败');
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleSetRule(d: Discount) {
    const ruleType = prompt('规则类型（fixed / percentage / free_shipping）：', d.rule?.rule_type || 'percentage');
    if (!ruleType) return;
    let amount_cents: number | undefined;
    let percent: number | undefined;
    if (ruleType === 'fixed') {
      const v = prompt('立减金额（分）：', d.rule?.amount_cents != null ? String(d.rule.amount_cents) : '');
      if (v == null) return;
      amount_cents = Number(v);
    } else if (ruleType === 'percentage') {
      const v = prompt('折扣百分比（0-100）：', d.rule?.percent != null ? String(d.rule.percent) : '');
      if (v == null) return;
      percent = Number(v);
    }
    try {
      const res = d.rule
        ? await api.updateDiscountRule({ rule_id: d.rule.id, amount_cents, percent })
        : await api.createDiscountRule({ discount_id: d.id, rule_type: ruleType, amount_cents, percent });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '设置规则失败');
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleAddCondition(d: Discount) {
    const conditionType = prompt('限制类型（product / category / all）：', 'all');
    if (!conditionType) return;
    let target_id: string | undefined;
    if (conditionType !== 'all') {
      target_id = prompt(`${conditionType === 'product' ? '商品' : '分类'} ID：`) || undefined;
    }
    try {
      const res = await api.createDiscountCondition({ discount_id: d.id, condition_type: conditionType, target_id });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '添加条件失败');
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleDeleteCondition(id: string) {
    if (!confirm('删除此限制条件？')) return;
    try {
      const res = await api.deleteDiscountCondition({ condition_id: id });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '删除失败');
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleCreateGiftCard(e: React.FormEvent) {
    e.preventDefault();
    try {
      const res = await api.createGiftCard({
        code: gcCode,
        initial_balance_cents: gcBalance,
        currency: gcCurrency,
        expires_at: gcExpires || undefined,
      });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '创建失败');
      setGcCode('');
      setShowGiftForm(false);
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleToggleGiftCard(g: GiftCard) {
    try {
      const newStatus = g.status === 'active' ? 'inactive' : 'active';
      const res = await api.updateGiftCard({ gift_card_id: g.id, status: newStatus });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '更新失败');
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleDeleteGiftCard(id: string) {
    if (!confirm('确认删除此礼品卡？')) return;
    try {
      const res = await api.deleteGiftCard({ gift_card_id: id });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '删除失败');
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">营销管理</h1>
      {error && <div className="mb-4 text-red-600">{error}</div>}

      {/* Discounts */}
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-lg font-semibold">折扣</h2>
        <button onClick={() => setShowDiscountForm(!showDiscountForm)} className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 text-sm">
          {showDiscountForm ? '取消' : '新建折扣'}
        </button>
      </div>

      {showDiscountForm && (
        <form onSubmit={handleCreateDiscount} className="mb-4 p-4 bg-white rounded-xl border border-gray-200 flex gap-3 items-end">
          <div>
            <label className="text-xs text-gray-500">折扣码</label>
            <input placeholder="SAVE10" value={discountCode} onChange={(e) => setDiscountCode(e.target.value)} required className="px-3 py-2 border rounded-lg text-sm" />
          </div>
          <button type="submit" className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm hover:bg-blue-700">创建</button>
        </form>
      )}

      {loading ? (
        <div className="text-gray-500">加载中...</div>
      ) : (
        <div className="overflow-x-auto mb-8">
          <table className="w-full text-sm bg-white rounded-xl overflow-hidden border border-gray-200">
            <thead className="bg-gray-50 text-left">
              <tr>
                <th className="px-4 py-3 font-medium text-gray-600">折扣码</th>
                <th className="px-4 py-3 font-medium text-gray-600">状态</th>
                <th className="px-4 py-3 font-medium text-gray-600">规则</th>
                <th className="px-4 py-3 font-medium text-gray-600">限制条件</th>
                <th className="px-4 py-3 font-medium text-gray-600">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {discounts.map((d) => (
                <tr key={d.id} className="hover:bg-gray-50">
                  <td className="px-4 py-3 font-medium">{d.code}</td>
                  <td className="px-4 py-3">
                    <span className={`px-2 py-0.5 rounded text-xs ${d.status === 'active' ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-600'}`}>{d.status}</span>
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-2">
                      <span className="text-gray-600">{ruleSummary(d.rule)}</span>
                      <button onClick={() => handleSetRule(d)} className="text-xs text-blue-600 hover:underline">{d.rule ? '编辑' : '设置'}</button>
                    </div>
                  </td>
                  <td className="px-4 py-3">
                    <div className="text-xs space-y-0.5">
                      {d.conditions.map((c) => (
                        <div key={c.id} className="flex items-center justify-between gap-2 text-gray-500">
                          <span>{c.condition_type}{c.target_id ? `: ${c.target_id.slice(0, 8)}...` : ''}</span>
                          <button onClick={() => handleDeleteCondition(c.id)} className="text-red-600 hover:underline shrink-0">删除</button>
                        </div>
                      ))}
                      <button onClick={() => handleAddCondition(d)} className="text-blue-600 hover:underline">+ 添加条件</button>
                    </div>
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex gap-2">
                      <button onClick={() => handleToggleDiscount(d)} className="text-xs text-blue-600 hover:underline">
                        {d.status === 'active' ? '停用' : '启用'}
                      </button>
                      <button onClick={() => handleDeleteDiscount(d.id)} className="text-xs text-red-600 hover:underline">删除</button>
                    </div>
                  </td>
                </tr>
              ))}
              {discounts.length === 0 && (
                <tr><td colSpan={5} className="px-4 py-8 text-center text-gray-400">暂无折扣</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {/* Gift Cards */}
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-lg font-semibold">礼品卡</h2>
        <button onClick={() => setShowGiftForm(!showGiftForm)} className="px-4 py-2 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 text-sm">
          {showGiftForm ? '取消' : '新建礼品卡'}
        </button>
      </div>

      {showGiftForm && (
        <form onSubmit={handleCreateGiftCard} className="mb-4 p-4 bg-white rounded-xl border border-gray-200 space-y-3">
          <div className="grid grid-cols-4 gap-3">
            <div>
              <label className="text-xs text-gray-500">卡号</label>
              <input placeholder="GIFT100" value={gcCode} onChange={(e) => setGcCode(e.target.value)} required className="w-full px-3 py-2 border rounded-lg text-sm" />
            </div>
            <div>
              <label className="text-xs text-gray-500">初始余额（分）</label>
              <input type="number" value={gcBalance} onChange={(e) => setGcBalance(+e.target.value)} className="w-full px-3 py-2 border rounded-lg text-sm" />
            </div>
            <div>
              <label className="text-xs text-gray-500">币种</label>
              <input value={gcCurrency} onChange={(e) => setGcCurrency(e.target.value)} className="w-full px-3 py-2 border rounded-lg text-sm" />
            </div>
            <div>
              <label className="text-xs text-gray-500">过期时间（可选，ISO）</label>
              <input placeholder="2027-01-01T00:00:00Z" value={gcExpires} onChange={(e) => setGcExpires(e.target.value)} className="w-full px-3 py-2 border rounded-lg text-sm" />
            </div>
          </div>
          <button type="submit" className="px-4 py-2 bg-emerald-600 text-white rounded-lg text-sm hover:bg-emerald-700">创建</button>
        </form>
      )}

      {!loading && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm bg-white rounded-xl overflow-hidden border border-gray-200">
            <thead className="bg-gray-50 text-left">
              <tr>
                <th className="px-4 py-3 font-medium text-gray-600">卡号</th>
                <th className="px-4 py-3 font-medium text-gray-600">余额</th>
                <th className="px-4 py-3 font-medium text-gray-600">币种</th>
                <th className="px-4 py-3 font-medium text-gray-600">状态</th>
                <th className="px-4 py-3 font-medium text-gray-600">过期时间</th>
                <th className="px-4 py-3 font-medium text-gray-600">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {giftCards.map((g) => (
                <tr key={g.id} className="hover:bg-gray-50">
                  <td className="px-4 py-3 font-medium">{g.code}</td>
                  <td className="px-4 py-3">{fmt(g.balance_cents, g.currency)} / {fmt(g.initial_balance_cents, g.currency)}</td>
                  <td className="px-4 py-3">{g.currency}</td>
                  <td className="px-4 py-3">
                    <span className={`px-2 py-0.5 rounded text-xs ${g.status === 'active' ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-600'}`}>{g.status}</span>
                  </td>
                  <td className="px-4 py-3 text-gray-500 text-xs">{g.expires_at ? new Date(g.expires_at).toLocaleDateString('zh-CN') : '—'}</td>
                  <td className="px-4 py-3">
                    <div className="flex gap-2">
                      <button onClick={() => handleToggleGiftCard(g)} className="text-xs text-blue-600 hover:underline">
                        {g.status === 'active' ? '停用' : '启用'}
                      </button>
                      <button onClick={() => handleDeleteGiftCard(g.id)} className="text-xs text-red-600 hover:underline">删除</button>
                    </div>
                  </td>
                </tr>
              ))}
              {giftCards.length === 0 && (
                <tr><td colSpan={6} className="px-4 py-8 text-center text-gray-400">暂无礼品卡</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      <div className="mt-10 mb-3 flex items-center justify-between">
        <h2 className="text-lg font-semibold">hemall 扩展运维 — 播报记录 / 价格基线 / 试探单</h2>
        <button
          onClick={() => setOpsListRefreshKey((k) => k + 1)}
          className="text-xs text-blue-600 hover:underline"
        >
          刷新列表
        </button>
      </div>
      <BroadcastLogsTable refreshKey={opsListRefreshKey} />
      <PriceBenchmarksTable refreshKey={opsListRefreshKey} />
      <ProbeLogsTable refreshKey={opsListRefreshKey} />

      <OpsSection title="清仓喊单 / 社交播报 / 众包核价">
        <ActionCard
          title="竞对小票比价 generate_crushing_offer_workflow"
          badge="public"
          desc="接收小票 OCR 结果 (演示直接填结构化数据)，匹配库存平替商品，生成限时 30 分钟暴击订单。"
          fields={[
            { key: 'customer_ref', label: 'customer_ref' },
            {
              key: 'receipt_items',
              label: 'receipt_items (JSON 数组)',
              type: 'json',
              placeholder: '[{"item":"商品名关键词","qty":1,"price":6000}]',
            },
          ]}
          defaults={opsCtx}
          onSubmit={(v) => api.hemallGenerateCrushingOffer(v as any)}
        />
        <ActionCard
          title="发版 execute_channel_broadcast_workflow"
          badge="admin"
          desc="取批次真实数据 → LLM 生成恐慌感文案 → 落库审计 → 推流微信视频号。同一批次同一播报类型 (fresh_arrival/clearance) 只能成功一次，唯一索引原子拦截重复播报。ManualLLMProvider/ManualWeChatChannelProvider 都是内存态占位实现，没接真实 API key。"
          fields={[
            { key: 'batch_id', label: 'batch_id' },
            { key: 'broadcast_type', label: 'broadcast_type', placeholder: 'fresh_arrival | clearance' },
            { key: 'market_price', label: 'market_price (传统商超参考价，分)', type: 'number' },
            { key: 'access_token', label: 'access_token (微信视频号，演示用任意字符串)' },
          ]}
          defaults={opsCtx}
          onSubmit={(v) => api.hemallExecuteChannelBroadcast(v as any)}
          onResult={() => setOpsListRefreshKey((k) => k + 1)}
        />
        <ActionCard
          title="众包核销 reward_crowdsourced_benchmark_workflow"
          badge="public"
          desc="上传小票 → OCR 解析商品/价格/单位 → 按商品名匹配已有 variant (匹配不上就只存原始品名，不再假装有关联) → 归一化单价写入价格基线 → 无门槛打赏 5 元系统余额 (落到真实 customer.system_balance)。ManualCVProvider 是内存态占位实现，默认识别不出任何商品 (需先用 set_receipt_result 注入测试数据)。"
          fields={[
            { key: 'customer_id', label: 'customer_id' },
            { key: 'receipt_image_text', label: 'receipt_image_text (小票内容，纯文本占位)' },
          ]}
          defaults={opsCtx}
          onSubmit={(v) => api.hemallRewardCrowdsourcedBenchmark(v as any)}
          onResult={(r) => {
            mergeOpsCtx(r as any);
            setOpsListRefreshKey((k) => k + 1);
          }}
        />
      </OpsSection>
    </div>
  );
}
