/** lib/cart-store 纯逻辑单测（audit 核验：购物车/结账要有单测）。
 *
 * 覆盖乐观购物车状态机的可测边界：
 *  - 本地持久化只剔除已过期/已失败的锁，不复活死锁；
 *  - localStorage 内容损坏时安全退回空车；
 *  - 倒计时格式化 mm:ss；
 *  - 触觉反馈区分短震（tap）与长震（fail）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { formatCountdown, haptic, loadPersisted } from '@/lib/cart-store';

const STORAGE_KEY = 'hemall_optimistic_cart_v9';
const NOW = 1_700_000_000_000;

function writeCart(items: Record<string, unknown>): void {
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify(items));
}

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});

afterEach(() => {
  vi.useRealTimers();
});

describe('购物车持久化恢复', () => {
  it('未持久化任何内容时返回空车', () => {
    expect(loadPersisted()).toEqual({});
  });

  it('恢复仍在有效期内且未失败的锁', () => {
    writeCart({
      b1: { batch_id: 'b1', retail_price: 1200, qty: 2, locked_until: NOW + 60_000, status: 'locked' },
    });
    expect(loadPersisted()).toEqual({
      b1: { batch_id: 'b1', retail_price: 1200, qty: 2, locked_until: NOW + 60_000, status: 'locked' },
    });
  });

  it('恢复时剔除已过期的锁（不会复活死锁）', () => {
    writeCart({
      dead: { batch_id: 'dead', retail_price: 100, qty: 1, locked_until: NOW - 1, status: 'locked' },
      live: { batch_id: 'live', retail_price: 100, qty: 1, locked_until: NOW + 1, status: 'locking' },
    });
    const restored = loadPersisted();
    expect(Object.keys(restored)).toEqual(['live']);
  });

  it('恢复时剔除 status=failed 的条目，即使锁未到期', () => {
    writeCart({
      bad: { batch_id: 'bad', retail_price: 100, qty: 1, locked_until: NOW + 60_000, status: 'failed' },
    });
    expect(loadPersisted()).toEqual({});
  });

  it('持久化内容不是合法 JSON 时安全退回空车', () => {
    window.localStorage.setItem(STORAGE_KEY, '{not json');
    expect(loadPersisted()).toEqual({});
  });

  it('持久化内容是 null/数组等异常结构时不抛错', () => {
    window.localStorage.setItem(STORAGE_KEY, 'null');
    expect(loadPersisted()).toEqual({});
    window.localStorage.setItem(STORAGE_KEY, '[1,2,3]');
    expect(() => loadPersisted()).not.toThrow();
  });
});

describe('倒计时格式化', () => {
  it.each([
    [0, '00:00'],
    [1, '00:01'],
    [59_000, '00:59'],
    [60_000, '01:00'],
    [61_500, '01:02'], // 向上取整
    [599_000, '09:59'],
    [3_599_000, '59:59'],
    [3_600_000, '60:00'],
    [-5_000, '00:00'], // 负数夹到 0
  ])('%ims → %s', (ms, expected) => {
    expect(formatCountdown(ms)).toBe(expected);
  });
});

describe('触觉反馈', () => {
  it('tap 短震 50ms', () => {
    const vibrate = vi.fn();
    vi.stubGlobal('navigator', { vibrate });
    haptic('tap');
    expect(vibrate).toHaveBeenCalledWith(50);
  });

  it('fail 是长震节奏', () => {
    const vibrate = vi.fn();
    vi.stubGlobal('navigator', { vibrate });
    haptic('fail');
    expect(vibrate).toHaveBeenCalledWith([80, 60, 120]);
  });

  it('不支持 vibrate 的环境不抛错', () => {
    vi.stubGlobal('navigator', {});
    expect(() => haptic('tap')).not.toThrow();
  });
});