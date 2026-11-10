'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { api } from '@/lib/api-client';
import { errMessage } from '@/lib/errors';
import { formatMoney } from '@/lib/format';

type TodayBoard = Awaited<ReturnType<typeof api.storeOpsToday>>;

const GROUP_COLORS: Record<string, string> = {
  pick: 'border-blue-200 bg-blue-50 text-blue-800',
  weigh: 'border-amber-200 bg-amber-50 text-amber-800',
  weigh_confirm: 'border-purple-200 bg-purple-50 text-purple-800',
  delivery: 'border-emerald-200 bg-emerald-50 text-emerald-800',
  pickup: 'border-indigo-200 bg-indigo-50 text-indigo-800',
  preorder: 'border-red-200 bg-red-50 text-red-800',
};

function when(iso: string | null): string {
  if (!iso) return '—';
  return new Date(iso).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}

export default function TodayBoardPage() {
  const [data, setData] = useState<TodayBoard | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    let alive = true;
    async function load() {
      try {
        const res = await api.storeOpsToday();
        if (alive) {
          setData(res);
          setError('');
        }
      } catch (e) {
        if (alive) setError(errMessage(e));
      }
    }
    void load();
    const timer = setInterval(() => void load(), 30_000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, []);

  return (
    <div>
      <div className="mb-6 flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold">今日待办</h1>
          <p className="text-sm text-gray-500">
            待拣货、待称重、待补差确认、待配送、待自提、缺货预订，按截止时间排序；每 30 秒自动刷新。
          </p>
        </div>
        <div className="rounded-xl border bg-white px-4 py-2 text-right">
          <div className="text-xs text-gray-500">待办合计</div>
          <div className="text-2xl font-bold text-emerald-700">{data?.total ?? '—'}</div>
        </div>
      </div>

      {error && <div className="mb-4 text-red-600 text-sm">{error}</div>}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2 xl:grid-cols-3">
        {(data?.groups || []).map((group) => (
          <section key={group.key} className={`rounded-xl border p-4 ${GROUP_COLORS[group.key] || 'bg-white'}`}>
            <div className="mb-2 flex items-center justify-between">
              <h2 className="font-semibold">{group.label}</h2>
              <span className="rounded-full bg-white/70 px-2 py-0.5 text-xs font-bold">{group.count}</span>
            </div>
            <div className="max-h-72 space-y-1.5 overflow-auto">
              {group.tasks.map((task) => (
                <Link
                  key={`${group.key}-${task.order_id}`}
                  href={`/admin/orders?order=${task.order_id}`}
                  className="block rounded-lg bg-white px-3 py-2 shadow-sm transition hover:shadow"
                >
                  <div className="flex items-center justify-between gap-2 text-xs">
                    <span className="font-mono text-gray-500">{task.order_id.slice(0, 10)}…</span>
                    <span className="font-semibold text-gray-800">
                      {formatMoney(task.grand_total_cents, task.currency)}
                    </span>
                  </div>
                  <div className="mt-0.5 flex items-center justify-between gap-2 text-[11px] text-gray-500">
                    <span className="truncate">{task.customer || '—'} · {task.line_count} 行</span>
                    <span className="shrink-0">截止 {when(task.deadline)}</span>
                  </div>
                  {task.pickup_arrived_at && (
                    <div className="mt-1 rounded bg-red-600 px-1.5 py-0.5 text-[10px] font-bold text-white">
                      顾客已到店（{when(task.pickup_arrived_at)}）
                    </div>
                  )}
                </Link>
              ))}
              {group.tasks.length === 0 && (
                <div className="py-4 text-center text-xs text-gray-400">无</div>
              )}
            </div>
          </section>
        ))}
      </div>
    </div>
  );
}
