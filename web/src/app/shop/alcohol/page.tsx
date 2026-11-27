'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { api } from '@/lib/api-client';
import { errMessage } from '@/lib/errors';
import { formatMoney } from '@/lib/format';
import { productImage } from '@/lib/product-image';
import { getSelectedRegion } from '@/lib/region-store';
import { getCustomerAuth, initCustomerAuth } from '@/lib/customer-auth-store';
import { useToast } from '@/components/Toast';
import type { StorefrontProduct } from '@/types/api';

type AlcoholStatus = {
  enabled: boolean;
  min_age: number;
  verified: boolean;
  notice: string;
};

export default function AlcoholPage() {
  const showToast = useToast();
  const [status, setStatus] = useState<AlcoholStatus | null>(null);
  const [birthDate, setBirthDate] = useState('');
  const [products, setProducts] = useState<StorefrontProduct[]>([]);
  const [loading, setLoading] = useState(true);
  const [addingId, setAddingId] = useState<string | null>(null);

  async function loadProducts() {
    const all = await api.storeProducts({ limit: 500 });
    setProducts(all.filter((p) => p.category_slug === 'drinks'));
  }

  useEffect(() => {
    api.alcoholStatus()
      .then(async (next) => {
        setStatus(next);
        if (next.enabled && next.verified) await loadProducts();
      })
      .catch((e) => showToast(errMessage(e), 'error'))
      .finally(() => setLoading(false));
  }, [showToast]);

  async function verify(e: React.FormEvent) {
    e.preventDefault();
    try {
      await api.verifyAlcoholAge(birthDate);
      const next = await api.alcoholStatus();
      setStatus(next);
      await loadProducts();
      showToast('年龄确认完成');
    } catch (e) {
      showToast(errMessage(e), 'error');
    }
  }

  async function addToCart(product: StorefrontProduct) {
    if (addingId) return;
    const variant = product.variants.find((v) => (v.total_stock ?? 0) > 0) || product.variants[0];
    if (!variant) {
      showToast('该商品暂无可选规格', 'error');
      return;
    }
    const batch = variant.batches.find((b) => b.available_qty > 0) || null;
    setAddingId(product.id);
    try {
      let cartId = sessionStorage.getItem('hemall_cart_id');
      if (!cartId) {
        const region = getSelectedRegion();
        const cart = region
          ? await api.createCart(region.code, region.currency)
          : await api.createCart();
        cartId = cart.cart_id;
        sessionStorage.setItem('hemall_cart_id', cartId);
        await initCustomerAuth();
        const customer = getCustomerAuth();
        if (customer) await api.setCartCustomer(cartId, customer.customerId).catch(() => {});
      }
      if (batch) await api.addLineItem(cartId, batch.id, 1);
      else await api.addVariantLineItem(cartId, variant.id, 1);
      showToast(`已加入购物车: ${product.title}`);
    } catch (e) {
      showToast(errMessage(e), 'error');
    } finally {
      setAddingId(null);
    }
  }

  if (loading) return <div className="py-16 text-center text-gray-500">加载中...</div>;

  if (!status?.enabled) {
    return (
      <div className="mx-auto max-w-xl rounded-3xl border border-amber-200 bg-amber-50 p-8 text-center">
        <div className="text-5xl">🔞</div>
        <h1 className="mt-4 text-2xl font-black text-amber-950">酒类商品已下架</h1>
        <p className="mt-3 text-sm leading-6 text-amber-800">
          为保护未成年人，酒类销售默认关闭。商家完成资质与销售配置后方可开放。
        </p>
        <p className="mt-2 text-sm font-bold text-red-700">未成年人禁止购买和饮酒。请理性饮酒。</p>
        <Link href="/shop" className="mt-6 inline-block rounded-full bg-emerald-700 px-6 py-2.5 text-sm font-bold text-white">
          返回商城
        </Link>
      </div>
    );
  }

  if (!status.verified) {
    return (
      <div className="mx-auto max-w-xl rounded-3xl border border-red-200 bg-white p-8 shadow-sm">
        <div className="text-5xl">🔞</div>
        <h1 className="mt-4 text-2xl font-black text-gray-950">酒类专区 · 年龄确认</h1>
        <p className="mt-3 text-sm leading-6 text-red-700">
          未满 {status.min_age} 周岁禁止购买酒类。请理性饮酒，切勿酒后驾车。
        </p>
        <form onSubmit={verify} className="mt-6 space-y-4">
          <label className="block text-sm font-bold text-gray-700">
            出生日期
            <input
              type="date"
              required
              value={birthDate}
              onChange={(e) => setBirthDate(e.target.value)}
              className="mt-2 w-full rounded-xl border border-gray-300 px-4 py-3 text-base"
            />
          </label>
          <button className="w-full rounded-xl bg-red-700 px-5 py-3 font-bold text-white hover:bg-red-800">
            我确认已满 {status.min_age} 周岁
          </button>
        </form>
        <p className="mt-4 text-xs leading-5 text-gray-500">
          年龄信息仅用于本次确认，不保存出生日期；确认结果以签名凭证保留一段时间。
        </p>
      </div>
    );
  }

  return (
    <div>
      <section className="mb-6 rounded-3xl bg-gradient-to-r from-red-800 to-amber-700 p-6 text-white">
        <p className="text-xs font-bold tracking-[0.2em] text-amber-200">18+ · RESPONSIBLE DRINKING</p>
        <h1 className="mt-2 text-3xl font-black">酒类专区</h1>
        <p className="mt-2 text-sm text-red-50">{status.notice}</p>
      </section>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        {products.map((product) => {
          const price = product.min_price_cents;
          return (
            <article key={product.id} className="overflow-hidden rounded-2xl border border-gray-200 bg-white shadow-sm">
              <Link href={`/shop/products/${product.id}`} className="block aspect-square bg-gray-100">
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src={productImage(product) || ''} alt={product.title} className="h-full w-full object-cover" />
              </Link>
              <div className="p-3">
                <h2 className="line-clamp-2 min-h-10 text-sm font-bold text-gray-900">{product.title}</h2>
                <div className="mt-2 flex items-center justify-between gap-2">
                  <span className="font-black text-red-700">
                    {formatMoney(price, getSelectedRegion()?.currency)}
                  </span>
                  <button
                    onClick={() => addToCart(product)}
                    disabled={addingId === product.id}
                    className="rounded-lg bg-red-700 px-3 py-1.5 text-xs font-bold text-white disabled:opacity-60"
                  >
                    {addingId === product.id ? '加入中...' : '+ 加购'}
                  </button>
                </div>
              </div>
            </article>
          );
        })}
      </div>
      {products.length === 0 && <p className="py-12 text-center text-gray-400">当前暂无可售酒类商品</p>}
    </div>
  );
}
