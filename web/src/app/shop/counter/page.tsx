'use client';

/**
 * /shop/counter — 柜台称重试点（仅商家账号）
 *
 * 拍照点选 + 称重联动：点选照片格商品 → 显示秤读数（或手工输重量）→
 * 确认 → 重量 × 单价记行进单据。真实秤尚未接入，秤读数来自模拟器注入
 * （明确标记 simulator）；识别模型另行立项，本页不做视觉识别。
 * 结算只到“待结算”，不创建支付单据、不碰券与折扣。
 */

import { useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { api } from '@/lib/api-client';
import { formatMoney } from '@/lib/format';
import { getSelectedRegion } from '@/lib/region-store';
import { getCustomerAuth, initCustomerAuth } from '@/lib/customer-auth-store';
import { productIcon, productGradient } from '@/lib/product-visual';
import { useToast } from '@/components/Toast';
import type { CounterProduct, CounterTicket, CounterWeighLine } from '@/types/api';
import { errMessage } from '@/lib/errors';

function basisLabel(grams: number): string {
  return grams >= 1000 ? `${grams / 1000}kg` : `${grams}g`;
}

function sourceBadge(source: string | null): string {
  if (source === 'simulator') return '模拟';
  if (source === 'manual') return '手工';
  return '秤';
}

export default function CounterPage() {
  const router = useRouter();
  const showToast = useToast();
  const currency = getSelectedRegion()?.currency;
  const fmt = (cents: number | null | undefined) => formatMoney(cents, currency);

  const [phase, setPhase] = useState<'checking' | 'denied' | 'ready'>('checking');
  const [products, setProducts] = useState<CounterProduct[]>([]);
  const [filter, setFilter] = useState('');
  const [selected, setSelected] = useState<CounterProduct | null>(null);
  const [ticket, setTicket] = useState<CounterTicket | null>(null);
  const [lines, setLines] = useState<CounterWeighLine[]>([]);
  const [tickets, setTickets] = useState<CounterTicket[]>([]);
  const [latest, setLatest] = useState<CounterWeighLine | null>(null);
  const [weightInput, setWeightInput] = useState('');
  const [useScaleReading, setUseScaleReading] = useState(true);
  const [simWeight, setSimWeight] = useState('');
  const [brokenImgs, setBrokenImgs] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);

  const loadTicket = useCallback(async (ticketId: string) => {
    const { ticket: t, lines: ls } = await api.counterGetTicket(ticketId);
    setTicket(t);
    setLines(ls);
  }, []);

  const loadTickets = useCallback(async () => {
    const { tickets: list } = await api.counterListTickets();
    setTickets(list);
    return list;
  }, []);

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
        const { products: list } = await api.counterProducts();
        setProducts(list);
        const open = (await loadTickets()).find((t) => t.status === 'open');
        if (open) {
          await loadTicket(open.ticket_id);
        } else {
          const created = await api.counterCreateTicket({});
          setTicket(created);
          setLines([]);
          void loadTickets();
        }
      } catch (e) {
        showToast(errMessage(e), 'error');
        setPhase('denied');
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [router]);

  // 轮询最新秤读数（仅当单据 open 时）
  useEffect(() => {
    if (!ticket || ticket.status !== 'open') return;
    let stopped = false;
    const poll = async () => {
      try {
        const reading = await api.counterLatestWeight({ ticket_id: ticket.ticket_id });
        if (stopped) return;
        setLatest('weight_grams' in reading && reading.weight_grams != null
          ? (reading as CounterWeighLine)
          : null);
      } catch {
        // 轮询失败不打断柜台操作，下一次继续
      }
    };
    void poll();
    const timer = setInterval(poll, 1500);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [ticket?.ticket_id, ticket?.status]);

  const filtered = products.filter((p) => !filter.trim() || p.title.includes(filter.trim()));

  async function handleNewTicket() {
    setBusy(true);
    try {
      const created = await api.counterCreateTicket({});
      setTicket(created);
      setLines([]);
      setSelected(null);
      setLatest(null);
      void loadTickets();
    } catch (e) {
      showToast(errMessage(e, '开单失败'), 'error');
    } finally {
      setBusy(false);
    }
  }

  async function handleConfirm() {
    if (!ticket || !selected) return;
    const readingUsable =
      useScaleReading && latest && latest.status === 'unpriced' && latest.weight_grams != null;
    let weightGrams: number | null = null;
    if (readingUsable) {
      weightGrams = latest.weight_grams;
    } else {
      const parsed = Number(weightInput);
      if (!Number.isInteger(parsed) || parsed < 1 || parsed > 15000) {
        showToast('请输入 1~15000 克的手工重量', 'error');
        return;
      }
      weightGrams = parsed;
    }
    setBusy(true);
    try {
      const { total_cents } = await api.counterAddLine(ticket.ticket_id, {
        batch_id: selected.batch_id,
        variant_id: selected.variant_id,
        weight_grams: weightGrams ?? undefined,
        source: readingUsable ? 'scale' : 'manual',
        weigh_message_id: readingUsable ? latest?.message_id : undefined,
      });
      setTicket((t) => (t ? { ...t, total_cents } : t));
      await loadTicket(ticket.ticket_id);
      setWeightInput('');
      showToast('已记行');
    } catch (e) {
      showToast(errMessage(e, '记行失败'), 'error');
    } finally {
      setBusy(false);
    }
  }

  async function handleSimulate() {
    if (!ticket) return;
    const parsed = Number(simWeight);
    if (!Number.isInteger(parsed) || parsed < 1 || parsed > 15000) {
      showToast('模拟重量请输入 1~15000 克', 'error');
      return;
    }
    setBusy(true);
    try {
      await api.counterSimulateWeigh({ ticket_id: ticket.ticket_id, weight_grams: parsed });
      setSimWeight('');
      showToast('模拟重量已注入，等待秤读数刷新');
    } catch (e) {
      showToast(errMessage(e, '模拟注入失败'), 'error');
    } finally {
      setBusy(false);
    }
  }

  async function handleSettle() {
    if (!ticket) return;
    setBusy(true);
    try {
      const updated = await api.counterMarkPendingSettlement(ticket.ticket_id);
      setTicket(updated);
      showToast('已挂单待结算（未创建支付单据）');
      void loadTickets();
    } catch (e) {
      showToast(errMessage(e, '挂单失败'), 'error');
    } finally {
      setBusy(false);
    }
  }

  async function handleVoid() {
    if (!ticket) return;
    setBusy(true);
    try {
      const updated = await api.counterVoidTicket(ticket.ticket_id);
      setTicket(updated);
      showToast('单据已作废');
      void loadTickets();
    } catch (e) {
      showToast(errMessage(e, '作废失败'), 'error');
    } finally {
      setBusy(false);
    }
  }

  if (phase === 'checking') {
    return <div className="text-center py-12 text-gray-500">加载中...</div>;
  }

  if (phase === 'denied') {
    return (
      <div className="text-center py-16">
        <div className="text-6xl mb-4">⚖️</div>
        <h2 className="text-xl font-semibold text-gray-700 mb-2">柜台称重仅对商家账号开放</h2>
        <p className="text-sm text-gray-400 mb-4">如需开通商家账号，请联系平台客服。</p>
        <Link href="/shop" className="text-emerald-600 hover:underline">返回商城 →</Link>
      </div>
    );
  }

  const ticketOpen = ticket?.status === 'open';

  return (
    <div className="max-w-6xl mx-auto space-y-6">
      <div>
        <h1 className="text-2xl font-bold">柜台称重试点</h1>
        <p className="text-sm text-gray-500">
          点选商品 → 秤读数/手工重量 → 确认记行。真实秤未接入，读数来自模拟器（标记“模拟”）。
        </p>
      </div>

      {/* 单据栏 */}
      <div className="rounded-2xl border bg-white p-4 shadow-sm">
        <div className="flex flex-wrap items-center gap-3">
          <span className="font-semibold">
            单据 {ticket ? `#${ticket.ticket_id.slice(0, 8)}` : '—'}
          </span>
          <span className="text-sm text-gray-500">
            状态：{ticket?.status === 'open' ? '进行中' : ticket?.status === 'pending_settlement' ? '待结算' : ticket?.status ?? '—'}
          </span>
          <span className="ml-auto text-lg font-bold">合计 {fmt(ticket?.total_cents)}</span>
          <button
            type="button"
            onClick={handleNewTicket}
            disabled={busy}
            className="rounded-full bg-emerald-600 px-4 py-1.5 text-sm text-white disabled:opacity-50"
          >
            新开单据
          </button>
          <button
            type="button"
            onClick={handleSettle}
            disabled={busy || !ticketOpen}
            className="rounded-full bg-amber-500 px-4 py-1.5 text-sm text-white disabled:opacity-50"
          >
            挂单待结算
          </button>
          <button
            type="button"
            onClick={handleVoid}
            disabled={busy || !ticketOpen}
            className="rounded-full bg-gray-200 px-4 py-1.5 text-sm text-gray-700 disabled:opacity-50"
          >
            作废
          </button>
        </div>
        {tickets.length > 1 && (
          <div className="mt-3 flex flex-wrap gap-2">
            {tickets.slice(0, 8).map((t) => (
              <button
                key={t.ticket_id}
                type="button"
                onClick={() => void loadTicket(t.ticket_id)}
                className={`rounded-full border px-3 py-1 text-xs ${t.ticket_id === ticket?.ticket_id ? 'border-emerald-600 text-emerald-700' : 'text-gray-500'}`}
              >
                #{t.ticket_id.slice(0, 8)} · {t.status} · {fmt(t.total_cents)}
              </button>
            ))}
          </div>
        )}
        {/* 行项 */}
        <div className="mt-4 divide-y">
          {lines.length === 0 && <p className="py-2 text-sm text-gray-400">暂无行项，先点选商品称重。</p>}
          {lines.map((line) => (
            <div key={line.message_id} className="flex items-center gap-3 py-2 text-sm">
              <span className="font-medium">{line.title ?? line.batch_id?.slice(0, 8) ?? '—'}</span>
              <span className="text-gray-500">
                {line.weight_grams}g × {fmt(line.unit_price_cents)}/{line.price_basis_grams ? basisLabel(line.price_basis_grams) : '—'}
              </span>
              <span className="ml-auto font-semibold">{fmt(line.amount_cents)}</span>
              <span className="rounded-full bg-gray-100 px-2 py-0.5 text-xs text-gray-500">
                {sourceBadge(line.source)}
              </span>
              <span className="text-xs text-gray-400">{line.status}</span>
            </div>
          ))}
        </div>
      </div>

      {/* 选中商品 + 重量确认 */}
      {selected && (
        <div className="rounded-2xl border border-emerald-200 bg-emerald-50 p-4">
          <div className="flex flex-wrap items-center gap-3">
            <span className="font-semibold">{selected.title}</span>
            <span className="text-sm text-gray-600">
              {fmt(selected.unit_price_cents)} / {basisLabel(selected.price_basis_grams)}
            </span>
            <span className="text-sm">
              秤读数：
              {latest && latest.weight_grams != null
                ? `${latest.weight_grams}g（${sourceBadge(latest.source)}${latest.status === 'unpriced' ? '，未确认' : ''}）`
                : '暂无'}
            </span>
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-3">
            <label className="flex items-center gap-1 text-sm">
              <input
                type="checkbox"
                checked={useScaleReading}
                onChange={(e) => setUseScaleReading(e.target.checked)}
              />
              用秤读数
            </label>
            <input
              value={weightInput}
              onChange={(e) => setWeightInput(e.target.value)}
              inputMode="numeric"
              placeholder="手工重量（克）"
              className="w-36 rounded-lg border px-3 py-1.5 text-sm"
            />
            <span className="text-xs text-gray-500">手工输入会明确标记“手工”</span>
            <button
              type="button"
              onClick={handleConfirm}
              disabled={busy || !ticketOpen}
              className="rounded-full bg-emerald-600 px-5 py-1.5 text-sm text-white disabled:opacity-50"
            >
              确认记行
            </button>
          </div>
        </div>
      )}

      {/* 模拟器 */}
      <div className="rounded-2xl border border-dashed bg-white p-4">
        <p className="text-sm font-semibold">模拟器（无真秤时演示用，注入的读数标记“模拟”）</p>
        <div className="mt-2 flex flex-wrap items-center gap-3">
          <input
            value={simWeight}
            onChange={(e) => setSimWeight(e.target.value)}
            inputMode="numeric"
            placeholder="模拟重量（克）"
            className="w-40 rounded-lg border px-3 py-1.5 text-sm"
          />
          <button
            type="button"
            onClick={handleSimulate}
            disabled={busy || !ticketOpen}
            className="rounded-full bg-gray-800 px-4 py-1.5 text-sm text-white disabled:opacity-50"
          >
            注入重量
          </button>
          <span className="text-xs text-gray-400">真实秤接入后停用本区；接入契约见 docs/weigh-scale-contract.md</span>
        </div>
      </div>

      {/* 照片格 */}
      <div>
        <input
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="筛选商品（名称）"
          className="mb-3 w-full max-w-sm rounded-lg border px-3 py-2 text-sm"
        />
        {filtered.length === 0 ? (
          <p className="py-8 text-center text-sm text-gray-400">暂无可称重商品（需有可售批次与计价基准）。</p>
        ) : (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5">
            {filtered.map((p) => {
              const img = brokenImgs.has(p.batch_id) ? null : p.image_url;
              const active = selected?.batch_id === p.batch_id;
              return (
                <button
                  key={p.batch_id}
                  type="button"
                  onClick={() => setSelected(p)}
                  className={`overflow-hidden rounded-2xl border bg-white text-left shadow-sm transition hover:shadow-md ${active ? 'border-emerald-600 ring-2 ring-emerald-200' : 'border-gray-100'}`}
                >
                  <div className="relative aspect-square w-full overflow-hidden bg-white">
                    {img ? (
                      // eslint-disable-next-line @next/next/no-img-element
                      <img
                        src={img}
                        alt={p.title}
                        loading="lazy"
                        className="h-full w-full object-contain p-2"
                        onError={() => {
                          setBrokenImgs((prev) => new Set(prev).add(p.batch_id));
                        }}
                      />
                    ) : (
                      <div className={`flex h-full w-full items-center justify-center bg-gradient-to-br ${productGradient(p.product_id)}`}>
                        <span className="text-5xl">{productIcon(p.title)}</span>
                      </div>
                    )}
                  </div>
                  <div className="p-2">
                    <p className="truncate text-sm font-medium">{p.title}</p>
                    <p className="text-sm text-emerald-700">
                      {fmt(p.unit_price_cents)} / {basisLabel(p.price_basis_grams)}
                    </p>
                  </div>
                </button>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
