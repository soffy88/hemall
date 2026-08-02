'use client';

import { useEffect, useState } from 'react';
import { api } from '@/lib/api-client';
import { ActionCard, OpsSection } from '@/components/ops/OpsActionCard';

/**
 * 增长运维控制台 —— 抖音拓客与数字领主：云加盟认领、领主契约册封、转化落账。
 * 原 /admin/clearnode 页面 v6.0 §4 全部内容搬迁至此，统一改造 Phase 4.3。
 */

/** 抖音达人智能分润契约 —— 领主税/雇佣兵悬赏绑定关系，运营核查用，带手动刷新。 */
function AffiliateContractsTable({ refreshKey }: { refreshKey: number }) {
  const [rows, setRows] = useState<Awaited<ReturnType<typeof api.adminListClearnodeAffiliateContracts>>>([]);
  const [loading, setLoading] = useState(true);

  async function load() {
    setLoading(true);
    try {
      setRows(await api.adminListClearnodeAffiliateContracts());
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
            <th className="px-3 py-2 font-medium text-gray-600">contract_id</th>
            <th className="px-3 py-2 font-medium text-gray-600">douyin_uid</th>
            <th className="px-3 py-2 font-medium text-gray-600">contract_type</th>
            <th className="px-3 py-2 font-medium text-gray-600">bound_entity_id</th>
            <th className="px-3 py-2 font-medium text-gray-600">status</th>
            <th className="px-3 py-2 font-medium text-gray-600">created_at</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100">
          {rows.map((c) => (
            <tr key={c.id} className="hover:bg-gray-50">
              <td className="px-3 py-2 font-mono">{c.id}</td>
              <td className="px-3 py-2 font-mono truncate max-w-[10rem]" title={c.douyin_uid}>{c.douyin_uid}</td>
              <td className="px-3 py-2">
                <span
                  className={`px-1.5 py-0.5 rounded ${
                    c.contract_type === 'digital_lord'
                      ? 'bg-violet-100 text-violet-700'
                      : 'bg-orange-100 text-orange-700'
                  }`}
                >
                  {c.contract_type}
                </span>
              </td>
              <td className="px-3 py-2 font-mono truncate max-w-[10rem]" title={c.bound_entity_id}>{c.bound_entity_id}</td>
              <td className="px-3 py-2">{c.status}</td>
              <td className="px-3 py-2 text-gray-500">{new Date(c.created_at).toLocaleString('zh-CN')}</td>
            </tr>
          ))}
          {!loading && rows.length === 0 && (
            <tr><td colSpan={6} className="px-3 py-6 text-center text-gray-400">暂无分润契约</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

/** 抖音引流转化流水 —— 每笔订单的分润明细 + 结算状态，运营核查用，带手动刷新。 */
function ConversionLogsTable({ refreshKey }: { refreshKey: number }) {
  const [rows, setRows] = useState<Awaited<ReturnType<typeof api.adminListClearnodeConversionLogs>>>([]);
  const [loading, setLoading] = useState(true);

  async function load() {
    setLoading(true);
    try {
      setRows(await api.adminListClearnodeConversionLogs());
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
            <th className="px-3 py-2 font-medium text-gray-600">order_id</th>
            <th className="px-3 py-2 font-medium text-gray-600">douyin_uid</th>
            <th className="px-3 py-2 font-medium text-gray-600">dividend_amount_cents (分)</th>
            <th className="px-3 py-2 font-medium text-gray-600">settlement_status</th>
            <th className="px-3 py-2 font-medium text-gray-600">created_at</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100">
          {rows.map((l) => (
            <tr key={l.id} className="hover:bg-gray-50">
              <td className="px-3 py-2 font-mono">{l.id}</td>
              <td className="px-3 py-2 font-mono">{l.order_id}</td>
              <td className="px-3 py-2 font-mono truncate max-w-[10rem]" title={l.douyin_uid}>{l.douyin_uid}</td>
              <td className="px-3 py-2">{l.dividend_amount_cents}</td>
              <td className="px-3 py-2">
                <span
                  className={`px-1.5 py-0.5 rounded ${
                    l.settlement_status === 'settled'
                      ? 'bg-emerald-100 text-emerald-700'
                      : 'bg-gray-100 text-gray-600'
                  }`}
                >
                  {l.settlement_status}
                </span>
              </td>
              <td className="px-3 py-2 text-gray-500">{new Date(l.created_at).toLocaleString('zh-CN')}</td>
            </tr>
          ))}
          {!loading && rows.length === 0 && (
            <tr><td colSpan={6} className="px-3 py-6 text-center text-gray-400">暂无转化流水</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

export default function GrowthPage() {
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
      <h1 className="text-2xl font-bold mb-2">增长运维 — 抖音拓客与数字领主</h1>
      <p className="text-sm text-gray-500 mb-6">
        云加盟认领、领主契约册封、转化落账。每张卡片提交后原样展示后端返回的 JSON
        （绿色=completed，红色=failed），方便直接核对 decision_trail / fingerprint 等字段。
      </p>

      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-gray-500">分润契约 / 转化流水</h2>
        <button
          onClick={() => setListRefreshKey((k) => k + 1)}
          className="text-xs text-blue-600 hover:underline"
        >
          刷新列表
        </button>
      </div>
      <AffiliateContractsTable refreshKey={listRefreshKey} />
      <ConversionLogsTable refreshKey={listRefreshKey} />

      <OpsSection title="抖音拓客与数字领主">
        <ActionCard
          title="云加盟认领 process_cloud_franchise_claim_workflow"
          badge="public"
          desc="抖音粉丝 0 元认领【微仓硬件盲盒】→ 秒级核验该 GPS 坐标方圆 1 公里内是否已有节点 (含还没装硬件的节点，纯 haversine 计算，非 PostGIS)。无冲突批准，生成新节点 (status='pending_hardware')；有冲突判 rejected，不是系统故障。"
          fields={[
            { key: 'douyin_uid', label: 'douyin_uid' },
            { key: 'address', label: 'address' },
            { key: 'lat', label: 'lat', type: 'number' },
            { key: 'lon', label: 'lon', type: 'number' },
          ]}
          defaults={ctx}
          onSubmit={(v) => api.clearnodeProcessCloudFranchiseClaim(v as any)}
          onResult={(r) => merge(r as any)}
        />
        <ActionCard
          title="领主册封 bind_digital_lord_contract_workflow"
          badge="admin"
          desc="小区意向金达成 500 单 ('点火成功') 后，运营核实达标手动触发：把该节点的永久领主税契约 (万分之二流水抽成) 写入底层，同时把节点从 pending_hardware 促活成 active。同一个达人重复调用幂等；换一个达人调用会被拒绝 (一个节点只能有一个创始领主)。"
          fields={[
            { key: 'douyin_uid', label: 'douyin_uid' },
            { key: 'location_id', label: 'location_id' },
          ]}
          defaults={ctx}
          onSubmit={(v) => api.clearnodeBindDigitalLordContract(v as any)}
          onResult={() => setListRefreshKey((k) => k + 1)}
        />
        <ActionCard
          title="转化落账 record_douyin_conversion_workflow"
          badge="admin"
          desc="订单确认后触发：领主税按订单落在哪个节点判定 (跟本次请求带的 douyin_uid 无关，只要节点有生效领主就抽成)；雇佣兵悬赏只有带了 douyin_uid 才判定 (按批次距离销毁的紧迫程度算佣金比例)。两条都不适用时依旧 completed，conversions 为空，不是失败。"
          fields={[
            { key: 'order_id', label: 'order_id' },
            { key: 'douyin_uid', label: 'douyin_uid (可选，抖音引荐人)' },
          ]}
          defaults={ctx}
          onSubmit={(v) => api.clearnodeRecordDouyinConversion(v as any)}
          onResult={() => setListRefreshKey((k) => k + 1)}
        />
      </OpsSection>
    </div>
  );
}
