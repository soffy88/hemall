'use client';

import { useEffect, useState } from 'react';
import { api } from '@/lib/api-client';
import { ActionCard, OpsSection } from '@/components/ops/OpsActionCard';

/**
 * 供应链运维控制台 —— 批次入库/销毁/结款、供应商入驻/斩仓、C2B 集单、顶棚 CV
 * 自动入库、反向竞标。原独立运维页面 §4.1 批次与供应链 + v2.0 供应商
 * 治理 + 顶棚 CV 入库 + v5.0 反向竞标搬迁至此，统一改造 Phase 4.3。
 *
 * variant_id / location_id 这些底层实体目前没有专门的创建入口——先用已有 ID
 * (或联系工程侧用 SQL 建好参照数据) 粘贴进来。
 */

/** 供应商列表 —— 信誉分 / escrow 质押余额 / 状态，运营巡查用，带手动刷新。 */
function SuppliersTable({ refreshKey }: { refreshKey: number }) {
  const [rows, setRows] = useState<Awaited<ReturnType<typeof api.adminListHemallSuppliers>>>([]);
  const [loading, setLoading] = useState(true);

  async function load() {
    setLoading(true);
    try {
      setRows(await api.adminListHemallSuppliers());
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
            <th className="px-3 py-2 font-medium text-gray-600">supplier_id</th>
            <th className="px-3 py-2 font-medium text-gray-600">wallet_account</th>
            <th className="px-3 py-2 font-medium text-gray-600">trust_score</th>
            <th className="px-3 py-2 font-medium text-gray-600">escrow_balance (分)</th>
            <th className="px-3 py-2 font-medium text-gray-600">status</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100">
          {rows.map((s) => (
            <tr key={s.id} className="hover:bg-gray-50">
              <td className="px-3 py-2 font-mono">{s.id}</td>
              <td className="px-3 py-2">{s.wallet_account}</td>
              <td className="px-3 py-2">
                <span
                  className={`px-1.5 py-0.5 rounded ${
                    s.trust_score < 60 ? 'bg-red-100 text-red-700' : 'bg-emerald-100 text-emerald-700'
                  }`}
                >
                  {s.trust_score}
                </span>
              </td>
              <td className="px-3 py-2">{s.escrow_balance}</td>
              <td className="px-3 py-2">
                <span
                  className={`px-1.5 py-0.5 rounded ${
                    s.status === 'slashed' ? 'bg-red-100 text-red-700' : 'bg-gray-100 text-gray-600'
                  }`}
                >
                  {s.status}
                </span>
              </td>
            </tr>
          ))}
          {!loading && rows.length === 0 && (
            <tr><td colSpan={5} className="px-3 py-6 text-center text-gray-400">暂无供应商</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

export default function SupplyChainPage() {
  const [ctx, setCtx] = useState<Record<string, string>>({});
  const [listRefreshKey, setListRefreshKey] = useState(0);

  function merge(patch: Record<string, unknown>) {
    const next: Record<string, string> = {};
    for (const [k, v] of Object.entries(patch)) {
      if (typeof v === 'string' || typeof v === 'number') next[k] = String(v);
    }
    setCtx((c) => ({ ...c, ...next }));
  }

  return (
    <div>
      <h1 className="text-2xl font-bold mb-2">供应链运维</h1>
      <p className="text-sm text-gray-500 mb-6">
        批次入库/销毁/结款、供应商入驻/斩仓、C2B 集单、顶棚 CV 自动入库、反向竞标。
        每张卡片提交后原样展示后端返回的 JSON（绿色=completed，红色=failed），方便直接核对
        decision_trail / fingerprint 等字段。批次/供应商 ID 会在成功后自动带入下一张卡片。
      </p>

      <OpsSection title="批次与供应链">
        <ActionCard
          title="入库 create_inventory_batch"
          badge="admin"
          desc="接收源头验证过的视频与货物信息，落库开启 active 状态。"
          fields={[
            { key: 'variant_id', label: 'variant_id' },
            { key: 'location_id', label: 'location_id' },
            { key: 'video_url', label: 'video_url', placeholder: 'https://video.example/v.mp4' },
            { key: 'stock_qty', label: 'stock_qty', type: 'number' },
            { key: 'cost_price', label: 'cost_price (分)', type: 'number' },
            { key: 'retail_price', label: 'retail_price (分)', type: 'number' },
            { key: 'expiration_time', label: 'expiration_time (可选, ISO)' },
            { key: 'supplier_id', label: 'supplier_id (可选, 供应商治理)' },
          ]}
          defaults={ctx}
          onSubmit={(v) => api.hemallCreateInventoryBatch(v as any)}
          onResult={(r) => merge({ batch_id: r.batch_id, variant_id: r.variant_id })}
        />
        <ActionCard
          title="C2B 集单 create_crowd_intent"
          badge="public"
          desc="记录预付意向单；同一 variant pending 满 500 单触发规模化采购通知。"
          fields={[
            { key: 'variant_id', label: 'variant_id' },
            { key: 'customer_ref', label: 'customer_ref' },
            { key: 'prepaid_amount', label: 'prepaid_amount (分)', type: 'number' },
          ]}
          defaults={ctx}
          onSubmit={(v) => api.hemallCreateCrowdIntent(v as any)}
        />
        <ActionCard
          title="过期销毁 mark_batch_for_disposal"
          badge="admin"
          desc="死神引擎指令：把过期未售批次标记 disposed，下发丢弃任务。"
          fields={[
            { key: 'batch_id', label: 'batch_id' },
            { key: 'reason', label: 'reason (默认 expired)' },
          ]}
          defaults={ctx}
          onSubmit={(v) => api.hemallMarkBatchForDisposal(v as any)}
        />
        <ActionCard
          title="供应商结款 batch_settlement"
          badge="admin"
          desc="批次售罄后秒结供应商货款。"
          fields={[
            { key: 'batch_id', label: 'batch_id' },
            { key: 'supplier_account', label: 'supplier_account' },
          ]}
          defaults={ctx}
          onSubmit={(v) => api.hemallBatchSettlement(v as any)}
        />
        <ActionCard
          title="顶棚 CV 自动入库 execute_ambient_intake_workflow"
          badge="admin"
          desc="顶棚摄像头信号自动点亮已预登记的批次为 active。演示用文本代替真实视频流。"
          fields={[
            {
              key: 'video_stream_text',
              label: 'video_stream_text (演示用文本，代替真实视频字节流)',
            },
          ]}
          defaults={ctx}
          onSubmit={(v) => api.hemallExecuteAmbientIntake(v as any)}
        />
      </OpsSection>

      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-gray-500">供应商治理 — 供应商列表</h2>
        <button
          onClick={() => setListRefreshKey((k) => k + 1)}
          className="text-xs text-blue-600 hover:underline"
        >
          刷新列表
        </button>
      </div>
      <SuppliersTable refreshKey={listRefreshKey} />

      <OpsSection title="供应商治理与定价">
        <ActionCard
          title="供应商入驻 claim_origin_workflow"
          badge="public"
          desc="果农 PWA 注册，录入原产地围栏 (GeoJSON Polygon)，trust_score 初始 100。"
          fields={[
            { key: 'wallet_account', label: 'wallet_account' },
            {
              key: 'spatial_polygon',
              label: 'spatial_polygon (GeoJSON Polygon)',
              type: 'json',
              placeholder: '{"type":"Polygon","coordinates":[[[121.0,31.0],[121.1,31.0],[121.1,31.1],[121.0,31.0]]]}',
            },
          ]}
          defaults={ctx}
          onSubmit={(v) => api.hemallClaimOrigin(v as any)}
          onResult={(r) => {
            merge({ supplier_id: (r as any).supplier_id });
            setListRefreshKey((k) => k + 1);
          }}
        />
        <ActionCard
          title="斩仓处罚 execute_slashing_workflow"
          badge="admin"
          desc="从供应商 escrow_balance 扣 3 倍罚金，原路退给消费者；信誉分跌破 60 直接冻结账号。"
          fields={[
            { key: 'supplier_id', label: 'supplier_id' },
            { key: 'order_id', label: 'order_id (原路退款用)' },
            { key: 'penalty_base_amount', label: 'penalty_base_amount (分)', type: 'number' },
            { key: 'new_trust_score', label: 'new_trust_score (仲裁后信誉分)', type: 'number' },
            { key: 'reason', label: 'reason' },
          ]}
          defaults={ctx}
          onSubmit={(v) => api.hemallExecuteSlashing(v as any)}
          onResult={() => setListRefreshKey((k) => k + 1)}
        />
        <ActionCard
          title="反向竞标 submit_supplier_reverse_auction_workflow"
          badge="public"
          desc="供应商 (果农) 申报报价 → 对比该批次 SKU 的历史价格基线均价 → 未达 50% 毛利红线直接拒绝，达标则批准入库发车 (cost_price 写回，状态转 in_transit)。SKU 完全没有历史基线数据时也会直接拒绝——没有参照就没法核验红线。"
          fields={[
            { key: 'batch_id', label: 'batch_id' },
            { key: 'supplier_bid_price_per_gram', label: 'supplier_bid_price_per_gram (跟基准线同口径: 分/克)', type: 'number' },
            { key: 'batch_cost_price', label: 'batch_cost_price (实际写回的整批成本，分)', type: 'number' },
          ]}
          defaults={ctx}
          onSubmit={(v) => api.hemallSubmitSupplierReverseAuction(v as any)}
        />
      </OpsSection>
    </div>
  );
}
