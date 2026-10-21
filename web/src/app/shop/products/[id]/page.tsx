'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { useParams, useRouter } from 'next/navigation';
import { api } from '@/lib/api-client';
import { formatMoney } from '@/lib/format';
import { getSelectedRegion } from '@/lib/region-store';
import { getCustomerAuth, initCustomerAuth } from '@/lib/customer-auth-store';
import { productIcon, productGradient } from '@/lib/product-visual';
import { productImage } from '@/lib/product-image';
import { useToast } from '@/components/Toast';
import type { StorefrontProduct, ProductVariant, InventoryBatch } from '@/types/api';
import { errMessage } from '@/lib/errors';

export default function ProductDetailPage() {
  const params = useParams();
  const router = useRouter();
  const showToast = useToast();
  const productId = params.id as string;

  const [product, setProduct] = useState<StorefrontProduct | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [selectedVariant, setSelectedVariant] = useState<ProductVariant | null>(null);
  const [selectedBatch, setSelectedBatch] = useState<InventoryBatch | null>(null);
  const [quantity, setQuantity] = useState(1);
  const [adding, setAdding] = useState(false);
  const [similar, setSimilar] = useState<StorefrontProduct[]>([]);
  const [imgBroken, setImgBroken] = useState(false);

  // 选批次：先挑有可用库存的，没有就用首个批次（0 库存也可加购=预订），无批次返回 null。
  function pickBatch(v: ProductVariant): InventoryBatch | null {
    const batches = v.batches || [];
    return batches.find((b) => b.available_qty > 0) || batches[0] || null;
  }

  useEffect(() => {
    api.storeProduct(productId)
      .then((p) => {
        setProduct(p);
        setImgBroken(false);
        if (p.variants?.length > 0) {
          setSelectedVariant(p.variants[0]);
          setSelectedBatch(pickBatch(p.variants[0]));
        }
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));

    // 切换商品时先清空旧的相似推荐（微任务延后，effect 内不允许同步 setState）
    Promise.resolve().then(() => {
      setSimilar([]);
    });
    api.storeRecommendSimilar(productId, 6)
      .then(async (res) => {
        const items = await Promise.all(
          res.data.products.map((r) => api.storeProduct(r.product_id).catch(() => null)),
        );
        setSimilar(items.filter((p): p is StorefrontProduct => Boolean(p)));
      })
      .catch(() => {});
  }, [productId]);

  function handleVariantChange(v: ProductVariant) {
    setSelectedVariant(v);
    setSelectedBatch(pickBatch(v));
    setQuantity(1);
  }

  async function handleAddToCart() {
    if (!selectedVariant) {
      showToast('请选择规格', 'error');
      return;
    }
    setAdding(true);
    try {
      // Get or create cart
      let cartId = sessionStorage.getItem('hemall_cart_id');
      if (!cartId) {
        const region = getSelectedRegion();
        const cart = region
          ? await api.createCart(region.code, region.currency)
          : await api.createCart();
        cartId = cart.cart_id;
        sessionStorage.setItem('hemall_cart_id', cartId);

        await initCustomerAuth();
        const customerAuth = getCustomerAuth();
        if (customerAuth) {
          await api.setCartCustomer(cartId, customerAuth.customerId).catch(() => {});
        }
      }
      if (selectedBatch) {
        await api.addLineItem(cartId, selectedBatch.id, quantity);
      } else {
        // 商品无任何批次：按变体加购，下单时整行计入预订待配货
        await api.addVariantLineItem(cartId, selectedVariant.id, quantity);
      }
      router.push('/shop/cart');
    } catch (e) {
      showToast(errMessage(e), 'error');
    } finally {
      setAdding(false);
    }
  }

  if (loading) return <div className="text-center py-12 text-gray-500">加载中...</div>;
  if (error) return <div className="text-center py-12 text-red-600">{error}</div>;
  if (!product) return <div className="text-center py-12 text-gray-400">商品不存在</div>;

  const heroImg = imgBroken ? null : productImage(product);
  // 预订态：选中变体没有任何可用库存（无批次或批次可用量为 0）——仍可加购，下单后待配货。
  const isPreorder = !!selectedVariant && (!selectedBatch || selectedBatch.available_qty <= 0);
  const maxQty = isPreorder ? 999 : (selectedBatch?.available_qty || 1);

  return (
    <div>
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-8">
        {/* Product Image — 白底方图, 缺图/加载失败回落 emoji 占位 */}
        <div className="aspect-square overflow-hidden rounded-2xl border border-gray-100 bg-white">
          {heroImg ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={heroImg}
              alt={product.title}
              loading="lazy"
              className="h-full w-full object-contain p-4"
              onError={() => setImgBroken(true)}
            />
          ) : (
            <div className={`flex h-full w-full items-center justify-center bg-gradient-to-br ${productGradient(product.id)}`}>
              <span className="text-8xl">{productIcon(product.title)}</span>
            </div>
          )}
        </div>

        {/* Product Info */}
        <div className="space-y-6">
          <div>
            <h1 className="text-2xl font-bold text-gray-900">{product.title}</h1>
            {product.description && (
              <p className="mt-2 text-gray-600">{product.description}</p>
            )}
          </div>

          <div className="text-3xl font-bold text-emerald-700">
            {selectedBatch
              ? formatMoney(selectedBatch.retail_price_cents, selectedBatch.currency)
              : formatMoney(
                  product.min_price_cents ?? selectedVariant?.min_price_cents ?? selectedVariant?.reference_price_cents,
                  getSelectedRegion()?.currency,
                )}
          </div>

          {/* Variant Selection */}
          {product.variants && product.variants.length > 0 && (
            <div>
              <h3 className="text-sm font-medium text-gray-700 mb-2">选择规格</h3>
              <div className="flex flex-wrap gap-2">
                {product.variants.map((v) => {
                  const label = Object.values(v.option_values || {}).join(' / ') || v.sku_code;
                  const isSelected = selectedVariant?.id === v.id;
                  const hasStock = (v.batches || []).some((b) => b.available_qty > 0);
                  return (
                    <button
                      key={v.id}
                      onClick={() => handleVariantChange(v)}
                      className={`px-4 py-2 rounded-lg text-sm border transition ${
                        isSelected
                          ? 'border-emerald-600 bg-emerald-50 text-emerald-700'
                          : hasStock
                          ? 'border-gray-300 text-gray-700 hover:border-gray-400'
                          : 'border-amber-300 text-amber-700 hover:border-amber-400'
                      }`}
                    >
                      {label}
                      {!hasStock && <span className="ml-1 text-xs">（缺货可预订）</span>}
                    </button>
                  );
                })}
              </div>
            </div>
          )}

          {/* Stock Info */}
          {selectedBatch && selectedBatch.available_qty > 0 && (
            <div className="text-sm text-gray-500">
              <span>可售库存: </span>
              <span className={`font-medium ${selectedBatch.available_qty > 5 ? 'text-green-600' : 'text-amber-600'}`}>
                {selectedBatch.available_qty} 件
              </span>
            </div>
          )}
          {isPreorder && (
            <div className="text-sm text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">
              缺货可预订：暂时无货，可先下单预订，到货后按订单顺序配货。
            </div>
          )}

          {/* Quantity */}
          <div className="flex items-center gap-3">
            <label className="text-sm font-medium text-gray-700">数量</label>
            <div className="flex items-center border border-gray-300 rounded-lg">
              <button
                onClick={() => setQuantity(Math.max(1, quantity - 1))}
                className="px-3 py-1 text-gray-600 hover:text-gray-900"
              >
                −
              </button>
              <input
                type="number"
                min={1}
                max={maxQty}
                value={quantity}
                onChange={(e) => setQuantity(Math.max(1, Math.min(Number(e.target.value) || 1, maxQty)))}
                className="w-16 text-center border-x border-gray-300 py-1 text-sm"
              />
              <button
                onClick={() => setQuantity(Math.min(quantity + 1, maxQty))}
                className="px-3 py-1 text-gray-600 hover:text-gray-900"
              >
                +
              </button>
            </div>
          </div>

          {/* Add to Cart */}
          <button
            onClick={handleAddToCart}
            disabled={adding || !selectedVariant}
            className={`w-full py-3 text-white rounded-xl disabled:opacity-50 disabled:cursor-not-allowed transition font-medium text-lg ${
              isPreorder ? 'bg-amber-500 hover:bg-amber-600' : 'bg-emerald-700 hover:bg-emerald-800'
            }`}
          >
            {adding ? '加入中...' : isPreorder ? '预订' : '加入购物车'}
          </button>
        </div>
      </div>

      {/* Similar products */}
      {similar.length > 0 && (
        <section className="mt-14">
          <h2 className="text-lg font-bold text-gray-900 mb-3">相似商品</h2>
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-4">
            {similar.map((p) => {
              const simImg = productImage(p);
              return (
                <Link
                  key={p.id}
                  href={`/shop/products/${p.id}`}
                  className="group bg-white rounded-xl border border-gray-200 overflow-hidden hover:shadow-md transition"
                >
                  {/* emoji 占位垫底, 图片叠在上层; 加载失败隐藏图片即回落占位 */}
                  <div className={`relative aspect-square bg-gradient-to-br ${productGradient(p.id)} flex items-center justify-center text-3xl`}>
                    <span>{productIcon(p.title)}</span>
                    {simImg && (
                      // eslint-disable-next-line @next/next/no-img-element
                      <img
                        src={simImg}
                        alt={p.title}
                        loading="lazy"
                        className="absolute inset-0 h-full w-full bg-white object-contain p-2"
                        onError={(e) => {
                          (e.currentTarget as HTMLImageElement).style.display = 'none';
                        }}
                      />
                    )}
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
              );
            })}
          </div>
        </section>
      )}
    </div>
  );
}
