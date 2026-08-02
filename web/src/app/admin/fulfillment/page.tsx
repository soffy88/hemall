'use client';

import { useState } from 'react';
import { api } from '@/lib/api-client';
import { ActionCard, OpsSection } from '@/components/ops/OpsActionCard';

/**
 * 履约运维控制台 —— 拣货确认、幽灵库存上报、邻居代送确认。原 /admin/clearnode
 * 页面 §4.3 物理流转 + v2.0 物理世界异常处理里跟"履约"直接相关的三张卡片
 * 搬迁至此，统一改造 Phase 4.3。
 */

export default function FulfillmentPage() {
  const [ctx, setCtx] = useState<Record<string, string>>({});

  function merge(patch: Record<string, unknown>) {
    const next: Record<string, string> = {};
    for (const [k, v] of Object.entries(patch)) {
      if (typeof v === 'string' || typeof v === 'number') next[k] = String(v);
    }
    setCtx((c) => ({ ...c, ...next }));
  }

  return (
    <div>
      <h1 className="text-2xl font-bold mb-2">履约运维</h1>
      <p className="text-sm text-gray-500 mb-6">
        拣货确认、幽灵库存上报、邻居代送确认。每张卡片提交后原样展示后端返回的 JSON
        （绿色=completed，红色=failed），方便直接核对 decision_trail / fingerprint 等字段。
      </p>

      <OpsSection title="物理流转">
        <ActionCard
          title="拣货确认 confirm_batch_pick"
          badge="admin"
          desc="扫码/CV 验证正确后转出库，顺带按当前积压深度记一笔计件工资。"
          fields={[
            { key: 'order_line_item_id', label: 'order_line_item_id' },
            { key: 'worker_id', label: 'worker_id' },
          ]}
          defaults={ctx}
          onSubmit={(v) => api.clearnodeConfirmBatchPick(v as any)}
        />
        <ActionCard
          title="幽灵库存上报 report_phantom_stock_workflow"
          badge="admin"
          desc="大妈拣货发现实物丢失：清零该批次库存，秒退顾客，记入节点损耗账本。"
          fields={[
            { key: 'order_line_item_id', label: 'order_line_item_id' },
            { key: 'worker_id', label: 'worker_id' },
            { key: 'reason', label: 'reason (默认 phantom_stock)' },
          ]}
          defaults={ctx}
          onSubmit={(v) => api.clearnodeReportPhantomStock(v as any)}
        />
        <ActionCard
          title="邻居代送确认 execute_peer_delivery_workflow"
          badge="public"
          desc="确认邻居扫码接单：释放 Tote，扣买家悬赏金，打入代送邻居余额。"
          fields={[
            { key: 'order_id', label: 'order_id' },
            { key: 'tote_id', label: 'tote_id' },
            { key: 'neighbor_id', label: 'neighbor_id' },
            { key: 'bounty_amount', label: 'bounty_amount (分)', type: 'number' },
          ]}
          defaults={ctx}
          onSubmit={(v) => api.clearnodeExecutePeerDelivery(v as any)}
        />
      </OpsSection>
    </div>
  );
}
