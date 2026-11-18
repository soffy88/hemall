/** Pick Weigh 页面单测 — 测试纯逻辑函数（稳定判定、差额计算、进度计算） */

import { describe, it, expect } from 'vitest';

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

interface PickOrderLine {
  id: string;
  variant_id: string;
  name: string;
  sku: string;
  quantity: number;
  unit: string;
  price_cents: number;
  pricing_mode: 'piece' | 'weight';
  estimated_weight_grams?: number;
  actual_weight_grams?: number;
  estimated_amount_cents: number;
  actual_amount_cents?: number;
  diff_cents?: number;
  settled: boolean;
}

interface PickOrder {
  id: string;
  order_no: string;
  customer_name: string;
  customer_phone: string;
  status: string;
  lines: PickOrderLine[];
  created_at: string;
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

function calculateDiff(actualWeight: number, estimatedWeight: number, priceCents: number): number {
  // 按重量计价：实际金额 - 预估金额
  // 预估金额 = estimatedWeight * price / JIN_GRAMS
  // 实际金额 = actualWeight * price / JIN_GRAMS
  // 差额 = (actualWeight - estimatedWeight) * price / JIN_GRAMS
  return Math.round(((actualWeight - estimatedWeight) * priceCents) / JIN_GRAMS);
}

function calculateProgress(order: PickOrder): { progress: number; totalLines: number } {
  const totalLines = order.lines.length;
  const progress = order.lines.filter(l => l.settled).length;
  return { progress, totalLines };
}

describe('Pick Weigh Page Logic', () => {
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
      expect(stable!.weight_grams).toBe(1001);
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

  describe('calculateDiff', () => {
    it('calculates positive diff when actual > estimated', () => {
      // 预估 1kg (2斤)，实际 1.2kg (2.4斤)，单价 10元/斤
      // 预估金额 = 20元，实际金额 = 24元，差额 = +4元 = +400分
      const diff = calculateDiff(1200, 1000, 1000);
      expect(diff).toBe(400);
    });

    it('calculates negative diff when actual < estimated', () => {
      // 预估 1kg，实际 0.8kg，单价 10元/斤
      // 差额 = -0.4斤 * 10元 = -4元 = -400分
      const diff = calculateDiff(800, 1000, 1000);
      expect(diff).toBe(-400);
    });

    it('calculates zero diff when actual == estimated', () => {
      const diff = calculateDiff(1000, 1000, 1000);
      expect(diff).toBe(0);
    });

    it('handles different price per jin', () => {
      // 单价 20元/斤，预估 0.5kg (1斤)，实际 0.6kg (1.2斤)
      // 差额 = 0.2斤 * 20元 = 4元 = 400分
      const diff = calculateDiff(600, 500, 2000);
      expect(diff).toBe(400);
    });
  });

  describe('calculateProgress', () => {
    it('returns correct progress for mixed settled/unsettled lines', () => {
      const order: PickOrder = {
        id: 'o1',
        order_no: 'SO001',
        customer_name: '张三',
        customer_phone: '13800000000',
        status: 'pending_pick',
        lines: [
          { id: 'l1', variant_id: 'v1', name: '苹果', sku: 'APL001', quantity: 2, unit: '斤', price_cents: 1000, pricing_mode: 'weight', estimated_weight_grams: 1000, estimated_amount_cents: 2000, settled: true },
          { id: 'l2', variant_id: 'v2', name: '香蕉', sku: 'BAN001', quantity: 1, unit: '斤', price_cents: 500, pricing_mode: 'weight', estimated_weight_grams: 500, estimated_amount_cents: 500, settled: false },
          { id: 'l3', variant_id: 'v3', name: '牛奶', sku: 'MLK001', quantity: 1, unit: '盒', price_cents: 300, pricing_mode: 'piece', estimated_amount_cents: 300, settled: true },
        ],
        created_at: '2024-01-01T00:00:00Z',
      };
      const { progress, totalLines } = calculateProgress(order);
      expect(progress).toBe(2);
      expect(totalLines).toBe(3);
    });

    it('returns 0 progress when no lines settled', () => {
      const order: PickOrder = {
        id: 'o1',
        order_no: 'SO001',
        customer_name: '张三',
        customer_phone: '13800000000',
        status: 'pending_pick',
        lines: [
          { id: 'l1', variant_id: 'v1', name: '苹果', sku: 'APL001', quantity: 2, unit: '斤', price_cents: 1000, pricing_mode: 'weight', estimated_weight_grams: 1000, estimated_amount_cents: 2000, settled: false },
        ],
        created_at: '2024-01-01T00:00:00Z',
      };
      const { progress, totalLines } = calculateProgress(order);
      expect(progress).toBe(0);
      expect(totalLines).toBe(1);
    });

    it('returns full progress when all lines settled', () => {
      const order: PickOrder = {
        id: 'o1',
        order_no: 'SO001',
        customer_name: '张三',
        customer_phone: '13800000000',
        status: 'pending_pick',
        lines: [
          { id: 'l1', variant_id: 'v1', name: '苹果', sku: 'APL001', quantity: 2, unit: '斤', price_cents: 1000, pricing_mode: 'weight', estimated_weight_grams: 1000, estimated_amount_cents: 2000, settled: true },
          { id: 'l2', variant_id: 'v2', name: '香蕉', sku: 'BAN001', quantity: 1, unit: '斤', price_cents: 500, pricing_mode: 'weight', estimated_weight_grams: 500, estimated_amount_cents: 500, settled: true },
        ],
        created_at: '2024-01-01T00:00:00Z',
      };
      const { progress, totalLines } = calculateProgress(order);
      expect(progress).toBe(2);
      expect(totalLines).toBe(2);
    });
  });

  describe('next line index finding', () => {
    it('finds next unsettled line after current index', () => {
      const lines: PickOrderLine[] = [
        { id: 'l1', variant_id: 'v1', name: '苹果', sku: 'APL001', quantity: 2, unit: '斤', price_cents: 1000, pricing_mode: 'weight', estimated_weight_grams: 1000, estimated_amount_cents: 2000, settled: true },
        { id: 'l2', variant_id: 'v2', name: '香蕉', sku: 'BAN001', quantity: 1, unit: '斤', price_cents: 500, pricing_mode: 'weight', estimated_weight_grams: 500, estimated_amount_cents: 500, settled: false },
        { id: 'l3', variant_id: 'v3', name: '牛奶', sku: 'MLK001', quantity: 1, unit: '盒', price_cents: 300, pricing_mode: 'piece', estimated_amount_cents: 300, settled: false },
      ];
      const currentLineIndex = 0;
      const nextIndex = lines.findIndex((l, i) => i > currentLineIndex && !l.settled);
      expect(nextIndex).toBe(1);
    });

    it('returns -1 when all remaining lines are settled', () => {
      const lines: PickOrderLine[] = [
        { id: 'l1', variant_id: 'v1', name: '苹果', sku: 'APL001', quantity: 2, unit: '斤', price_cents: 1000, pricing_mode: 'weight', estimated_weight_grams: 1000, estimated_amount_cents: 2000, settled: true },
        { id: 'l2', variant_id: 'v2', name: '香蕉', sku: 'BAN001', quantity: 1, unit: '斤', price_cents: 500, pricing_mode: 'weight', estimated_weight_grams: 500, estimated_amount_cents: 500, settled: true },
      ];
      const currentLineIndex = 0;
      const nextIndex = lines.findIndex((l, i) => i > currentLineIndex && !l.settled);
      expect(nextIndex).toBe(-1);
    });
  });
});