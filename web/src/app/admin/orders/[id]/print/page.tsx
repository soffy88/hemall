'use client';

import { useEffect, useState } from 'react';
import { useParams } from 'next/navigation';
import Link from 'next/link';
import { api } from '@/lib/api-client';
import { errMessage } from '@/lib/errors';

type Summary = {
  paper: string;
  order: {
    id: string; status: string; currency: string; subtotal_cents: number;
    discount_cents: number; tax_cents: number; shipping_cents: number;
    grand_total_cents: number; shipping_method_code: string | null;
    weigh_status: string | null; created_at: string;
    shipping_address: Record<string, string>;
  };
  lines: Array<{
    id: string; title: string; sku: string; quantity: number; unit_price_cents: number;
    line_total_cents: number; pricing_mode: string; unit_price_cents_per_jin: number | null;
    estimated_weight_grams: number | null; actual_weight_grams: number | null;
    estimated_amount_cents: number | null; actual_amount_cents: number | null;
    settlement_status: string;
  }>;
  settlements: Array<{ id: string; order_line_item_id: string; weight_grams: number; diff_cents: number; status: string }>;
};

function yuan(cents: number): string {
  return `¥${(cents / 100).toFixed(2)}`;
}

export default function OrderPrintPage() {
  const params = useParams<{ id: string }>();
  const id = params?.id;
  const [paper, setPaper] = useState<'58' | 'a4'>('a4');
  const [data, setData] = useState<Summary | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!id) return;
    api.storeOpsOrderSummary(id, paper)
      .then((d) => setData(d as unknown as Summary))
      .catch((e) => setError(errMessage(e)));
  }, [id, paper]);

  return (
    <div>
      <style>{`
        @media print {
          body * { visibility: hidden; }
          #print-area, #print-area * { visibility: visible; }
          #print-area { position: absolute; left: 0; top: 0; width: 100%; }
          .no-print { display: none !important; }
        }
        @page { margin: 6mm; }
      `}</style>
      <div className="no-print flex items-center justify-between mb-4">
        <h1 className="text-2xl font-bold">订单打印</h1>
        <div className="flex gap-2">
          <button onClick={() => setPaper('58')}
            className={`px-3 py-2 rounded-lg text-sm border ${paper === '58' ? 'bg-emerald-600 text-white' : 'bg-white'}`}>58mm 小票</button>
          <button onClick={() => setPaper('a4')}
            className={`px-3 py-2 rounded-lg text-sm border ${paper === 'a4' ? 'bg-emerald-600 text-white' : 'bg-white'}`}>A4 拣货单</button>
          <button onClick={() => window.print()} className="px-3 py-2 rounded-lg text-sm bg-gray-900 text-white">打印</button>
          <Link href="/admin/orders" className="px-3 py-2 rounded-lg text-sm border bg-white">返回订单</Link>
        </div>
      </div>
      <div className="no-print text-sm text-gray-500 mb-4">
        小票与 A4 均含称重商品预估与实称栏位；浏览器打印，不接硬件打印机。
      </div>
      {error && <div className="text-red-600 text-sm mb-3">{error}</div>}

      {data && (
        <div id="print-area" className="mx-auto bg-white text-black"
          style={paper === '58' ? { width: '58mm', fontSize: '10px', lineHeight: 1.45, padding: '2mm' }
            : { width: '190mm', fontSize: '13px', padding: '4mm' }}>
          <div style={{ textAlign: 'center', marginBottom: 4 }}>
            <div style={{ fontWeight: 700, fontSize: paper === '58' ? '13px' : '18px' }}>Hemall 惠环社区店</div>
            <div>{paper === '58' ? '拣货小票' : '拣货单 / 小票'}</div>
            <div>{data.order.shipping_method_code === 'pickup' ? '到店自提' : '配送'}</div>
          </div>
          <div style={{ borderTop: '1px dashed #000', margin: '4px 0' }} />
          <div>订单：{data.order.id}</div>
          <div>时间：{new Date(data.order.created_at).toLocaleString('zh-CN')}</div>
          <div>状态：{data.order.status}{data.order.weigh_status ? ` / 称重:${data.order.weigh_status}` : ''}</div>
          {data.order.shipping_address?.recipient_name && (
            <div>
              收货：{data.order.shipping_address.recipient_name} {data.order.shipping_address.phone}<br />
              {data.order.shipping_address.address_line1} {data.order.shipping_address.city}
            </div>
          )}
          <div style={{ borderTop: '1px dashed #000', margin: '4px 0' }} />
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ textAlign: 'left' }}>
                <th>商品</th><th style={{ width: '14%' }}>数量</th>
                <th style={{ width: '20%' }}>预估</th><th style={{ width: '20%' }}>实称</th>
              </tr>
            </thead>
            <tbody>
              {data.lines.map((l) => (
                <tr key={l.id} style={{ verticalAlign: 'top' }}>
                  <td>{l.title}<br /><span style={{ color: '#444' }}>{l.sku}</span></td>
                  <td>{l.quantity}{l.pricing_mode === 'weight' ? ' 份' : ' 件'}</td>
                  <td>
                    {l.pricing_mode === 'weight'
                      ? `${l.estimated_weight_grams ?? 0}g / ${yuan(l.estimated_amount_cents ?? 0)}`
                      : yuan(l.line_total_cents)}
                  </td>
                  <td>{l.pricing_mode === 'weight'
                    ? (l.actual_weight_grams != null
                      ? `${l.actual_weight_grams}g / ${yuan(l.actual_amount_cents ?? 0)}`
                      : '________ g')
                    : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div style={{ borderTop: '1px dashed #000', margin: '4px 0' }} />
          <div style={{ display: 'flex', justifyContent: 'space-between' }}><span>小计</span><span>{yuan(data.order.subtotal_cents)}</span></div>
          <div style={{ display: 'flex', justifyContent: 'space-between' }}><span>优惠</span><span>-{yuan(data.order.discount_cents)}</span></div>
          <div style={{ display: 'flex', justifyContent: 'space-between' }}><span>运费</span><span>{yuan(data.order.shipping_cents)}</span></div>
          <div style={{ display: 'flex', justifyContent: 'space-between', fontWeight: 700 }}><span>合计</span><span>{yuan(data.order.grand_total_cents)}</span></div>
          {paper === 'a4' && (
            <>
              <div style={{ borderTop: '1px dashed #000', margin: '6px 0' }} />
              <div>拣货人：__________　复核：__________　实称人：__________</div>
              <div style={{ marginTop: 6 }}>备注：称重商品以实称为准，差额按多退少补处理。</div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
