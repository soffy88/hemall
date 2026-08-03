'use client';

import { useEffect, useMemo, useState } from 'react';
import Link from 'next/link';
import { api } from '@/lib/api-client';
import { formatMoney } from '@/lib/format';
import { getSelectedRegion } from '@/lib/region-store';
import { productIcon, productGradient } from '@/lib/product-visual';
import type { StorefrontProduct } from '@/types/api';

type SortMode = 'default' | 'price_asc' | 'price_desc';

export default function ShopPage() {
  const [products, setProducts] = useState<StorefrontProduct[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');
  const [minPrice, setMinPrice] = useState('');
  const [maxPrice, setMaxPrice] = useState('');
  const [sort, setSort] = useState<SortMode>('default');
  const [hotPicks, setHotPicks] = useState<StorefrontProduct[]>([]);

  async function load() {
    setLoading(true);
    try {
      const data = await api.storeProducts({
        search: search || undefined,
        min_price: minPrice ? Math.round(Number(minPrice) * 100) : undefined,
        max_price: maxPrice ? Math.round(Number(maxPrice) * 100) : undefined,
      });
      setProducts(data);
    } catch (e: any) {
      console.error(e);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, []);

  useEffect(() => {
    (async () => {
      try {
        const [hot, catalog] = await Promise.all([
          api.storeRecommendHot(8),
          api.storeProducts({ limit: 200 }),
        ]);
        const byId = new Map(catalog.map((p) => [p.id, p]));
        const resolved = hot.data.products
          .map((r) => byId.get(r.product_id))
          .filter((p): p is StorefrontProduct => Boolean(p));
        setHotPicks(resolved);
      } catch (e: any) {
        console.error(e);
      }
    })();
  }, []);

  const sortedProducts = useMemo(() => {
    if (sort === 'default') return products;
    const copy = [...products];
    copy.sort((a, b) => {
      const pa = a.min_price_cents ?? 0;
      const pb = b.min_price_cents ?? 0;
      return sort === 'price_asc' ? pa - pb : pb - pa;
    });
    return copy;
  }, [products, sort]);

  return (
    <div>
      {/* Hero */}
      <div className="relative overflow-hidden rounded-2xl bg-gradient-to-br from-emerald-700 via-emerald-600 to-teal-600 text-white px-8 py-12 mb-10">
        <div className="relative z-10 max-w-lg">
          <h1 className="text-3xl font-bold tracking-tight">好物商城，一站买齐</h1>
          <p className="mt-2 text-emerald-50/90">生鲜日用 · 数码好物，实时库存，多区域配送。</p>
        </div>
        <div className="pointer-events-none absolute -right-6 -bottom-10 text-[160px] opacity-20 select-none">🛍️</div>
      </div>

      {/* 生鲜日用超市入口 (对标小象超市) */}
      <div className="mb-3">
        <Link
          href="/shop/market"
          className="flex items-center justify-between rounded-2xl bg-gradient-to-r from-emerald-600 via-teal-500 to-cyan-500 px-5 py-4 text-white shadow-lg transition-transform active:scale-[0.99]"
        >
          <div>
            <div className="text-base font-black" style={{ fontSize: 22, fontWeight: 800 }}>
              🥬 生鲜日用超市
            </div>
            <div className="text-xs text-emerald-50/90 font-medium">
              9 大分类 · 蔬菜水果肉禽蛋 · 粮油乳品冷冻 · 真实价格
            </div>
          </div>
          <span className="rounded-full bg-white/25 px-4 py-2 text-sm font-extrabold">
            逛逛 →
          </span>
        </Link>
      </div>

      {/* 极速商城入口 (Phase 9: 扫码即买零层级交互) */}
      <div className="mb-6">
        <Link
          href="/shop/feed"
          className="flex items-center justify-between rounded-2xl bg-gradient-to-r from-red-600 via-orange-500 to-amber-500 px-5 py-4 text-white shadow-lg transition-transform active:scale-[0.99]"
        >
          <div>
            <div className="text-base font-black" style={{ fontSize: 22, fontWeight: 800 }}>
              ⚡ 极速商城
            </div>
            <div className="text-xs text-orange-100 font-medium">
              附近微仓直供 · 暴降大卡 · 一键抢购
            </div>
          </div>
          <span className="rounded-full bg-white/25 px-4 py-2 text-sm font-extrabold">
            抢 →
          </span>
        </Link>
      </div>

      {/* Hot picks */}
      {hotPicks.length > 0 && (
        <section className="mb-10">
          <h2 className="text-lg font-bold text-gray-900 mb-3">🔥 人气好物</h2>
          <div className="flex gap-4 overflow-x-auto pb-2 -mx-1 px-1">
            {hotPicks.map((p) => (
              <Link
                key={p.id}
                href={`/shop/products/${p.id}`}
                className="group shrink-0 w-44 bg-white rounded-xl border border-gray-200 overflow-hidden hover:shadow-md transition"
              >
                <div className={`aspect-square bg-gradient-to-br ${productGradient(p.id)} flex items-center justify-center text-4xl`}>
                  {productIcon(p.title)}
                </div>
                <div className="p-3">
                  <h3 className="text-sm font-medium text-gray-900 truncate group-hover:text-emerald-700 transition">
                    {p.title}
                  </h3>
                  <div className="text-sm font-bold text-emerald-700 mt-1">
                    {formatMoney(p.min_price_cents, getSelectedRegion()?.currency)}
                  </div>
                </div>
              </Link>
            ))}
          </div>
        </section>
      )}

      {/* Filters */}
      <div className="flex flex-wrap gap-3 mb-6 items-end bg-white border border-gray-200 rounded-xl p-4">
        <div>
          <label className="block text-xs text-gray-500 mb-1">搜索</label>
          <input
            type="text"
            placeholder="搜索商品..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && load()}
            className="px-3 py-2 border border-gray-300 rounded-lg text-sm w-48"
          />
        </div>
        <div>
          <label className="block text-xs text-gray-500 mb-1">最低价</label>
          <input
            type="number"
            placeholder="¥"
            value={minPrice}
            onChange={(e) => setMinPrice(e.target.value)}
            className="px-3 py-2 border border-gray-300 rounded-lg text-sm w-24"
          />
        </div>
        <div>
          <label className="block text-xs text-gray-500 mb-1">最高价</label>
          <input
            type="number"
            placeholder="¥"
            value={maxPrice}
            onChange={(e) => setMaxPrice(e.target.value)}
            className="px-3 py-2 border border-gray-300 rounded-lg text-sm w-24"
          />
        </div>
        <button onClick={load} className="px-4 py-2 bg-emerald-700 text-white rounded-lg text-sm hover:bg-emerald-800 transition">
          筛选
        </button>
        <div className="ml-auto">
          <label className="block text-xs text-gray-500 mb-1">排序</label>
          <select
            value={sort}
            onChange={(e) => setSort(e.target.value as SortMode)}
            className="px-3 py-2 border border-gray-300 rounded-lg text-sm"
          >
            <option value="default">默认排序</option>
            <option value="price_asc">价格从低到高</option>
            <option value="price_desc">价格从高到低</option>
          </select>
        </div>
      </div>

      {/* Product Grid */}
      {loading ? (
        <div className="text-gray-500 py-12 text-center">加载中...</div>
      ) : sortedProducts.length === 0 ? (
        <div className="text-gray-400 py-12 text-center">暂无商品</div>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-6">
          {sortedProducts.map((p) => {
            const lowStock = p.total_stock > 0 && p.total_stock <= 10;
            return (
              <Link
                key={p.id}
                href={`/shop/products/${p.id}`}
                className="group bg-white rounded-xl border border-gray-200 overflow-hidden hover:shadow-lg hover:-translate-y-0.5 transition"
              >
                <div className={`relative aspect-[4/3] bg-gradient-to-br ${productGradient(p.id)} flex items-center justify-center`}>
                  <span className="text-4xl">{productIcon(p.title)}</span>
                  {lowStock && (
                    <span className="absolute top-2 right-2 text-[11px] font-semibold bg-amber-500 text-white rounded-full px-2 py-0.5">
                      仅剩 {p.total_stock} 件
                    </span>
                  )}
                </div>
                <div className="p-4">
                  <h3 className="font-semibold text-gray-900 group-hover:text-emerald-700 transition">
                    {p.title}
                  </h3>
                  {p.description && (
                    <p className="text-sm text-gray-500 mt-1 line-clamp-2">{p.description}</p>
                  )}
                  <div className="flex items-center justify-between mt-3">
                    <span className="text-lg font-bold text-emerald-700">{formatMoney(p.min_price_cents, getSelectedRegion()?.currency)}</span>
                    <span className="text-xs text-gray-400">{p.total_stock} 件可售</span>
                  </div>
                  {p.variants && p.variants.length > 1 && (
                    <div className="text-xs text-gray-400 mt-1">{p.variants.length} 种规格可选</div>
                  )}
                </div>
              </Link>
            );
          })}
        </div>
      )}
    </div>
  );
}
