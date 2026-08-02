'use client';

import { useEffect, useState } from 'react';
import { api } from '@/lib/api-client';
import { formatMoney } from '@/lib/format';
import type { Product, ProductVariant } from '@/types/api';

/** 商品/变体的“最低价”本身没有单独的币种字段，取第一个批次的币种做展示。 */
function fmt(cents: number | null, currency?: string): string {
  return formatMoney(cents, currency);
}

export default function ProductsPage() {
  const [products, setProducts] = useState<Product[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [showForm, setShowForm] = useState(false);
  const [editProduct, setEditProduct] = useState<Product | null>(null);
  const [search, setSearch] = useState('');

  // Create product form state
  const [title, setTitle] = useState('');
  const [slug, setSlug] = useState('');
  const [description, setDescription] = useState('');
  const [status, setStatus] = useState('draft');

  // Create variant form state
  const [variantProductId, setVariantProductId] = useState('');
  const [skuCode, setSkuCode] = useState('');
  const [showVariantForm, setShowVariantForm] = useState(false);

  // Create batch form state
  const [batchVariantId, setBatchVariantId] = useState('');
  const [batchNo, setBatchNo] = useState('');
  const [stockQty, setStockQty] = useState(10);
  const [retailPrice, setRetailPrice] = useState(9900);
  const [costPrice, setCostPrice] = useState(5000);
  const [locationId, setLocationId] = useState('');
  const [videoUrl, setVideoUrl] = useState('');
  const [showBatchForm, setShowBatchForm] = useState(false);

  async function load() {
    setLoading(true);
    try {
      const data = await api.adminListProducts({ search: search || undefined });
      setProducts(data);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, []);

  async function handleCreateProduct(e: React.FormEvent) {
    e.preventDefault();
    try {
      const res = await api.createProduct({ title, slug, description, status });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '创建失败');
      setTitle(''); setSlug(''); setDescription(''); setStatus('draft');
      setShowForm(false);
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleDelete(id: string) {
    if (!confirm('确认删除此商品？')) return;
    try {
      await api.deleteProduct({ product_id: id });
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handlePublish(product: Product) {
    try {
      const newStatus = product.status === 'published' ? 'draft' : 'published';
      await api.updateProduct({ product_id: product.id, status: newStatus });
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleCreateVariant(e: React.FormEvent) {
    e.preventDefault();
    try {
      const res = await api.createProductVariant({ product_id: variantProductId, sku_code: skuCode });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '创建变体失败');
      setSkuCode('');
      setShowVariantForm(false);
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleToggleVariant(v: ProductVariant) {
    try {
      const newStatus = v.status === 'active' ? 'inactive' : 'active';
      const res = await api.updateProductVariant({ variant_id: v.id, status: newStatus });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '更新变体失败');
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleDeleteVariant(id: string) {
    if (!confirm('确认删除此变体？')) return;
    try {
      const res = await api.deleteProductVariant({ variant_id: id });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '删除变体失败');
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleAdjustStock(batchId: string) {
    const deltaStr = prompt('库存调整数量（正数盘盈，负数盘亏）：');
    if (deltaStr == null || deltaStr.trim() === '') return;
    const delta = Number(deltaStr);
    if (!Number.isFinite(delta) || delta === 0) {
      alert('请输入非零数字');
      return;
    }
    const reason = prompt('调整原因：') || '';
    try {
      const res = await api.adjustInventoryLevel({ batch_id: batchId, delta, reason });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '调整库存失败');
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleAddOption(productId: string) {
    const name = prompt('新属性键名称（如 颜色 / 尺码）：');
    if (!name) return;
    try {
      const res = await api.createProductOption({ product_id: productId, name });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '添加选项失败');
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleRenameOption(optionId: string, currentName: string) {
    const name = prompt('新名称：', currentName);
    if (!name || name === currentName) return;
    try {
      const res = await api.updateProductOption({ option_id: optionId, name });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '更新选项失败');
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleDeleteOption(optionId: string) {
    if (!confirm('确认删除此属性键？')) return;
    try {
      const res = await api.deleteProductOption({ option_id: optionId });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '删除选项失败');
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleCreateBatch(e: React.FormEvent) {
    e.preventDefault();
    try {
      const res = await api.createInventoryBatch({
        variant_id: batchVariantId,
        batch_no: batchNo,
        location_id: locationId,
        stock_qty: stockQty,
        retail_price_cents: retailPrice,
        cost_price_cents: costPrice,
        video_url: videoUrl,
      });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '创建批次失败');
      setBatchNo(''); setVideoUrl('');
      setShowBatchForm(false);
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold">商品管理</h1>
        <div className="flex gap-2">
          <button onClick={() => setShowForm(!showForm)} className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 text-sm">
            {showForm ? '取消' : '新建商品'}
          </button>
          <button onClick={() => setShowVariantForm(!showVariantForm)} className="px-4 py-2 bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 text-sm">
            新建变体
          </button>
          <button onClick={() => setShowBatchForm(!showBatchForm)} className="px-4 py-2 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 text-sm">
            新建批次
          </button>
        </div>
      </div>

      {/* Search */}
      <div className="mb-4">
        <input
          type="text"
          placeholder="搜索商品..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && load()}
          className="px-3 py-2 border border-gray-300 rounded-lg text-sm w-64"
        />
        <button onClick={load} className="ml-2 px-3 py-2 bg-gray-100 rounded-lg text-sm hover:bg-gray-200">搜索</button>
      </div>

      {/* Create Product Form */}
      {showForm && (
        <form onSubmit={handleCreateProduct} className="mb-6 p-4 bg-white rounded-xl border border-gray-200 space-y-3">
          <h2 className="font-semibold">新建商品</h2>
          <div className="grid grid-cols-2 gap-3">
            <input placeholder="标题" value={title} onChange={(e) => setTitle(e.target.value)} required className="px-3 py-2 border rounded-lg text-sm" />
            <input placeholder="slug (唯一)" value={slug} onChange={(e) => setSlug(e.target.value)} required className="px-3 py-2 border rounded-lg text-sm" />
          </div>
          <textarea placeholder="描述" value={description} onChange={(e) => setDescription(e.target.value)} className="w-full px-3 py-2 border rounded-lg text-sm" rows={2} />
          <select value={status} onChange={(e) => setStatus(e.target.value)} className="px-3 py-2 border rounded-lg text-sm">
            <option value="draft">草稿</option>
            <option value="published">已发布</option>
          </select>
          <button type="submit" className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm hover:bg-blue-700">创建</button>
        </form>
      )}

      {/* Create Variant Form */}
      {showVariantForm && (
        <form onSubmit={handleCreateVariant} className="mb-6 p-4 bg-white rounded-xl border border-gray-200 space-y-3">
          <h2 className="font-semibold">新建变体 (SKU)</h2>
          <select value={variantProductId} onChange={(e) => setVariantProductId(e.target.value)} required className="w-full px-3 py-2 border rounded-lg text-sm">
            <option value="">选择商品...</option>
            {products.map((p) => <option key={p.id} value={p.id}>{p.title}</option>)}
          </select>
          <input placeholder="SKU 编码" value={skuCode} onChange={(e) => setSkuCode(e.target.value)} required className="w-full px-3 py-2 border rounded-lg text-sm" />
          <button type="submit" className="px-4 py-2 bg-indigo-600 text-white rounded-lg text-sm hover:bg-indigo-700">创建变体</button>
        </form>
      )}

      {/* Create Batch Form */}
      {showBatchForm && (
        <form onSubmit={handleCreateBatch} className="mb-6 p-4 bg-white rounded-xl border border-gray-200 space-y-3">
          <h2 className="font-semibold">新建库存批次</h2>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="text-xs text-gray-500">商品 → 变体</label>
              <select value={batchVariantId} onChange={(e) => setBatchVariantId(e.target.value)} required className="w-full px-3 py-2 border rounded-lg text-sm">
                <option value="">选择变体...</option>
                {products.flatMap((p) => (p.variants || []).map((v) => (
                  <option key={v.id} value={v.id}>{p.title} / {v.sku_code}</option>
                )))}
              </select>
            </div>
            <div>
              <label className="text-xs text-gray-500">批次号</label>
              <input placeholder="批次号" value={batchNo} onChange={(e) => setBatchNo(e.target.value)} required className="w-full px-3 py-2 border rounded-lg text-sm" />
            </div>
          </div>
          <div className="grid grid-cols-4 gap-3">
            <div>
              <label className="text-xs text-gray-500">库存数量</label>
              <input type="number" value={stockQty} onChange={(e) => setStockQty(+e.target.value)} className="w-full px-3 py-2 border rounded-lg text-sm" />
            </div>
            <div>
              <label className="text-xs text-gray-500">零售价(分)</label>
              <input type="number" value={retailPrice} onChange={(e) => setRetailPrice(+e.target.value)} className="w-full px-3 py-2 border rounded-lg text-sm" />
            </div>
            <div>
              <label className="text-xs text-gray-500">成本价(分)</label>
              <input type="number" value={costPrice} onChange={(e) => setCostPrice(+e.target.value)} className="w-full px-3 py-2 border rounded-lg text-sm" />
            </div>
            <div>
              <label className="text-xs text-gray-500">仓库 ID</label>
              <input placeholder="UUID" value={locationId} onChange={(e) => setLocationId(e.target.value)} required className="w-full px-3 py-2 border rounded-lg text-sm" />
            </div>
          </div>
          <div>
            <label className="text-xs text-gray-500">溯源视频 URL（质检凭证，必填）</label>
            <input placeholder="https://..." value={videoUrl} onChange={(e) => setVideoUrl(e.target.value)} required className="w-full px-3 py-2 border rounded-lg text-sm" />
          </div>
          <button type="submit" className="px-4 py-2 bg-emerald-600 text-white rounded-lg text-sm hover:bg-emerald-700">创建批次</button>
        </form>
      )}

      {/* Error */}
      {error && <div className="mb-4 text-red-600">{error}</div>}

      {/* Product Table */}
      {loading ? (
        <div className="text-gray-500">加载中...</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm bg-white rounded-xl overflow-hidden border border-gray-200">
            <thead className="bg-gray-50 text-left">
              <tr>
                <th className="px-4 py-3 font-medium text-gray-600">标题</th>
                <th className="px-4 py-3 font-medium text-gray-600">状态</th>
                <th className="px-4 py-3 font-medium text-gray-600">价格</th>
                <th className="px-4 py-3 font-medium text-gray-600">库存</th>
                <th className="px-4 py-3 font-medium text-gray-600">属性键</th>
                <th className="px-4 py-3 font-medium text-gray-600">变体</th>
                <th className="px-4 py-3 font-medium text-gray-600">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {products.map((p) => (
                <tr key={p.id} className="hover:bg-gray-50">
                  <td className="px-4 py-3">
                    <div className="font-medium">{p.title}</div>
                    <div className="text-xs text-gray-400">{p.slug}</div>
                  </td>
                  <td className="px-4 py-3">
                    <span className={`px-2 py-0.5 rounded text-xs ${p.status === 'published' ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-600'}`}>
                      {p.status}
                    </span>
                  </td>
                  <td className="px-4 py-3">{fmt(p.min_price_cents, p.variants?.[0]?.batches?.[0]?.currency)}</td>
                  <td className="px-4 py-3">{p.total_stock}</td>
                  <td className="px-4 py-3">
                    <div className="text-xs space-y-1 min-w-[120px]">
                      {(p.options || []).map((o) => (
                        <div key={o.id} className="flex items-center justify-between gap-2">
                          <span className="text-gray-600">{o.name}</span>
                          <span className="flex gap-1.5 shrink-0">
                            <button onClick={() => handleRenameOption(o.id, o.name)} className="text-blue-600 hover:underline">改名</button>
                            <button onClick={() => handleDeleteOption(o.id)} className="text-red-600 hover:underline">删除</button>
                          </span>
                        </div>
                      ))}
                      <button onClick={() => handleAddOption(p.id)} className="text-blue-600 hover:underline">+ 添加属性键</button>
                    </div>
                  </td>
                  <td className="px-4 py-3">
                    <div className="text-xs space-y-1.5 min-w-[220px]">
                      {(p.variants || []).map((v) => (
                        <div key={v.id} className="border-b border-gray-50 pb-1.5 last:border-0 last:pb-0">
                          <div className="flex items-center justify-between gap-2">
                            <span className="text-gray-600">
                              {v.sku_code}: {v.total_stock} 件, {fmt(v.min_price_cents, v.batches?.[0]?.currency)}
                              <span className={`ml-1 ${v.status === 'active' ? 'text-gray-400' : 'text-amber-500'}`}>({v.status})</span>
                            </span>
                            <span className="flex gap-1.5 shrink-0">
                              <button onClick={() => handleToggleVariant(v)} className="text-blue-600 hover:underline">
                                {v.status === 'active' ? '停用' : '启用'}
                              </button>
                              <button onClick={() => handleDeleteVariant(v.id)} className="text-red-600 hover:underline">删除</button>
                            </span>
                          </div>
                          {(v.batches || []).map((b) => (
                            <div key={b.id} className="flex items-center justify-between gap-2 text-gray-400 pl-2 mt-0.5">
                              <span>批次 {b.batch_no}: 可售 {b.available_qty}</span>
                              <button onClick={() => handleAdjustStock(b.id)} className="text-emerald-600 hover:underline shrink-0">调库存</button>
                            </div>
                          ))}
                        </div>
                      ))}
                      {(p.variants || []).length === 0 && <span className="text-gray-300">无变体</span>}
                    </div>
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex gap-2">
                      <button onClick={() => handlePublish(p)} className="text-xs text-blue-600 hover:underline">
                        {p.status === 'published' ? '下架' : '发布'}
                      </button>
                      <button onClick={() => handleDelete(p.id)} className="text-xs text-red-600 hover:underline">删除</button>
                    </div>
                  </td>
                </tr>
              ))}
              {products.length === 0 && (
                <tr><td colSpan={7} className="px-4 py-8 text-center text-gray-400">暂无商品</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
