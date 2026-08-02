'use client';

import { useState } from 'react';
import { api } from '@/lib/api-client';
import { ActionCard, OpsSection } from '@/components/ops/OpsActionCard';

/**
 * 分润结算运维控制台 —— 大妈计件工资 / 车库宿主场地分润。原独立运维页面
 * 页面 §4.4 去中心化分润结算里的两张卡片搬迁至此（自动补货挪到了库存与渠道
 * 页面，语义上更贴合"补货"而不是"分润"），统一改造 Phase 4.3。
 */

export default function SettlementPage() {
  const [ctx] = useState<Record<string, string>>({});

  return (
    <div>
      <h1 className="text-2xl font-bold mb-2">分润结算运维</h1>
      <p className="text-sm text-gray-500 mb-6">
        大妈计件工资、车库宿主场地分润。每张卡片提交后原样展示后端返回的 JSON
        （绿色=completed，红色=failed），方便直接核对 decision_trail / fingerprint 等字段。
      </p>

      <OpsSection title="去中心化分润结算">
        <ActionCard
          title="大妈工资 dispatch_labor_payment"
          badge="admin"
          desc="聚合某个大妈名下全部 pending 计件工资，一次性结清。"
          fields={[
            { key: 'worker_id', label: 'worker_id' },
            { key: 'payout_account', label: 'payout_account' },
          ]}
          defaults={ctx}
          onSubmit={(v) => api.hemallDispatchLaborPayment(v as any)}
        />
        <ActionCard
          title="宿主分润 dispatch_host_dividend"
          badge="admin"
          desc="按 tote 中转量给车库宿主结算场地分润 (tote_count 目前需人工统计传入)。"
          fields={[
            { key: 'host_id', label: 'host_id' },
            { key: 'location_id', label: 'location_id' },
            { key: 'payout_account', label: 'payout_account' },
            { key: 'tote_count', label: 'tote_count', type: 'number' },
          ]}
          defaults={ctx}
          onSubmit={(v) => api.hemallDispatchHostDividend(v as any)}
        />
      </OpsSection>
    </div>
  );
}
