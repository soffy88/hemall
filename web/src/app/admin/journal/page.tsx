'use client';

import { useEffect, useState } from 'react';
import { api } from '@/lib/api-client';
import { errMessage } from '@/lib/errors';
import type { Product } from '@/types/api';
import { displayOptionLabel } from '@/lib/option-label';

type JournalRow = {
  sku: string; product: string; unit: string;
  opening_qty: number; in_qty: number; sales_qty: number; loss_qty: number;
  stock_qty: number; unit_cost_cents: number | null; amount_cents: number;
};
type Jam = { date: string; rows: JournalRow[]; entries: Array<Record<string, string | number | null>> };

function today(): string {
  const d = new Date();
  const local = new Date(d.getTime() - d.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 10);
}

export default function JournalPage() {
  const [day, setDay] = useState(today());
  const [data, setData] = useState<Jam | null>(null);
  const [error, setError] = useState('');
  const [search, setSearch] = useState('');
  const [products, setProducts] = useState<Product[]>([]);
  const [variantId, setVariantId] = useState('');
  const [variantLabel, setVariantLabel] = useState('');
  const [qty, setQty] = useState('1');
  const [costYuan, setCostYuan] = useState('');
  const [supplier, setSupplier] = useState('');
  const [reason, setReason] = useState('损耗');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);

  async function load(date: string) {
    setError('');
    try {
      setData((await api.storeOpsJournal(date)) as unknown as Jam);
    } catch (e) {
      setError(errMessage(e));
    }
  }

  useEffect(() => { void load(day); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  async function doSearch() {
    if (!search.trim()) return;
    try {
      setProducts(await api.adminListProducts({ search: search.trim(), limit: 20 }));
    } catch (e) {
      setError(errMessage(e));
    }
  }

  function pick(variant: { id: string }, label: string) {
    setVariantId(variant.id);
    setVariantLabel(label);
  }

  async function submit(kind: 'in' | 'loss') {
    if (!variantId) { setError('请先搜索并选择商品规格'); return; }
    const n = Number(qty);
    if (!Number.isInteger(n) || n <= 0) { setError('数量必须是正整数（称重商品按克）'); return; }
    setBusy(true);
    setError('');
    try {
      if (kind === 'in') {
        const cents = costYuan.trim() ? Math.round(Number(costYuan) * 100) : undefined;
        if (cents !== undefined && (!Number.isFinite(cents) || cents < 0)) throw new Error('进价格式不正确');
        await api.storeOpsJournalIn({ variant_id: variantId, qty: n, unit_cost_cents: cents, supplier, note });
      } else {
        await api.storeOpsJournalLoss({ variant_id: variantId, qty: n, reason, note });
      }
      setQty('1'); setNote('');
      await load(day);
    } catch (e) {
      setError(errMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <div>
          <h1 className="text-2xl font-bold">进销存日记</h1>
          <p className="text-sm text-gray-500">按 SKU 记入库（数量/重量、进价、供应商）、销售自动出库、损耗（原因），日末结存可导出 CSV。</p>
        </div>
        <div className="flex gap-2">
          <input type="date" value={day} onChange={(e) => setDay(e.target.value)} className="px-3 py-2 border rounded-lg text-sm" />
          <button onClick={() => load(day)} className="px-4 py-2 bg-emerald-600 text-white rounded-lg text-sm">查询</button>
          <a href={`/api/admin/store-ops/journal.csv?date=${day}`} className="px-4 py-2 border rounded-lg text-sm hover:bg-gray-50">导出 CSV</a>
        </div>
      </div>

      {error && <div className="mb-3 text-red-600 text-sm">{error}</div>}

      <div className="grid lg:grid-cols-2 gap-4 mb-6">
        <div className="bg-white border rounded-xl p-4 space-y-2">
          <h2 className="font-semibold">选择商品</h2>
          <div className="flex gap-2">
            <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="搜索商品名称"
              onKeyDown={(e) => { if (e.key === 'Enter') void doSearch(); }}
              className="flex-1 px-3 py-2 border rounded-lg text-sm" />
            <button onClick={doSearch} className="px-3 py-2 bg-gray-900 text-white rounded-lg text-sm">搜索</button>
          </div>
          <div className="max-h-40 overflow-auto divide-y text-sm">
            {products.map((p) => (p.variants || []).map((v) => (
              <button key={v.id} onClick={() => pick(v, `${p.title} ${v.sku_code} ${displayOptionLabel(v.option_values)}`)}
                className={`w-full text-left px-2 py-1.5 hover:bg-emerald-50 ${variantId === v.id ? 'bg-emerald-50 font-medium' : ''}`}>
                {p.title} · {v.sku_code} · {displayOptionLabel(v.option_values)} · {v.total_stock ?? 0} 库存
              </button>
            )))}
            {products.length === 0 && <div className="text-gray-400 py-2">输入关键词搜索 SKU</div>}
          </div>
          {variantId && <div className="text-xs text-emerald-700">已选：{variantLabel}</div>}
        </div>

        <div className="bg-white border rounded-xl p-4 space-y-2">
          <h2 className="font-semibold">入库 / 损耗</h2>
          <div className="grid grid-cols-2 gap-2">
            <label className="text-xs text-gray-500">数量（称重按克，按件按件）
              <input value={qty} onChange={(e) => setQty(e.target.value)} type="number" min={1}
                className="mt-1 w-full px-3 py-2 border rounded-lg text-sm" />
            </label>
            <label className="text-xs text-gray-500">进价（元，可空）
              <input value={costYuan} onChange={(e) => setCostYuan(e.target.value)} type="number" step="0.01" min={0}
                className="mt-1 w-full px-3 py-2 border rounded-lg text-sm" />
            </label>
            <label className="text-xs text-gray-500 col-span-2">供应商（入库用）
              <input value={supplier} onChange={(e) => setSupplier(e.target.value)}
                className="mt-1 w-full px-3 py-2 border rounded-lg text-sm" />
            </label>
            <label className="text-xs text-gray-500 col-span-2">损耗原因（损耗用）
              <input value={reason} onChange={(e) => setReason(e.target.value)}
                className="mt-1 w-full px-3 py-2 border rounded-lg text-sm" />
            </label>
            <label className="text-xs text-gray-500 col-span-2">备注
              <input value={note} onChange={(e) => setNote(e.target.value)}
                className="mt-1 w-full px-3 py-2 border rounded-lg text-sm" />
            </label>
          </div>
          <div className="flex gap-2 pt-1">
            <button onClick={() => submit('in')} disabled={busy}
              className="flex-1 px-4 py-2 bg-emerald-600 text-white rounded-lg text-sm disabled:opacity-50">入库</button>
            <button onClick={() => submit('loss')} disabled={busy}
              className="flex-1 px-4 py-2 bg-amber-600 text-white rounded-lg text-sm disabled:opacity-50">记损耗</button>
          </div>
        </div>
      </div>

      <div className="bg-white border rounded-xl overflow-hidden mb-6">
        <div className="px-4 py-3 border-b font-semibold">日末结存（{data?.date || day}）</div>
        <table className="w-full text-sm">
          <thead className="bg-gray-50 text-left text-gray-600">
            <tr>
              <th className="px-3 py-2">SKU</th><th className="px-3 py-2">商品</th><th className="px-3 py-2">单位</th>
              <th className="px-3 py-2">期初</th><th className="px-3 py-2">入库</th><th className="px-3 py-2">销售出库</th>
              <th className="px-3 py-2">损耗</th><th className="px-3 py-2">日末结存</th><th className="px-3 py-2">销售额</th>
            </tr>
          </thead>
          <tbody className="divide-y">
            {(data?.rows || []).map((r, i) => (
              <tr key={`${r.sku}-${i}`}>
                <td className="px-3 py-2 font-mono text-xs">{r.sku || '—'}</td>
                <td className="px-3 py-2">{r.product}</td>
                <td className="px-3 py-2">{r.unit}</td>
                <td className="px-3 py-2">{r.opening_qty}</td>
                <td className="px-3 py-2">{r.in_qty}</td>
                <td className="px-3 py-2">{r.sales_qty}</td>
                <td className="px-3 py-2">{r.loss_qty}</td>
                <td className="px-3 py-2 font-medium">{r.stock_qty}</td>
                <td className="px-3 py-2">¥{(r.amount_cents / 100).toFixed(2)}</td>
              </tr>
            ))}
            {(data?.rows || []).length === 0 && (
              <tr><td colSpan={9} className="px-4 py-8 text-center text-gray-400">当日无进销存记录</td></tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="bg-white border rounded-xl overflow-hidden">
        <div className="px-4 py-3 border-b font-semibold">当日手工流水</div>
        <table className="w-full text-sm">
          <thead className="bg-gray-50 text-left text-gray-600">
            <tr><th className="px-3 py-2">时间</th><th className="px-3 py-2">类型</th><th className="px-3 py-2">商品</th>
              <th className="px-3 py-2">数量</th><th className="px-3 py-2">进价</th><th className="px-3 py-2">供应商/原因</th><th className="px-3 py-2">备注</th></tr>
          </thead>
          <tbody className="divide-y">
            {(data?.entries || []).map((e, i) => (
              <tr key={String(e.id || i)}>
                <td className="px-3 py-2 text-xs text-gray-500">{String(e.created_at || '').replace('T', ' ').slice(0, 16)}</td>
                <td className="px-3 py-2">{e.entry_type === 'in' ? '入库' : '损耗'}</td>
                <td className="px-3 py-2">{e.product}</td>
                <td className="px-3 py-2">{e.qty} {e.unit}</td>
                <td className="px-3 py-2">{e.unit_cost_cents != null ? `¥${(Number(e.unit_cost_cents) / 100).toFixed(2)}` : '—'}</td>
                <td className="px-3 py-2">{e.supplier || e.reason}</td>
                <td className="px-3 py-2 text-gray-500">{e.note}</td>
              </tr>
            ))}
            {(data?.entries || []).length === 0 && (
              <tr><td colSpan={7} className="px-4 py-8 text-center text-gray-400">当日无手工流水</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
