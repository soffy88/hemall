'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { api } from '@/lib/api-client';

type AlertEvent = {
  type: string;
  at: string;
  order_id: string;
  title: string;
  detail: string;
  status: string;
};

/**
 * 门店新订单提醒（店内用，不对外发消息）。
 *
 * - 10 秒轮询 /admin/store-ops/alerts，只取“上次服务端时间之后”的新事件；
 * - 新待处理订单 / 顾客补差确认或拒绝 / 顾客到店自提都会提示；
 * - 页面提示 + WebAudio 提示音；浏览器通知需店员点“开启提醒”授权。
 */
export default function AdminOrderAlerts() {
  const router = useRouter();
  const sinceRef = useRef<string | null>(null);
  const [events, setEvents] = useState<AlertEvent[]>([]);
  const [open, setOpen] = useState(false);
  const [permission, setPermission] = useState<string>('default');
  const [soundOn, setSoundOn] = useState(true);
  const [flash, setFlash] = useState(false);

  const beep = useCallback(() => {
    if (!soundOn) return;
    try {
      const Ctx = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
      const ctx = new Ctx();
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'sine';
      osc.frequency.value = 880;
      gain.gain.value = 0.08;
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start();
      osc.stop(ctx.currentTime + 0.18);
      setTimeout(() => void ctx.close(), 400);
    } catch {
      // 浏览器未解锁音频时静默失败，不打断收银操作。
    }
  }, [soundOn]);

  const poll = useCallback(async () => {
    try {
      if (typeof window !== 'undefined' && 'Notification' in window) {
        setPermission((prev) => (prev === Notification.permission ? prev : Notification.permission));
      }
      const res = await api.storeOpsAlerts(sinceRef.current ?? undefined);
      sinceRef.current = res.server_time;
      if (res.events.length > 0) {
        setEvents((prev) => [...res.events, ...prev].slice(0, 30));
        setFlash(true);
        setTimeout(() => setFlash(false), 4000);
        beep();
        if (typeof window !== 'undefined' && 'Notification' in window && Notification.permission === 'granted') {
          for (const e of res.events.slice(0, 3)) {
            new Notification(e.title, { body: e.detail || 'Hemall 门店提醒', tag: `${e.type}:${e.order_id}` });
          }
        }
      }
    } catch {
      // 网络抖动/未登录时下一轮再试，不弹错误打扰门店。
    }
  }, [beep]);

  useEffect(() => {
    const kickoff = setTimeout(() => void poll(), 0);
    const timer = setInterval(() => void poll(), 10_000);
    return () => {
      clearTimeout(kickoff);
      clearInterval(timer);
    };
  }, [poll]);

  async function enableNotifications() {
    if (!('Notification' in window)) return;
    const result = await Notification.requestPermission();
    setPermission(result);
  }

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={`relative rounded-full px-3 py-1.5 text-sm font-medium transition ${
          flash ? 'bg-red-100 text-red-700 animate-pulse' : 'bg-gray-100 text-gray-700 hover:bg-gray-200'
        }`}
        title="门店提醒"
      >
        🔔 提醒
        {events.length > 0 && (
          <span className="ml-1 rounded-full bg-red-600 px-1.5 text-[11px] font-bold text-white">
            {events.length}
          </span>
        )}
      </button>

      {open && (
        <div className="absolute right-0 top-full z-50 mt-2 w-80 rounded-xl border border-gray-200 bg-white p-3 shadow-xl">
          <div className="flex items-center justify-between gap-2">
            <div className="text-sm font-semibold">门店提醒</div>
            <div className="flex gap-2">
              <button
                type="button"
                onClick={() => setSoundOn((v) => !v)}
                className="text-xs text-gray-500 hover:text-gray-800"
              >
                {soundOn ? '🔊 声音开' : '🔇 声音关'}
              </button>
              {permission !== 'granted' && (
                <button
                  type="button"
                  onClick={() => void enableNotifications()}
                  className="text-xs text-blue-600 hover:underline"
                >
                  开启浏览器通知
                </button>
              )}
            </div>
          </div>
          <div className="mt-2 max-h-80 space-y-1 overflow-auto">
            {events.map((e, i) => (
              <button
                key={`${e.type}-${e.order_id}-${e.at}-${i}`}
                type="button"
                onClick={() => {
                  router.push(`/admin/orders?order=${e.order_id}`);
                  setOpen(false);
                }}
                className="block w-full rounded-lg px-2 py-1.5 text-left hover:bg-gray-50"
              >
                <div className="text-xs font-semibold text-gray-800">{e.title}</div>
                <div className="text-[11px] text-gray-500">
                  {new Date(e.at).toLocaleTimeString('zh-CN')} · {e.detail || e.order_id.slice(0, 8)}
                </div>
              </button>
            ))}
            {events.length === 0 && (
              <div className="py-6 text-center text-xs text-gray-400">暂无新提醒</div>
            )}
          </div>
          <div className="mt-2 border-t pt-2 text-right">
            <Link href="/admin/today" className="text-xs text-emerald-700 hover:underline">
              打开今日待办 →
            </Link>
          </div>
        </div>
      )}
    </div>
  );
}
