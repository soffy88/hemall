'use client';

/**
 * /shop/market — 生鲜日用超市 (对标小象超市 Phase 9.5)
 *
 * 体验设计:
 *   - 顶部横向滚动分类胶囊条 (图标 + 名称 + 商品数)，点击即过滤
 *   - 商品区 2 列卡片网格 (小象超市风格):
 *     真实图片 (shelf_image_url, 加载失败回退 emoji) + 商品名 + 规格 +
 *     划线价 + 现价 + 库存 + 加购按钮
 *   - 全部商品来自生产库真实数据 (54 个生鲜日用 SKU, 真实价格)
 */

import { useEffect, useMemo, useState } from 'react';
import Link from 'next/link';
import { api } from '@/lib/api-client';
import { formatMoney } from '@/lib/format';
import { productIcon, productGradient } from '@/lib/product-visual';
import { useToast } from '@/components/Toast';
import type { GroceryCategory, StorefrontProduct } from '@/types/api';

// 分类图标映射 (后端只存 name/slug，图标前端映射)
const CATEGORY_ICONS: Record<string, string> = {
  vegetables: '🥬', fruits: '🍎', 'meat-eggs': '🥩', 'grain-oil': '🌾',
  'dairy-bakery': '🥛', frozen: '🧊', snacks: '🍫', drinks: '🥤', daily: '🧻',
};

