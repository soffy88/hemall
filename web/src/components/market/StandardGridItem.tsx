'use client';

/**
 * <StandardGridItem> — 高密网格组件 (Phase 9 SPEC §2.2)
 *
 * 渲染条件: tag_type = standard (刚需生鲜: 土豆/鸡蛋)
 * 一排两列/三列，去除所有干扰信息——只留商品图、现价、加号。
 * 整个卡片就是一个巨大的点击热区；右下角 [+] 绑定 300ms 物理防抖。
 */

import { useRef, useState } from 'react';
import type { FeedItem } from '@/types/api';

interface StandardGridItemProps {
  item: FeedItem;
  onLock: (item: FeedItem) => void;
  locked?: boolean;
}

export default function StandardGridItem({ item, onLock, locked = false }: StandardGridItemProps) {
  const [debouncing, setDebouncing] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  /** 300ms 物理防抖: 颤抖的手指连续误触只触发一次锁单。 */
  function handleDebouncedLock() {
    if (debouncing || locked || item.stock_qty === 0) return;
    setDebouncing(true);
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => {
      setDebouncing(false);
      onLock(item);
    }, 300);
  }

  return (
    <button
      type="button"
      onClick={handleDebouncedLock}
      disabled={locked || item.stock_qty === 0}
      aria-label={`抢购 ${item.sku_name}`}
      className={[
        'relative flex w-full flex-col overflow-hidden rounded-2xl bg-white text-left',
        'border border-gray-200 shadow-sm transition-all active:scale-95',
        // 适老化: 整卡大热区 + 16px 透明外扩
        'min-h-[160px]',
        locked ? 'ring-2 ring-amber-400' : 'hover:border-emerald-300',
      ].join(' ')}
    >
      {/* 商品图区 */}
      <div className="flex h-24 items-center justify-center overflow-hidden bg-gradient-to-br from-emerald-50 to-teal-100">
        {item.media_url ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={item.media_url} alt={item.sku_name} className="h-full w-full object-cover" loading="lazy" />
        ) : (
          <span className="text-4xl" aria-hidden>🥬</span>
        )}
      </div>

      {/* 信息区: 只留现价 + 加号 */}
      <div className="flex flex-1 items-end justify-between gap-1 px-2.5 py-2">
        <div className="min-w-0">
          <div className="truncate text-[13px] font-semibold text-gray-800">{item.sku_name}</div>
          <div className="font-black text-emerald-700 leading-tight" style={{ fontSize: 24, fontWeight: 800 }}>
            ¥{(item.retail_price / 100).toFixed(2)}
          </div>
        </div>

        {/* [+] 加号: 巨大热区 + 透明外扩 */}
        <div className="relative shrink-0 pb-1 pr-1">
          <span className="absolute -inset-3" aria-hidden />
          <span
            className={[
              'flex h-11 w-11 items-center justify-center rounded-full text-white',
              locked
                ? 'bg-amber-500'
                : item.stock_qty === 0
                  ? 'bg-gray-300'
                  : 'bg-emerald-600',
            ].join(' ')}
            style={{ fontSize: 24, fontWeight: 800 }}
            aria-hidden
          >
            {locked ? '✓' : '+'}
          </span>
        </div>
      </div>

      {/* 售罄遮罩 */}
      {item.stock_qty === 0 && (
        <div className="absolute inset-0 flex items-center justify-center bg-black/40 text-lg font-black text-white">
          已售罄
        </div>
      )}
    </button>
  );
}
