'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { api } from '@/lib/api-client';
import { formatMoney } from '@/lib/format';
import { getSelectedRegion } from '@/lib/region-store';
import type { StorefrontProduct } from '@/types/api';

export default function ShopPage() {
  const [products, setProducts] = useState<StorefrontProduct[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');
  const [minPrice, setMinPrice] = useState('');
  const [maxPrice, setMaxPrice] = useState('');

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

  return (
    <div>
      {/* Filters */}
      <div className="flex flex-wrap gap-3 mb-6 items-end">
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
        <button onClick={load} className="px-4 py-2 bg-emerald-600 text-white rounded-lg text-sm hover:bg-emerald-700">
          筛选
        </button>
      </div>

      {/* Product Grid */}
      {loading ? (
        <div className="text-gray-500 py-12 text-center">加载中...</div>
      ) : products.length === 0 ? (
        <div className="text-gray-400 py-12 text-center">暂无商品</div>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-6">
          {products.map((p) => (
            <Link
              key={p.id}
              href={`/shop/products/${p.id}`}
              className="group bg-white rounded-xl border border-gray-200 overflow-hidden hover:shadow-md transition"
            >
              <div className="aspect-[4/3] bg-gradient-to-br from-emerald-50 to-teal-50 flex items-center justify-center">
                <span className="text-4xl">📦</span>
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
          ))}
        </div>
      )}
    </div>
  );
}
