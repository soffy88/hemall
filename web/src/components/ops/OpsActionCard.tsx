'use client';

import { useEffect, useState } from 'react';
import type { OmodulResult } from '@/types/api';

/**
 * 裸操作卡片 —— 表单填参数 → 拍执行 → 摊 JSON 结果，配 decision_trail/
 * fingerprint 审计。原扩展域运营控制台统一改造
 * Phase 4.3 拆分后，各分域页面 (aftersales/inventory/marketing/customers/
 * supply-chain/fulfillment/settlement/growth) 共用这一套卡片组件，不重复
 *造轮子。跟"代客下单"这类专用业务对象 UI 不同：这里没有强制的线性步骤，
 * 是独立的运维动作，直接对着 omodul 参数走。
 */

export type FieldType = 'text' | 'number' | 'json';

export interface FieldDef {
  key: string;
  label: string;
  type?: FieldType;
  placeholder?: string;
}

export interface ActionCardProps {
  title: string;
  badge: 'admin' | 'public';
  desc: string;
  fields: FieldDef[];
  defaults?: Record<string, string>;
  onSubmit: (values: Record<string, unknown>) => Promise<OmodulResult>;
  onResult?: (result: OmodulResult) => void;
}

export function ActionCard({ title, badge, desc, fields, defaults, onSubmit, onResult }: ActionCardProps) {
  const [values, setValues] = useState<Record<string, string>>(defaults || {});
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; body: unknown } | null>(null);

  // 上游 ctx（上一次成功操作带出的 batch_id / cart_id 等）变化时，只补填当前
  // 还是空的字段——不覆盖用户已经手改过的值，也不清空 busy/result，这样
  // "刚提交成功、结果面板还显示着"的卡片不会因为别的卡片改了 ctx 就被重置。
  useEffect(() => {
    if (!defaults) return;
    setValues((prev) => {
      const next = { ...prev };
      let changed = false;
      for (const [k, v] of Object.entries(defaults)) {
        if (!next[k]) {
          next[k] = v;
          changed = true;
        }
      }
      return changed ? next : prev;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(defaults)]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setResult(null);
    try {
      const payload: Record<string, unknown> = {};
      for (const f of fields) {
        const raw = values[f.key];
        if (raw === undefined || raw === '') continue;
        if (f.type === 'number') payload[f.key] = Number(raw);
        else if (f.type === 'json') payload[f.key] = JSON.parse(raw);
        else payload[f.key] = raw;
      }
      const res = await onSubmit(payload);
      setResult({ ok: res.status !== 'failed', body: res });
      if (res.status !== 'failed') onResult?.(res);
    } catch (e: any) {
      setResult({ ok: false, body: { error: { message: e.message } } });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="bg-white rounded-xl border border-gray-200 p-4 flex flex-col">
      <div className="flex items-center gap-2 mb-1">
        <h3 className="font-semibold text-sm">{title}</h3>
        <span
          className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${
            badge === 'admin' ? 'bg-amber-100 text-amber-700' : 'bg-emerald-100 text-emerald-700'
          }`}
        >
          {badge === 'admin' ? '需登录' : '公开'}
        </span>
      </div>
      <p className="text-xs text-gray-500 mb-3">{desc}</p>
      <form onSubmit={handleSubmit} className="space-y-2 flex-1">
        {fields.map((f) =>
          f.type === 'json' ? (
            <textarea
              key={f.key}
              placeholder={f.placeholder || f.label}
              value={values[f.key] || ''}
              onChange={(e) => setValues({ ...values, [f.key]: e.target.value })}
              rows={2}
              className="w-full px-3 py-1.5 border rounded-lg text-xs font-mono"
            />
          ) : (
            <input
              key={f.key}
              type={f.type === 'number' ? 'number' : 'text'}
              placeholder={f.placeholder || f.label}
              value={values[f.key] || ''}
              onChange={(e) => setValues({ ...values, [f.key]: e.target.value })}
              className="w-full px-3 py-1.5 border rounded-lg text-xs"
            />
          )
        )}
        <button
          disabled={busy}
          type="submit"
          className="w-full py-1.5 bg-blue-600 text-white rounded-lg text-xs hover:bg-blue-700 disabled:opacity-50"
        >
          {busy ? '提交中...' : '执行'}
        </button>
      </form>
      {result && (
        <div
          className={`mt-2 p-2 rounded text-[11px] font-mono whitespace-pre-wrap break-all max-h-32 overflow-auto ${
            result.ok ? 'bg-emerald-50 text-emerald-800' : 'bg-red-50 text-red-700'
          }`}
        >
          {JSON.stringify(result.body, null, 1)}
        </div>
      )}
    </div>
  );
}

export function OpsSection({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="mb-8">
      <h2 className="text-sm font-semibold text-gray-500 mb-3">{title}</h2>
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">{children}</div>
    </div>
  );
}
