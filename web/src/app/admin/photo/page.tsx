'use client';
/* eslint-disable react-hooks/set-state-in-effect -- 页面加载沿用后台其他页的 useEffect+fetch 模式 */

import { useCallback, useEffect, useState } from 'react';
import { api } from '@/lib/api-client';
import { errMessage } from '@/lib/errors';
import { formatMoney } from '@/lib/format';
import ProductPhotoEditor from '@/components/ProductPhotoEditor';
import type { PhotoDraftResult, PhotoReferenceItem, PhotoReferenceResponse } from '@/types/api';

/**
 * 实拍补图 + 拍照上新。
 * - 实拍补图：筛出仍用参考图（苏宁/Wikimedia）的在线商品，逐个拍照换图；
 *   进度：门店 99 SKU 口径 x/98；全部在线商品口径 x/(参考图+已换)。
 * - 拍照上新：拍照 + 名称/分类/价格/计价方式，存草稿，人工发布。
 */
export default function PhotoWorkbenchPage() {
  const [tab, setTab] = useState<'replace' | 'new'>('replace');
  const [scope, setScope] = useState<'store' | 'all'>('store');
  const [search, setSearch] = useState('');
  const [data, setData] = useState<PhotoReferenceResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState<PhotoReferenceItem | null>(null);
  const [flash, setFlash] = useState('');

  const load = useCallback(() => {
    setLoading(true);
    api.photoReference({ scope, search: search || undefined, limit: 120 })
      .then(setData)
      .catch((e) => setError(errMessage(e)))
      .finally(() => setLoading(false));
  }, [scope, search]);

  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const productId = params.get('product');
    const title = params.get('title') || '商品';
    if (productId) {
      setSelected({ id: productId, title, slug: '', category_name: null, batch_id: null, batch_no: null, shelf_image_url: null, image_url: null, is_reference: true });
    }
  }, []);

  const progress = data?.progress;
  const done = progress ? progress.replaced : 0;
  const total = progress ? progress.total : 98;

  function onPhotoChanged() {
    setFlash('已更新商品图');
    load();
    window.setTimeout(() => setFlash(''), 2500);
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h1 className="text-2xl font-bold">拍照换图 / 拍照上新</h1>
          <p className="text-xs text-gray-500 mt-1">店员手机实拍 → 服务端压缩去 EXIF → 立即替换商城商品卡。</p>
        </div>
        <div className="flex gap-2">
          <button
            onClick={() => setTab('replace')}
            className={`px-3 py-2 rounded-lg text-sm ${tab === 'replace' ? 'bg-emerald-600 text-white' : 'bg-white border border-gray-200 text-gray-700'}`}
          >实拍补图</button>
          <button
            onClick={() => setTab('new')}
            className={`px-3 py-2 rounded-lg text-sm ${tab === 'new' ? 'bg-emerald-600 text-white' : 'bg-white border border-gray-200 text-gray-700'}`}
          >拍照上新</button>
        </div>
      </div>

      {flash && <p className="text-sm text-emerald-700 bg-emerald-50 border border-emerald-200 rounded-lg px-3 py-2">{flash}</p>}

      {tab === 'replace' ? (
        <>
          <div className="bg-white border border-gray-200 rounded-xl p-4 space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex gap-2">
                <button
                  onClick={() => setScope('store')}
                  className={`px-3 py-1.5 rounded-lg text-sm ${scope === 'store' ? 'bg-gray-900 text-white' : 'bg-gray-100 text-gray-700'}`}
                >门店 99 SKU</button>
                <button
                  onClick={() => setScope('all')}
                  className={`px-3 py-1.5 rounded-lg text-sm ${scope === 'all' ? 'bg-gray-900 text-white' : 'bg-gray-100 text-gray-700'}`}
                >全部在线商品</button>
              </div>
              <div className="text-sm font-semibold">
                已换 {done}/{total}
                <span className="ml-2 text-xs font-normal text-gray-500">
                  {scope === 'store'
                    ? `线上门店商品 ${progress?.store_total ?? '—'} 个，其中仍为参考图 ${data?.items.length ?? 0} 个`
                    : `仍为参考图 ${progress?.reference_remaining ?? 0} 个`}
                </span>
              </div>
            </div>
            <div className="h-2 bg-gray-100 rounded-full overflow-hidden">
              <div
                className="h-full bg-emerald-500 transition-all"
                style={{ width: `${total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0}%` }}
              />
            </div>
            {scope === 'store' && (
              <p className="text-[11px] text-gray-400">口径：PRICE_REPORT「99 SKU 中 98 个有参考图」；6 个 SKU 尚未建批次，线上现存门店商品以实际列表为准。</p>
            )}
            <div className="flex gap-2">
              <input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') load(); }}
                placeholder="搜索商品名"
                className="px-3 py-2 border border-gray-300 rounded-lg text-sm w-full sm:w-64"
              />
              <button onClick={load} className="px-3 py-2 bg-gray-100 rounded-lg text-sm hover:bg-gray-200">搜索</button>
            </div>
          </div>

          {selected && (
            <ProductPhotoEditor
              productId={selected.id}
              productTitle={selected.title}
              onClose={() => setSelected(null)}
              onChanged={onPhotoChanged}
            />
          )}

          {error && <p className="text-sm text-red-600">{error}</p>}

          {loading ? (
            <p className="text-sm text-gray-400">加载中...</p>
          ) : (data?.items.length ?? 0) === 0 ? (
            <div className="bg-white border border-gray-200 rounded-xl p-6 text-center text-sm text-gray-500">
              这个范围已经没有参考图商品了 🎉
            </div>
          ) : (
            <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-3">
              {data!.items.map((item) => (
                <div key={item.id} className="bg-white border border-gray-200 rounded-xl p-3 flex gap-3">
                  <div className="w-20 h-20 shrink-0 rounded-lg bg-gray-50 overflow-hidden">
                    {item.image_url ? (
                      // eslint-disable-next-line @next/next/no-img-element
                      <img src={item.image_url} alt={item.title} className="w-full h-full object-cover" />
                    ) : (
                      <div className="w-full h-full flex items-center justify-center text-2xl">🥬</div>
                    )}
                  </div>
                  <div className="min-w-0 flex-1 space-y-1">
                    <div className="text-sm font-medium line-clamp-2">{item.title}</div>
                    <div className="text-xs text-gray-500">{item.category_name || '未分类'}{item.batch_no ? ` · ${item.batch_no}` : ''}</div>
                    <div className="flex flex-wrap items-center gap-2 pt-1">
                      <span className="px-1.5 py-0.5 rounded bg-amber-100 text-amber-700 text-[11px]">参考图</span>
                      <button
                        onClick={() => setSelected(item)}
                        className="px-2.5 py-1.5 bg-emerald-600 text-white rounded-lg text-xs hover:bg-emerald-700"
                      >拍照换图</button>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </>
      ) : (
        <NewProductForm onCreated={() => { setFlash('草稿已创建，去商品管理补充资料后发布'); load(); }} />
      )}
    </div>
  );
}

function NewProductForm({ onCreated }: { onCreated: () => void }) {
  const [categories, setCategories] = useState<Array<{ id: string; name: string }>>([]);
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState('');
  const [categoryId, setCategoryId] = useState('');
  const [price, setPrice] = useState('');
  const [mode, setMode] = useState<'piece' | 'weight'>('piece');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState<PhotoDraftResult | null>(null);

  useEffect(() => {
    api.photoCategories()
      .then((rows) => {
        setCategories(rows);
        if (rows[0]) setCategoryId(rows[0].id);
      })
      .catch((e) => setError(errMessage(e)));
  }, []);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!file) { setError('请先拍照或选择图片'); return; }
    const yuan = Number(price);
    if (!Number.isFinite(yuan) || yuan <= 0) { setError('请填写有效价格'); return; }
    setBusy(true);
    setError('');
    try {
      const form = new FormData();
      form.append('file', file);
      form.append('title', title.trim());
      form.append('category_id', categoryId);
      form.append('price_cents', String(Math.round(yuan * 100)));
      form.append('pricing_mode', mode);
      const res = await api.createPhotoProduct(form);
      setResult(res);
      setFile(null);
      setTitle('');
      setPrice('');
      onCreated();
    } catch (e) {
      setError(errMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="bg-white border border-gray-200 rounded-xl p-4 space-y-3 max-w-2xl">
      <div>
        <h2 className="font-semibold text-sm">拍照新增商品（保存为草稿）</h2>
        <p className="text-xs text-gray-500 mt-0.5">VLM 识货不可用时不用等：直接手填名称/分类/价格，草稿不会出现在商城，需人工点发布。</p>
      </div>
      <form onSubmit={submit} className="space-y-3">
        <div>
          <label className="block text-xs text-gray-500 mb-1">商品照片</label>
          <input
            type="file"
            accept="image/*"
            capture="environment"
            onChange={(e) => setFile(e.target.files?.[0] || null)}
            className="text-sm w-full"
          />
          <p className="text-[11px] text-gray-400 mt-1">手机点开会优先调相机；也可在系统弹窗里改从相册选。</p>
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div>
            <label className="block text-xs text-gray-500 mb-1">商品名称</label>
            <input value={title} onChange={(e) => setTitle(e.target.value)} required maxLength={80} className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm" placeholder="如：本地菜心" />
          </div>
          <div>
            <label className="block text-xs text-gray-500 mb-1">分类</label>
            <select value={categoryId} onChange={(e) => setCategoryId(e.target.value)} required className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm">
              {categories.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </select>
          </div>
          <div>
            <label className="block text-xs text-gray-500 mb-1">计价方式</label>
            <select value={mode} onChange={(e) => setMode(e.target.value as 'piece' | 'weight')} className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm">
              <option value="piece">按份 / 按件</option>
              <option value="weight">按斤（500g）</option>
            </select>
          </div>
          <div>
            <label className="block text-xs text-gray-500 mb-1">{mode === 'weight' ? '价格（元/斤）' : '价格（元/份）'}</label>
            <input value={price} onChange={(e) => setPrice(e.target.value)} inputMode="decimal" required className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm" placeholder="0.00" />
          </div>
        </div>
        {error && <p className="text-sm text-red-600">{error}</p>}
        <button type="submit" disabled={busy} className="px-4 py-2 bg-emerald-600 text-white rounded-lg text-sm hover:bg-emerald-700 disabled:opacity-50">
          {busy ? '创建中...' : '存为草稿'}
        </button>
      </form>
      {result && (
        <div className="text-sm bg-emerald-50 border border-emerald-200 rounded-lg p-3 space-y-1">
          <div>已创建草稿「{result.title}」 · {result.pricing_mode === 'weight' ? '按斤' : '按份'} {formatMoney(result.price_cents, 'CNY')}</div>
          <a href={`/admin/products?search=${encodeURIComponent(result.title)}`} className="text-emerald-700 hover:underline">去商品管理补充库存并发布 →</a>
        </div>
      )}
    </div>
  );
}
