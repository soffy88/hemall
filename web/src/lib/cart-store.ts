'use client';

/**
 * 薛定谔购物车状态机 (Phase 9: The Optimistic Cart State Machine)
 *
 * 前端购物车状态管理是极速交互的命脉。采用乐观 UI 锁 (Optimistic UI
 * Locking) 配合后端 TTL (Time-To-Live)：
 *
 *   状态机: locking → locked → (failed | expired)
 *   倒计时: 取所有商品中最小的 locked_until，悬浮在购物车图标上
 *   持久化: localStorage 硬缓存 (弱网/杀进程恢复)
 *
 * 设计要点:
 *   - 点击瞬间触觉反馈 (navigator.vibrate 50ms) + 乐观置为 locking
 *   - 后端返回 locked_until (now + 5min) 后拨转为 locked
 *   - 被抢走 (status failed) → 长震动 + 强制剔除
 *   - 倒计时归零 → 强制 expired，清空购物车
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from '@/lib/api-client';
import type { FeedItem, OptimisticCartItem } from '@/types/api';

const STORAGE_KEY = 'hemall_optimistic_cart_v9';
const TICK_MS = 1000;

/** 触觉引擎: 短震 (点击瞬间 50ms) / 长震 (冲突失败 200ms)。 */
export function haptic(kind: 'tap' | 'fail' = 'tap') {
  if (typeof navigator !== 'undefined' && 'vibrate' in navigator) {
    navigator.vibrate(kind === 'tap' ? 50 : [80, 60, 120]);
  }
}

function loadPersisted(): Record<string, OptimisticCartItem> {
  if (typeof window === 'undefined') return {};
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as Record<string, OptimisticCartItem>;
    const now = Date.now();
    // 恢复时直接剔除已过期的锁
    const alive: Record<string, OptimisticCartItem> = {};
    for (const [k, v] of Object.entries(parsed)) {
      if (v.locked_until > now && v.status !== 'failed') alive[k] = v;
    }
    return alive;
  } catch {
    return {};
  }
}

export function useOptimisticCart() {
  const [items, setItems] = useState<Record<string, OptimisticCartItem>>(loadPersisted);
  const [now, setNow] = useState(() => Date.now());
  const itemsRef = useRef(items);
  itemsRef.current = items;

  // 持久化
  useEffect(() => {
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(items));
    } catch {
      /* quota exceeded — 忽略，购物车仍可用 */
    }
  }, [items]);

  // 倒计时心跳 (1s tick)
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), TICK_MS);
    return () => clearInterval(timer);
  }, []);

  // tick 时把过期项拨转为 expired 并剔除
  useEffect(() => {
    setItems((prev) => {
      let changed = false;
      const next: Record<string, OptimisticCartItem> = {};
      for (const [k, v] of Object.entries(prev)) {
        if (v.locked_until > now) {
          next[k] = v;
        } else if (v.status === 'locked') {
          next[k] = { ...v, status: 'expired' };
          changed = true;
        }
      }
      if (changed) {
        // 全部过期 → 清空 (SPEC: 提示"库存锁已释放给网络")
        return next;
      }
      return prev;
    });
  }, [now]);

  const lockItem = useCallback(async (item: FeedItem) => {
    haptic('tap');
    // 乐观置入 locking
    const batchId = item.batch_id;
    setItems((prev) => {
      const existing = prev[batchId];
      if (existing && existing.locked_until > Date.now() && existing.status === 'locked') {
        return prev; // 已锁，幂等
      }
      return {
        ...prev,
        [batchId]: {
          batch_id: batchId,
          sku_name: item.sku_name,
          retail_price: item.retail_price,
          qty: 1,
          locked_until: Date.now() + 5 * 60 * 1000, // 乐观占位 5min
          status: 'locking',
        },
      };
    });

    try {
      const res = await api.lockCartItem(batchId);
      if (res.status === 'locked' && res.locked_until) {
        setItems((prev) => ({
          ...prev,
          [batchId]: {
            ...prev[batchId],
            locked_until: new Date(res.locked_until!).getTime(),
            status: 'locked',
          },
        }));
      } else {
        // 已被邻居抢先 → 长震动 + 剔除
        haptic('fail');
        setItems((prev) => {
          const next = { ...prev };
          delete next[batchId];
          return next;
        });
        throw new Error(
          res.reason === 'oversold'
            ? '物理冲突！已被邻居抢先锁单'
            : '物理冲突！该商品库存不足',
        );
      }
    } catch (e: any) {
      // 网络失败: 保留乐观锁但标记 failed? SPEC 规定失败即剔除。
      haptic('fail');
      setItems((prev) => {
        const next = { ...prev };
        delete next[batchId];
        return next;
      });
      throw e;
    }
  }, []);

  const removeItem = useCallback((batchId: string) => {
    setItems((prev) => {
      const next = { ...prev };
      delete next[batchId];
      return next;
    });
  }, []);

  const clear = useCallback(() => setItems({}), []);

  const values = useMemo(() => Object.values(items), [items]);

  const minLockedUntil = useMemo(() => {
    let min = Infinity;
    for (const v of values) {
      if (v.status === 'locked' || v.status === 'locking') {
        if (v.locked_until < min) min = v.locked_until;
      }
    }
    return min === Infinity ? 0 : min;
  }, [values]);

  const remainingMs = Math.max(0, minLockedUntil - now);
  const totalCents = values.reduce(
    (sum, v) => sum + (v.status === 'locked' ? v.retail_price * v.qty : 0),
    0,
  );
  const lockedCount = values.filter((v) => v.status === 'locked').length;

  return {
    items: values,
    lockItem,
    removeItem,
    clear,
    minLockedUntil,
    remainingMs,
    totalCents,
    lockedCount,
  };
}

/** 倒计时格式化 mm:ss (SPEC: 全局悬浮倒计时，字体 ≥24px 粗体)。 */
export function formatCountdown(ms: number): string {
  const totalSec = Math.max(0, Math.ceil(ms / 1000));
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}