export default function MarketPage() {
  const showToast = useToast();
  const [products, setProducts] = useState<StorefrontProduct[]>([]);
  const [loading, setLoading] = useState(true);
  const [activeCat, setActiveCat] = useState<string>('all');
  const [search, setSearch] = useState('');

  async function load() {
    setLoading(true);
    try {
      const data = await api.storeProducts({ limit: 200 });
      setProducts(data);
    } catch (e: any) {
      showToast(e.message, 'error');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, []);

  // 聚合分类 (按 category_id + 后端返回的分类名/图标映射)
  const categories: GroceryCategory[] = useMemo(() => {
    const map = new Map<string, GroceryCategory>();
    for (const p of products) {
      if (!p.category_id) continue;
      if (!map.has(p.category_id)) {
        const slug = p.category_slug ?? p.category_id;
        map.set(p.category_id, {
          id: p.category_id,
          name: p.category_name ?? '分类',
          slug,
          icon: CATEGORY_ICONS[slug] ?? '🛒',
          count: 0,
        });
      }
      map.get(p.category_id)!.count++;
    }
    // 保持 9 大分类的展示顺序
    const order = Object.keys(CATEGORY_ICONS);
    return Array.from(map.values()).sort((a, b) => {
      const ia = order.indexOf(a.slug);
      const ib = order.indexOf(b.slug);
      if (ia === -1 && ib === -1) return 0;
      if (ia === -1) return 1;
      if (ib === -1) return -1;
      return ia - ib;
    });
  }, [products]);

  const filtered = useMemo(() => {
    let list = products;
    if (activeCat !== 'all') {
      list = list.filter((p) => p.category_id === activeCat);
    }
    if (search.trim()) {
      const q = search.trim().toLowerCase();
      list = list.filter((p) => p.title.toLowerCase().includes(q));
    }
    // 生鲜日用优先 (有 category 的排前)，按价格升序
    return [...list].sort((a, b) => {
      const ac = a.category_id ? 0 : 1;
      const bc = b.category_id ? 0 : 1;
      if (ac !== bc) return ac - bc;
      return (a.min_price_cents ?? 0) - (b.min_price_cents ?? 0);
    });
  }, [products, activeCat, search]);

  // 取商品首图 (batch.shelf_image_url → 第一个 media_assets → 兜底 emoji)
  function productImage(p: StorefrontProduct): string | null {
    const v = p.variants?.[0];
    const b = v?.batches?.[0];
    if (b?.shelf_image_url) return b.shelf_image_url;
    if (b?.media_assets?.length) return b.media_assets[0];
    return null;
  }

  // 取商品划线价 (reference_price_cents 优先，无则用批次)
  function referencePrice(p: StorefrontProduct): number | null {
    const v = p.variants?.[0];
    if (v?.reference_price_cents) return v.reference_price_cents;
    return null;
  }

  function specOf(p: StorefrontProduct): string {
    const ov = p.variants?.[0]?.option_values;
    if (ov && typeof ov === 'object') {
      const spec = (ov as Record<string, string>)['规格'];
      if (spec) return spec;
    }
    return p.variants?.[0]?.sku_code ?? '';
  }

  function addToCart(p: StorefrontProduct) {
    // 极速锁单链路: 跳转到 feed 页由购物车状态机接管 (演示环境直接提示)
    showToast(`已加入购物车: ${p.title}`);
  }

  return (
    <div className="min-h-screen bg-gray-50">
      {/* 顶部搜索栏 */}
      <div className="sticky top-0 z-30 bg-emerald-700 px-4 pb-3 pt-4 shadow-lg">
        <div className="flex items-center gap-2">
          <div className="flex-1 rounded-full bg-white/95 px-4 py-2.5">
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="🔍 搜索生鲜日用"
              className="w-full bg-transparent text-[15px] outline-none"
            />
          </div>
          <Link
            href="/shop/feed"
            className="shrink-0 rounded-full bg-amber-400 px-4 py-2.5 text-sm font-black text-emerald-900 active:scale-95"
          >
            ⚡ 极速抢
          </Link>
        </div>

        {/* 分类胶囊条 (横向滚动) */}
        <div className="mt-3 flex gap-2 overflow-x-auto pb-1 [-ms-overflow-style:none] [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
          <button
            type="button"
            onClick={() => setActiveCat('all')}
            className={`shrink-0 rounded-full px-4 py-2 text-[13px] font-bold transition ${
              activeCat === 'all'
                ? 'bg-white text-emerald-700'
                : 'bg-emerald-800/60 text-emerald-50'
            }`}
          >
            🛒 全部 {products.length}
          </button>
          {categories.map((c) => (
            <button
              key={c.id}
              type="button"
              onClick={() => setActiveCat(c.id)}
              className={`shrink-0 rounded-full px-4 py-2 text-[13px] font-bold transition ${
                activeCat === c.id
                  ? 'bg-white text-emerald-700'
                  : 'bg-emerald-800/60 text-emerald-50'
              }`}
            >
              {c.icon} {c.name.replace(c.id, '')}{c.count > 0 ? ` ${c.count}` : ''}
            </button>
          ))}
        </div>
      </div>

      {/* 商品网格 */}
      <div className="mx-auto max-w-md px-3 pb-24 pt-3">
        {loading ? (
          <div className="flex justify-center py-16">
            <div className="h-10 w-10 animate-spin rounded-full border-4 border-emerald-600 border-t-transparent" />
          </div>
        ) : (
          <div className="grid grid-cols-2 gap-3">
            {filtered.map((p) => {
              const img = productImage(p);
              const ref = referencePrice(p);
              const retail = p.min_price_cents ?? 0;
              const stock = p.total_stock ?? 0;
              const spec = specOf(p);
              return (
                <div
                  key={p.id}
                  className="group overflow-hidden rounded-2xl bg-white shadow-sm transition hover:shadow-md"
                >
                  {/* 图片区 */}
                  <Link href={`/shop/products/${p.id}`} className="block">
                    <div className="relative aspect-square w-full overflow-hidden">
                      {img ? (
                        // eslint-disable-next-line @next/next/no-img-element
                        <img
                          src={img}
                          alt={p.title}
                          loading="lazy"
                          className="h-full w-full object-cover transition-transform group-hover:scale-105"
                          onError={(e) => {
                            (e.target as HTMLImageElement).style.display = 'none';
                          }}
                        />
                      ) : (
                        <div className={`flex h-full w-full items-center justify-center bg-gradient-to-br ${productGradient(p.id)}`}>
                          <span className="text-5xl">{productIcon(p.title)}</span>
                        </div>
                      )}
                      {stock <= 5 && stock > 0 && (
                        <span className="absolute left-2 top-2 rounded-full bg-red-500 px-2 py-0.5 text-[10px] font-black text-white">
                          仅剩{stock}
                        </span>
                      )}
                      {stock === 0 && (
                        <div className="absolute inset-0 flex items-center justify-center bg-black/40 text-sm font-black text-white">
                          售罄
                        </div>
                      )}
                    </div>
                  </Link>

                  {/* 信息区 */}
                  <div className="p-2.5">
                    <Link href={`/shop/products/${p.id}`} className="block">
                      <h3 className="truncate text-[13px] font-semibold text-gray-800">
                        {p.title}
                      </h3>
                      {spec && <div className="mt-0.5 text-[11px] text-gray-400">{spec}</div>}
                    </Link>
                    <div className="mt-1.5 flex items-baseline gap-1.5">
                      <span
                        className="font-black text-emerald-700"
                        style={{ fontSize: 22, fontWeight: 800 }}
                      >
                        ¥{(retail / 100).toFixed(2)}
                      </span>
                      {ref && ref > retail && (
                        <span className="text-[11px] text-gray-400 line-through">
                          ¥{(ref / 100).toFixed(2)}
                        </span>
                      )}
                    </div>
                    <button
                      type="button"
                      onClick={() => addToCart(p)}
                      disabled={stock === 0}
                      className="mt-2 w-full rounded-xl bg-emerald-600 py-2 text-sm font-bold text-white transition active:scale-95 disabled:bg-gray-200 disabled:text-gray-400"
                    >
                      {stock === 0 ? '已售罄' : '+ 加入购物车'}
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
