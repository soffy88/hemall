'use client';

import { useEffect, useState } from 'react';
import { useParams, useRouter } from 'next/navigation';
import { api } from '@/lib/api-client';
import { formatMoney } from '@/lib/format';
import { getSelectedRegion } from '@/lib/region-store';
import { getCustomerAuth } from '@/lib/customer-auth-store';
import type { StorefrontProduct, ProductVariant, InventoryBatch } from '@/types/api';

export default function ProductDetailPage() {
  const params = useParams();
  const router = useRouter();
  const productId = params.id as string;

  const [product, setProduct] = useState<StorefrontProduct | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [selectedVariant, setSelectedVariant] = useState<ProductVariant | null>(null);
  const [selectedBatch, setSelectedBatch] = useState<InventoryBatch | null>(null);
  const [quantity, setQuantity] = useState(1);
  const [adding, setAdding] = useState(false);

  useEffect(() => {
    api.storeProduct(productId)
      .then((p) => {
        setProduct(p);
        if (p.variants?.length > 0) {
          setSelectedVariant(p.variants[0]);
          if (p.variants[0].batches?.length > 0) {
            setSelectedBatch(p.variants[0].batches[0]);
          }
        }
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [productId]);

  function handleVariantChange(v: ProductVariant) {
    setSelectedVariant(v);
    if (v.batches?.length > 0) {
      setSelectedBatch(v.batches[0]);
    } else {
      setSelectedBatch(null);
    }
    setQuantity(1);
  }

  async function handleAddToCart() {
    if (!selectedBatch) {
      alert('请选择规格');
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

        const customerAuth = getCustomerAuth();
        if (customerAuth) {
          await api.setCartCustomer(cartId, customerAuth.customerId).catch(() => {});
        }
      }
      await api.addLineItem(cartId, selectedBatch.id, quantity);
      router.push('/shop/cart');
    } catch (e: any) {
      alert(e.message);
    } finally {
      setAdding(false);
    }
  }

  if (loading) return <div className="text-center py-12 text-gray-500">加载中...</div>;
  if (error) return <div className="text-center py-12 text-red-600">{error}</div>;
  if (!product) return <div className="text-center py-12 text-gray-400">商品不存在</div>;

  // Collect all unique option keys
  const optionKeys = new Set<string>();
  product.variants?.forEach((v) => {
    Object.keys(v.option_values || {}).forEach((k) => optionKeys.add(k));
  });

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-8">
      {/* Product Image */}
      <div className="aspect-square bg-gradient-to-br from-emerald-50 to-teal-50 rounded-2xl flex items-center justify-center">
        <span className="text-8xl">📦</span>
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
            : formatMoney(product.min_price_cents, getSelectedRegion()?.currency)}
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
                    disabled={!hasStock}
                    className={`px-4 py-2 rounded-lg text-sm border transition ${
                      isSelected
                        ? 'border-emerald-600 bg-emerald-50 text-emerald-700'
                        : hasStock
                        ? 'border-gray-300 text-gray-700 hover:border-gray-400'
                        : 'border-gray-200 text-gray-300 cursor-not-allowed'
                    }`}
                  >
                    {label}
                  </button>
                );
              })}
            </div>
          </div>
        )}

        {/* Stock Info */}
        {selectedBatch && (
          <div className="text-sm text-gray-500">
            <span>可售库存: </span>
            <span className={`font-medium ${selectedBatch.available_qty > 5 ? 'text-green-600' : 'text-amber-600'}`}>
              {selectedBatch.available_qty} 件
            </span>
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
              max={selectedBatch?.available_qty || 1}
              value={quantity}
              onChange={(e) => setQuantity(Math.max(1, Math.min(Number(e.target.value), selectedBatch?.available_qty || 1)))}
              className="w-16 text-center border-x border-gray-300 py-1 text-sm"
            />
            <button
              onClick={() => setQuantity(Math.min(quantity + 1, selectedBatch?.available_qty || 1))}
              className="px-3 py-1 text-gray-600 hover:text-gray-900"
            >
              +
            </button>
          </div>
        </div>

        {/* Add to Cart */}
        <button
          onClick={handleAddToCart}
          disabled={adding || !selectedBatch || (selectedBatch.available_qty <= 0)}
          className="w-full py-3 bg-emerald-600 text-white rounded-xl hover:bg-emerald-700 disabled:opacity-50 disabled:cursor-not-allowed transition font-medium text-lg"
        >
          {adding ? '加入中...' : '加入购物车'}
        </button>
      </div>
    </div>
  );
}
