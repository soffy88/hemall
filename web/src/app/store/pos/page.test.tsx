/** POS 页面单测 — 测试纯逻辑函数（稳定判定、计价、金额汇总） */

import { describe, it, expect, vi, beforeEach } from 'vitest';

// 复制页面中的纯逻辑函数进行测试（避免渲染 React 组件）
const JIN_GRAMS = 500;

interface ScaleReading {
  weight_grams: number;
  unit: 'g' | 'kg';
  raw: string;
  stable: boolean;
  timestamp: number;
}

interface StableWeight {
  weight_grams: number;
  readings: ScaleReading[];
  source: 'scale' | 'simulator' | 'bridge';
}

interface RecognitionCandidate {
  variant_id: string;
  score: number;
  name: string;
  sku: string;
  price_cents: number;
  pricing_mode: 'piece' | 'weight';
  image_url?: string;
}

interface TicketLine {
  id: string;
  variant_id: string;
  name: string;
  sku: string;
  weight_grams: number;
  unit_price_cents: number;
  price_basis_grams: number;
  amount_cents: number;
  pricing_mode: 'piece' | 'weight';
}

const STABLE_COUNT = 3;
const STABLE_THRESHOLD_G = 2;

function isStable(window: ScaleReading[]): boolean {
  if (window.length < STABLE_COUNT) return false;
  const recent = window.slice(-STABLE_COUNT);
  for (let i = 1; i < recent.length; i++) {
    if (Math.abs(recent[i].weight_grams - recent[i - 1].weight_grams) > STABLE_THRESHOLD_G) {
      return false;
    }
  }
  return true;
}

function getStableWeight(window: ScaleReading[]): StableWeight | null {
  if (!isStable(window)) return null;
  const recent = window.slice(-STABLE_COUNT);
  const weights = recent.map(r => r.weight_grams).sort((a, b) => a - b);
  const median = weights[Math.floor(weights.length / 2)];
  return {
    weight_grams: median,
    readings: recent,
    source: 'scale',
  };
}

function calculateAmount(candidate: RecognitionCandidate, weight: number): number {
  if (candidate.pricing_mode === 'weight') {
    return Math.round((weight * candidate.price_cents) / JIN_GRAMS);
  }
  return candidate.price_cents;
}

describe('POS Page Logic', () => {
  describe('isStable', () => {
    it('returns false for insufficient readings', () => {
      const window: ScaleReading[] = [
        { weight_grams: 1000, unit: 'g', raw: '', stable: false, timestamp: Date.now() },
        { weight_grams: 1000, unit: 'g', raw: '', stable: false, timestamp: Date.now() },
      ];
      expect(isStable(window)).toBe(false);
    });

    it('returns true when readings within threshold', () => {
      const window: ScaleReading[] = [
        { weight_grams: 1000, unit: 'g', raw: '', stable: false, timestamp: Date.now() },
        { weight_grams: 1001, unit: 'g', raw: '', stable: false, timestamp: Date.now() },
        { weight_grams: 1000, unit: 'g', raw: '', stable: false, timestamp: Date.now() },
      ];
      expect(isStable(window)).toBe(true);
    });

    it('returns false when readings exceed threshold', () => {
      const window: ScaleReading[] = [
        { weight_grams: 1000, unit: 'g', raw: '', stable: false, timestamp: Date.now() },
        { weight_grams: 1005, unit: 'g', raw: '', stable: false, timestamp: Date.now() },
        { weight_grams: 1000, unit: 'g', raw: '', stable: false, timestamp: Date.now() },
      ];
      expect(isStable(window)).toBe(false);
    });
  });

  describe('getStableWeight', () => {
    it('returns median weight when stable', () => {
      const window: ScaleReading[] = [
        { weight_grams: 1000, unit: 'g', raw: '', stable: false, timestamp: Date.now() },
        { weight_grams: 1002, unit: 'g', raw: '', stable: false, timestamp: Date.now() },
        { weight_grams: 1001, unit: 'g', raw: '', stable: false, timestamp: Date.now() },
      ];
      const stable = getStableWeight(window);
      expect(stable).not.toBeNull();
      expect(stable!.weight_grams).toBe(1001); // median of [1000, 1001, 1002]
    });

    it('returns null when unstable', () => {
      const window: ScaleReading[] = [
        { weight_grams: 1000, unit: 'g', raw: '', stable: false, timestamp: Date.now() },
        { weight_grams: 1010, unit: 'g', raw: '', stable: false, timestamp: Date.now() },
        { weight_grams: 1000, unit: 'g', raw: '', stable: false, timestamp: Date.now() },
      ];
      expect(getStableWeight(window)).toBeNull();
    });
  });

  describe('calculateAmount', () => {
    it('calculates weight-based pricing correctly (per jin)', () => {
      const candidate: RecognitionCandidate = {
        variant_id: 'v1',
        score: 0.95,
        name: '苹果',
        sku: 'APL001',
        price_cents: 1000, // 10元/斤
        pricing_mode: 'weight',
      };
      // 500g = 1斤 -> 10元
      expect(calculateAmount(candidate, 500)).toBe(1000);
      // 1000g = 2斤 -> 20元
      expect(calculateAmount(candidate, 1000)).toBe(2000);
      // 250g = 0.5斤 -> 5元
      expect(calculateAmount(candidate, 250)).toBe(500);
    });

    it('calculates piece-based pricing correctly', () => {
      const candidate: RecognitionCandidate = {
        variant_id: 'v1',
        score: 0.95,
        name: '盒装牛奶',
        sku: 'MLK001',
        price_cents: 300, // 3元/件
        pricing_mode: 'piece',
      };
      expect(calculateAmount(candidate, 500)).toBe(300);
      expect(calculateAmount(candidate, 1000)).toBe(300);
    });
  });

  describe('ticket total calculation', () => {
    it('sums line amounts correctly', () => {
      const lines: TicketLine[] = [
        { id: '1', variant_id: 'v1', name: '苹果', sku: 'APL001', weight_grams: 500, unit_price_cents: 1000, price_basis_grams: 500, amount_cents: 1000, pricing_mode: 'weight' },
        { id: '2', variant_id: 'v2', name: '牛奶', sku: 'MLK001', weight_grams: 0, unit_price_cents: 300, price_basis_grams: 0, amount_cents: 300, pricing_mode: 'piece' },
        { id: '3', variant_id: 'v3', name: '面包', sku: 'BRD001', weight_grams: 0, unit_price_cents: 500, price_basis_grams: 0, amount_cents: 500, pricing_mode: 'piece' },
      ];
      const total = lines.reduce((sum, l) => sum + l.amount_cents, 0);
      expect(total).toBe(1800);
    });

    it('calculates change correctly', () => {
      const totalAmount = 1800;
      const cashReceived = 2000;
      const change = cashReceived - totalAmount;
      expect(change).toBe(200);
    });
  });
});