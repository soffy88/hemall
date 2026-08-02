'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { api } from '@/lib/api-client';
import { formatMoney } from '@/lib/format';
import type { Order } from '@/types/api';

export default function SuccessPage() {
  const [order, setOrder] = useState<Order | null>(null);
  const fmt = (cents: number) => formatMoney(cents, order?.currency);
  const [receiptToken, setReceiptToken] = useState('');
  const [error, setError] = useState('');

  useEffect(() => {
    const token = sessionStorage.getItem('hemall_receipt_token');
    const orderId = sessionStorage.getItem('hemall_order_id');
    if (token) {
      setReceiptToken(token);
      api.lookupOrder(token)
        .then(setOrder)
        .catch((e) => setError(e.message));
    }
  }, []);

  return (
    <div className="max-w-xl mx-auto text-center py-8">
      <div className="text-6xl mb-4">🎉</div>
      <h1 className="text-2xl font-bold text-gray-900 mb-2">下单成功！</h1>
      <p className="text-gray-500 mb-6">感谢您的购买，请保存以下收据信息</p>

      {error && <div className="text-red-600 text-sm mb-4">{error}</div>}

      {order && (
        <div className="bg-white rounded-xl border border-gray-200 p-5 text-left space-y-3 mb-6">
          <div className="text-sm space-y-1">
            <div className="flex justify-between">
              <span className="text-gray-500">订单号</span>
              <span className="font-mono text-xs">{order.id}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-gray-500">状态</span>
              <span className="text-amber-700 font-medium">{order.status}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-gray-500">总额</span>
              <span className="font-bold text-emerald-700">{fmt(order.grand_total_cents)}</span>
            </div>
          </div>
          <div className="border-t pt-3">
            <div className="text-xs text-gray-500 mb-2">商品明细</div>
            {(order.line_items || []).map((li) => (
              <div key={li.id} className="flex justify-between text-sm py-1">
                <span>{li.product_title || '—'} × {li.quantity}</span>
                <span>{fmt(li.line_total_cents)}</span>
              </div>
            ))}
          </div>
          {order.shipping_address && (
            <div className="border-t pt-3 text-xs">
              <div className="text-gray-500 mb-1">收货地址</div>
              <div>{order.shipping_address.recipient_name} {order.shipping_address.phone}</div>
              <div>{order.shipping_address.address_line1} {order.shipping_address.city} {order.shipping_address.postal_code}</div>
            </div>
          )}
        </div>
      )}

      {receiptToken && (
        <div className="bg-gray-50 rounded-xl border border-gray-200 p-4 text-left mb-6">
          <div className="text-xs text-gray-500 mb-1">收据 Token（凭此查单）</div>
          <div className="font-mono text-xs break-all bg-white p-2 rounded border">{receiptToken}</div>
        </div>
      )}

      <div className="flex gap-3 justify-center">
        <Link href="/shop" className="px-5 py-2 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 transition text-sm">
          继续购物
        </Link>
        <Link href="/shop/lookup" className="px-5 py-2 bg-gray-100 text-gray-700 rounded-lg hover:bg-gray-200 transition text-sm">
          查单
        </Link>
      </div>
    </div>
  );
}
