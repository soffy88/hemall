'use client';

/**
 * /shop/merchant — 商家预订（仅商家账号）
 *
 * 常购清单：商家维护一份常购商品清单（名称/图片/价格/默认数量），
 * 以后每次进货打开本页，改一下数量就能整单预订，不用去商城逐个选。
 * 有货部分下单即留货，缺货部分计入预订待配货（到货后按订单顺序配）。
 */

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { api } from '@/lib/api-client';
import { formatMoney } from '@/lib/format';
import { getSelectedRegion } from '@/lib/region-store';
import { getCustomerAuth, initCustomerAuth } from '@/lib/customer-auth-store';
import { productIcon, productGradient } from '@/lib/product-visual';
import { useToast } from '@/components/Toast';
import type { MerchantReserveItem, MerchantReserveOrderResult, StorefrontProduct } from '@/types/api';
import { errMessage } from '@/lib/errors';

interface SearchRow {
  key: string;
  productId: string;
  variantId: string;
  title: string;
  label: string;
  priceCents: number | null;
  totalStock: number;
}

export default function MerchantReservePage() {
  const router = useRouter();
  const showToast = useToast();
  const currency = getSelectedRegion()?.currency;
  const fmt = (cents: number | null | undefined) => formatMoney(cents, currency);

  const [phase, setPhase] = useState<'checking' | 'denied' | 'ready'>('checking');
  const [items, setItems] = useState<MerchantReserveItem[]>([]);
  const [qty, setQty] = useState<Record<string, number>>({});
  const [loadingList, setLoadingList] = useState(false);

  const [search, setSearch] = useState('');
  const [searching, setSearching] = useState(false);
  const [results, setResults] = useState<SearchRow[]>([]);
  const [addingId, setAddingId] = useState<string | null>(null);

  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<MerchantReserveOrderResult | null>(null);

  async function refresh(toDefaults: boolean) {
    setLoadingList(true);
    try {
      const { items: list } = await api.merchantReserveList();
      setItems(list);
      setQty(
        Object.fromEntries(list.map((i) => [i.id, toDefaults ? (i.default_qty || 0) : 0])),
      );
    } catch (e) {
      showToast(errMessage(e), 'error');
    } finally {
      setLoadingList(false);
    }
  }

  useEffect(() => {
    (async () => {
      await initCustomerAuth();
      if (!getCustomerAuth()) {
        router.push('/shop/login');
        return;
      }
      try {
        const me = await api.customerMe();
        if (!me.is_merchant) {
          setPhase('denied');
          return;
        }
        setPhase('ready');
        void refresh(true);
      } catch (e) {
        showToast(errMessage(e), 'error');
        setPhase('denied');
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [router]);

  async function handleSearch(e: React.FormEvent) {
    e.preventDefault();
    const q = search.trim();
    if (!q) return;
    setSearching(true);
    try {
      const products = await api.storeProducts({ search: q, limit: 50 });
      const rows: SearchRow[] = [];
      for (const p of products) {
        for (const v of p.variants || []) {
          const spec = Object.values(v.option_values || {}).join(' / ');
          rows.push({
            key: v.id,
            productId: p.id,
            variantId: v.id,
            title: p.title,
            label: spec || v.sku_code,
            priceCents: v.min_price_cents ?? p.min_price_cents ?? v.reference_price_cents,
            totalStock: v.total_stock ?? 0,
          });
        }
      }
      setResults(rows);
    } catch (e) {
      showToast(errMessage(e), 'error');
    } finally {
      setSearching(false);
    }
  }

  async function handleAddToList(row: SearchRow) {
    setAddingId(row.variantId);
    try {
      await api.merchantReserveUpsert({ variant_id: row.variantId, default_qty: 1 });
      showToast(`已加入清单: ${row.title}`);
      void refresh(true);
    } catch (e) {
      showToast(errMessage(e), 'error');
    } finally {
      setAddingId(null);
    }
  }

  async function handleSaveDefault(item: MerchantReserveItem) {
    try {
      await api.merchantReserveUpsert({
        variant_id: item.variant_id,
        default_qty: Math.max(0, qty[item.id] || 0),
      });
      showToast('默认数量已保存');
      void refresh(true);
    } catch (e) {
      showToast(errMessage(e), 'error');
    }
  }

  async function handleDelete(item: MerchantReserveItem) {
    if (!confirm(`确认从清单移除「${item.title}」？`)) return;
    try {
      await api.merchantReserveDelete(item.id);
      void refresh(true);
    } catch (e) {
      showToast(errMessage(e), 'error');
    }
  }

  async function handleSubmitOrder() {
    const lines = items
      .filter((i) => (qty[i.id] || 0) > 0)
      .map((i) => ({ variant_id: i.variant_id, quantity: qty[i.id] }));
    if (lines.length === 0) {
      showToast('请先填写要预订的数量', 'error');
      return;
    }
    setSubmitting(true);
    setResult(null);
    try {
      const res = await api.merchantReserveOrder(lines);
      setResult(res);
      showToast('预订已提交');
      // 提交后数量归零，并刷新清单上的最新可用库存
      void refresh(false);
    } catch (e) {
      showToast(errMessage(e), 'error');
    } finally {
      setSubmitting(false);
    }
  }

  const estimatedTotal = items.reduce((sum, i) => sum + (qty[i.id] || 0) * (i.price_cents || 0), 0);

  if (phase === 'checking') {
    return <div className="text-center py-12 text-gray-500">加载中...</div>;
  }

  if (phase === 'denied') {
    return (
      <div className="text-center py-16">
        <div className="text-6xl mb-4">🏪</div>
        <h2 className="text-xl font-semibold text-gray-700 mb-2">商家预订仅对商家账号开放</h2>
        <p className="text-sm text-gray-400 mb-4">如需开通商家账号，请联系平台客服。</p>
        <Link href="/shop" className="text-emerald-600 hover:underline">返回商城 →</Link>
      </div>
    );
  }

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      <div>
        <h1 className="text-2xl font-bold">商家预订</h1>
        <p className="text-sm text-gray-400 mt-1">常购清单直接填数量下单；有货先留货，缺货部分自动转预订，到货后配货。</p>
      </div>

      {/* 提交结果 */}
      {result && (
        <div className="bg-emerald-50 border border-emerald-200 rounded-xl p-4 text-sm">
          <div className="font-semibold text-emerald-800 mb-1">
            预订已提交，订单号 <span className="font-mono text-xs">{result.order_id}</span>
          </div>
          <div className="text-emerald-700 mb-2">订单总额 {fmt(result.grand_total_cents)}</div>
          <div className="space-y-1">
            {result.lines.map((l) => (
              <div key={l.variant_id} className="flex items-center justify-between gap-2 text-emerald-900">
                <span className="truncate">{l.title} × {l.quantity}</span>
                <span className="shrink-0 text-xs">
                  {l.reserved_qty > 0 && <span className="mr-2">已留货 ×{l.reserved_qty}</span>}
                  {l.preorder_qty > 0 && (
                    <span className="px-1.5 py-0.5 rounded bg-amber-100 text-amber-700">×{l.preorder_qty} 待配货</span>
                  )}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* 常购清单 */}
      <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
        <div className="px-5 py-4 border-b border-gray-100 flex items-center justify-between">
          <h2 className="font-semibold">我的常购清单</h2>
          {loadingList && <span className="text-xs text-gray-400">刷新中...</span>}
        </div>
        {items.length === 0 ? (
          <div className="px-5 py-8 text-center text-sm text-gray-400">
            清单还是空的，先在下方搜索并添加常购商品。
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-gray-50 text-left">
                <tr>
                  <th className="px-4 py-3 font-medium text-gray-600">商品</th>
                  <th className="px-4 py-3 font-medium text-gray-600">单价</th>
                  <th className="px-4 py-3 font-medium text-gray-600">可用库存</th>
                  <th className="px-4 py-3 font-medium text-gray-600">预订数量</th>
                  <th className="px-4 py-3 font-medium text-gray-600">操作</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {items.map((item) => (
                  <tr key={item.id} className="hover:bg-gray-50">
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-3">
                        {item.image_url ? (
                          // eslint-disable-next-line @next/next/no-img-element
                          <img src={item.image_url} alt={item.title} className="w-11 h-11 rounded-lg object-cover bg-white shrink-0" />
                        ) : (
                          <div className={`w-11 h-11 rounded-lg flex items-center justify-center shrink-0 bg-gradient-to-br ${productGradient(item.product_id)}`}>
                            <span className="text-xl">{productIcon(item.title)}</span>
                          </div>
                        )}
                        <span className="font-medium text-gray-900">{item.title}</span>
                      </div>
                    </td>
                    <td className="px-4 py-3 text-emerald-700 font-medium">{fmt(item.price_cents)}</td>
                    <td className="px-4 py-3">
                      {item.available_qty > 0
                        ? <span className="text-gray-600">{item.available_qty} 件</span>
                        : <span className="text-amber-600">缺货（可预订）</span>}
                    </td>
                    <td className="px-4 py-3">
                      <input
                        type="number"
                        min={0}
                        value={qty[item.id] ?? item.default_qty ?? 0}
                        onChange={(e) =>
                          setQty((prev) => ({ ...prev, [item.id]: Math.max(0, Number(e.target.value) || 0) }))
                        }
                        className="w-24 px-2 py-1.5 border border-gray-300 rounded-lg text-sm text-center"
                      />
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex gap-2 shrink-0">
                        <button onClick={() => handleSaveDefault(item)} className="text-xs text-emerald-600 hover:underline">
                          保存默认
                        </button>
                        <button onClick={() => handleDelete(item)} className="text-xs text-red-500 hover:underline">
                          删除
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {items.length > 0 && (
          <div className="px-5 py-4 border-t border-gray-100 flex items-center justify-between">
            <span className="text-sm text-gray-500">
              预计合计 <span className="font-bold text-emerald-700 text-base">{fmt(estimatedTotal)}</span>
            </span>
            <button
              onClick={handleSubmitOrder}
              disabled={submitting}
              className="px-6 py-2.5 bg-emerald-600 text-white rounded-xl hover:bg-emerald-700 transition font-medium disabled:opacity-50"
            >
              {submitting ? '提交中...' : '提交预订'}
            </button>
          </div>
        )}
      </div>

      {/* 添加商品 */}
      <div className="bg-white rounded-xl border border-gray-200 p-5">
        <h2 className="font-semibold mb-3">添加商品到清单</h2>
        <form onSubmit={handleSearch} className="flex gap-2 mb-3">
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="搜索商品名称"
            className="flex-1 px-3 py-2 border border-gray-300 rounded-lg text-sm"
          />
          <button
            type="submit"
            disabled={searching || !search.trim()}
            className="px-4 py-2 bg-gray-900 text-white rounded-lg text-sm hover:bg-gray-800 disabled:opacity-50"
          >
            {searching ? '搜索中...' : '搜索'}
          </button>
        </form>
        <div className="space-y-2">
          {results.map((row) => (
            <div key={row.key} className="flex items-center justify-between gap-3 bg-gray-50 rounded-lg px-3 py-2 text-sm">
              <span className="min-w-0">
                <span className="text-gray-800">{row.title}</span>
                <span className="text-xs text-gray-400 ml-2">{row.label}</span>
              </span>
              <span className="flex items-center gap-3 shrink-0">
                <span className="text-emerald-700 font-medium">{fmt(row.priceCents)}</span>
                <span className={`text-xs ${row.totalStock > 0 ? 'text-gray-400' : 'text-amber-600'}`}>
                  {row.totalStock > 0 ? `库存 ${row.totalStock}` : '缺货可预订'}
                </span>
                <button
                  onClick={() => handleAddToList(row)}
                  disabled={addingId === row.variantId}
                  className="text-xs px-3 py-1.5 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 disabled:opacity-50"
                >
                  {addingId === row.variantId ? '加入中...' : '加入清单'}
                </button>
              </span>
            </div>
          ))}
          {results.length === 0 && search.trim() && !searching && (
            <div className="text-sm text-gray-400">点「搜索」查看结果。</div>
          )}
        </div>
      </div>
    </div>
  );
}
