'use client';

/**
 * <OfflineTicketView> — 弱网离线核销凭证 (Phase 9 SPEC §5)
 *
 * 若大妈在地下车库断网打开 App：探测到 navigator.onLine === false 时，
 * 从 Service Worker Cache API 读取最新提货码，全屏最高亮度渲染，
 * 确保微仓扫码枪能反向读取。
 */

import { useEffect, useState } from 'react';
import type { PickupTicket } from '@/types/api';

export default function OfflineTicketView() {
  const [ticket, setTicket] = useState<PickupTicket | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    async function readTicket() {
      try {
        // 1. 先试 Service Worker Cache API (最新一张)
        if ('caches' in window) {
          const cache = await caches.open('hemall-pickup-tickets-v1');
          const resp = await cache.match('pickup:latest');
          if (resp) {
            const data = (await resp.json()) as PickupTicket;
            setTicket(data);
            setLoading(false);
            return;
          }
        }
        // 2. 兜底 localStorage
        const raw = localStorage.getItem('hemall_pickup_ticket');
        if (raw) {
          setTicket(JSON.parse(raw) as PickupTicket);
        }
      } catch {
        /* ignore */
      } finally {
        setLoading(false);
      }
    }
    readTicket();
  }, []);

  // 在线时不需要离线视图
  if (navigator.onLine) return null;

  if (loading) {
    return (
      <div className="fixed inset-0 z-[999] flex items-center justify-center bg-black">
        <p className="text-white">正在读取离线凭证...</p>
      </div>
    );
  }

  if (!ticket) {
    return (
      <div className="fixed inset-0 z-[999] flex flex-col items-center justify-center bg-black px-8 text-center text-white">
        <div className="mb-3 text-5xl">📵</div>
        <h1 className="mb-2 text-xl font-black">当前处于离线状态</h1>
        <p className="text-sm text-gray-400">
          未找到已缓存的提货码。请联网后在「订单记录」中重新获取。
        </p>
      </div>
    );
  }

  return (
    <div
      className="fixed inset-0 z-[999] flex flex-col items-center justify-center bg-white"
      style={{ filter: 'brightness(1.1) contrast(1.05)' }}
    >
      <div className="mb-1 text-xs font-bold uppercase tracking-widest text-gray-400">
        离线核销凭证 · 请展示给取货员
      </div>
      <h1 className="mb-4 text-2xl font-black text-gray-900">📍 {ticket.node_name}</h1>

      {/* 提货码: 全屏最大亮度 + 大号字体 */}
      <div className="mx-4 w-full max-w-sm rounded-3xl border-2 border-dashed border-emerald-600 bg-white px-4 py-6 text-center">
        <div
          className="break-all font-black tracking-[0.12em] text-black"
          style={{ fontSize: 22, fontWeight: 800, wordBreak: 'break-all' }}
        >
          {ticket.pickup_code}
        </div>
      </div>

      <div className="mt-5 text-sm text-gray-600">
        实付 <span className="font-black text-emerald-700">¥{(ticket.grand_total_cents / 100).toFixed(2)}</span>
      </div>
      <div className="mt-1 text-xs text-gray-400">
        有效期至 {new Date(ticket.expires_at).toLocaleString('zh-CN')}
      </div>
    </div>
  );
}
