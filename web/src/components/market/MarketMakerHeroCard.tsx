'use client';

/**
 * <MarketMakerHeroCard> — 零层级大卡片 (Phase 9 SPEC §2.1)
 *
 * 渲染条件: tag_type ∈ {clearance(暴降大卡), fresh(溯源大卡)}
 * 占据屏幕大面积，禁止点击进入详情页——整卡只有两个决策: 抢 / 不抢。
 *
 * 适老化硬性指标:
 *   - 价格/倒计时 font-weight 800, font-size ≥ 24px
 *   - 可交互按钮实际响应区向外扩展 ≥16px 透明 padding
 *   - 触觉反馈 (navigator.vibrate)
 */

import type { FeedItem } from '@/types/api';

interface MarketMakerHeroCardProps {
  item: FeedItem;
  onLock: (item: FeedItem) => void;
  /** 该商品是否已在购物车 (锁单中)，用于按钮态切换 */
  locked?: boolean;
  index?: number;
}

export default function MarketMakerHeroCard({ item, onLock, locked = false, index = 0 }: MarketMakerHeroCardProps) {
  const isPanic = item.stock_qty <= 5;
  const discountRate = item.benchmark_price > item.retail_price
    ? Math.round((1 - item.retail_price / item.benchmark_price) * 100)
    : 0;

  return (
    <article
      className="relative w-full overflow-hidden rounded-3xl bg-gray-900 text-white select-none snap-center"
      style={{ aspectRatio: '4 / 3' }}
      aria-label={item.sku_name}
    >
      {/* 媒体层: 自动静音循环播放物理实况 (无视频时降级为渐变占位) */}
      {item.media_url ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={item.media_url}
          alt={item.sku_name}
          className="absolute inset-0 h-full w-full object-cover"
          loading={index < 2 ? 'eager' : 'lazy'}
          draggable={false}
        />
      ) : (
        <div className={`absolute inset-0 bg-gradient-to-br ${index % 2 ? 'from-amber-700 via-orange-600 to-red-600' : 'from-emerald-800 via-teal-700 to-cyan-700'}`} />
      )}

      {/* 顶部: 行为插队徽标 + 暴降大卡角标 */}
      <div className="absolute top-3 left-3 right-3 flex items-start justify-between pointer-events-none">
        <div className="flex gap-2">
          {item.tag_type === 'clearance' && (
            <span className="rounded-full bg-red-600 px-3 py-1 text-sm font-extrabold tracking-wide">
              暴降 {discountRate}%
            </span>
          )}
          {item.tag_type === 'fresh' && (
            <span className="rounded-full bg-emerald-500 px-3 py-1 text-sm font-extrabold tracking-wide">
              🌱 产地溯源
            </span>
          )}
        </div>
        {item.affinity_boosted && (
          <span className="rounded-full bg-black/60 backdrop-blur px-3 py-1 text-xs font-bold text-amber-300">
            ⚡ 为你推荐
          </span>
        )}
      </div>

      {/* 盘口信息层 (悬浮于媒体之上) */}
      <div className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/85 via-black/50 to-transparent px-4 pb-4 pt-16">
        <h2 className="text-xl font-extrabold tracking-tight" style={{ fontSize: 24 }}>
          {item.sku_name}
        </h2>

        <div className="mt-1.5 flex items-baseline gap-2.5">
          <span
            className="font-black leading-none text-white"
            style={{ fontSize: 34, fontWeight: 800 }}
          >
            ¥{(item.retail_price / 100).toFixed(2)}
          </span>
          {item.benchmark_price > item.retail_price && (
            <span className="text-gray-400 line-through" style={{ fontSize: 22, fontWeight: 700 }}>
              ¥{(item.benchmark_price / 100).toFixed(2)}
            </span>
          )}
        </div>

        {/* 物理库存透出与动态警报 */}
        <div
          className={`mt-1.5 font-black ${isPanic ? 'animate-pulse text-red-400' : 'text-emerald-300'}`}
          style={{ fontSize: 24, fontWeight: 800 }}
        >
          {item.stock_qty === 0
            ? '已击穿'
            : isPanic
              ? `🚨 极度危险：仅剩 ${item.stock_qty} 份`
              : `当前节点剩余 ${item.stock_qty} 份`}
        </div>

        {/* 实况流速 (辅助动效) */}
        {item.observed_velocity > 0 && (
          <div className="mt-0.5 text-xs font-semibold text-white/70">
            🔥 过去 1 小时成交 {item.observed_velocity} 单
          </div>
        )}
      </div>

      {/* 极简决策层 */}
      <div className="absolute bottom-4 right-4">
        <button
          type="button"
          disabled={item.stock_qty === 0 || locked}
          onClick={() => onLock(item)}
          aria-label={`抢购 ${item.sku_name}`}
          className={[
            'relative rounded-2xl font-black transition-all active:scale-95',
            'border-4 border-white/90 shadow-2xl',
            // 适老化: 热区放大至 64x64 + 透明 padding 外扩 16px
            'min-w-[64px] min-h-[64px] px-4 py-3',
            'before:content-[""] before:absolute before:-inset-4 before:rounded-3xl',
            item.stock_qty === 0
              ? 'bg-gray-500 text-gray-300 cursor-not-allowed'
              : locked
                ? 'bg-amber-500 text-white cursor-wait'
                : 'bg-red-600 text-white hover:bg-red-500',
          ].join(' ')}
          style={{ fontSize: 24, fontWeight: 800 }}
        >
          {item.stock_qty === 0 ? '已击穿' : locked ? '已锁单' : '抢！'}
        </button>
      </div>
    </article>
  );
}
