'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '@/lib/api-client';
import { errMessage } from '@/lib/errors';
import type { ProductPhoto } from '@/types/api';

/**
 * 商品实拍相册：拍照/相册上传（可多张）、设主图、删除、排序、恢复旧图。
 * 设主图会同步前台商品卡（后端更新 inventory_batch.shelf_image_url）。
 */
export default function ProductPhotoEditor({
  productId,
  productTitle,
  onClose,
  onChanged,
}: {
  productId: string;
  productTitle: string;
  onClose?: () => void;
  onChanged?: () => void;
}) {
  const [photos, setPhotos] = useState<ProductPhoto[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const cameraRef = useRef<HTMLInputElement>(null);
  const galleryRef = useRef<HTMLInputElement>(null);

  const load = useCallback(() => {
    api.productPhotos(productId)
      .then((r) => setPhotos(r.items))
      .catch((e) => setError(errMessage(e)))
      .finally(() => setLoading(false));
  }, [productId]);

  useEffect(() => { load(); }, [load]);

  async function upload(files: FileList | null) {
    if (!files || files.length === 0) return;
    setBusy(true);
    setError('');
    setNotice('');
    const hasPrimary = photos.some((p) => p.is_primary && p.status === 'active');
    let done = 0;
    try {
      for (const [index, file] of Array.from(files).entries()) {
        if (!file.type.startsWith('image/')) {
          throw new Error(`「${file.name}」不是图片`);
        }
        if (file.size > 10 * 1024 * 1024) {
          throw new Error(`「${file.name}」超过 10MB`);
        }
        await api.uploadProductPhoto(productId, file, {
          makePrimary: !hasPrimary && index === 0,
        });
        done += 1;
      }
      setNotice(`已上传 ${done} 张`);
      load();
      onChanged?.();
    } catch (e) {
      setError(errMessage(e));
      if (done > 0) {
        setNotice(`已上传 ${done} 张，后续失败`);
        load();
        onChanged?.();
      }
    } finally {
      setBusy(false);
      if (cameraRef.current) cameraRef.current.value = '';
      if (galleryRef.current) galleryRef.current.value = '';
    }
  }

  async function act(fn: () => Promise<unknown>, message: string) {
    setBusy(true);
    setError('');
    setNotice('');
    try {
      await fn();
      setNotice(message);
      load();
      onChanged?.();
    } catch (e) {
      setError(errMessage(e));
    } finally {
      setBusy(false);
    }
  }

  async function remove(photo: ProductPhoto) {
    if (!confirm(photo.is_primary ? '删除主图？删除后会自动用下一张实拍做主图。' : '删除这张图片？')) return;
    await act(() => api.deletePhoto(productId, photo.id), '已删除（文件保留，可恢复）');
  }

  async function move(photo: ProductPhoto, dir: -1 | 1) {
    const active = photos.filter((p) => p.status === 'active');
    const index = active.findIndex((p) => p.id === photo.id);
    const target = index + dir;
    if (index < 0 || target < 0 || target >= active.length) return;
    const ordered = [...active];
    [ordered[index], ordered[target]] = [ordered[target], ordered[index]];
    await act(() => api.reorderPhotos(productId, ordered.map((p) => p.id)), '已调整顺序');
  }

  const active = photos.filter((p) => p.status === 'active');
  const archived = photos.filter((p) => p.status !== 'active');

  return (
    <section className="bg-white border border-gray-200 rounded-xl p-4 space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 className="font-semibold text-sm">商品实拍图 · {productTitle}</h3>
          <p className="text-xs text-gray-500 mt-0.5">主图会立即替换商城商品卡；旧图保留可恢复。</p>
        </div>
        {onClose && (
          <button onClick={onClose} className="text-xs text-gray-500 hover:text-gray-800">关闭</button>
        )}
      </div>

      <div className="flex flex-wrap gap-2">
        <input
          ref={cameraRef}
          type="file"
          accept="image/*"
          capture="environment"
          multiple
          className="hidden"
          onChange={(e) => upload(e.target.files)}
        />
        <input
          ref={galleryRef}
          type="file"
          accept="image/*"
          multiple
          className="hidden"
          onChange={(e) => upload(e.target.files)}
        />
        <button
          type="button"
          disabled={busy}
          onClick={() => cameraRef.current?.click()}
          className="px-3 py-2 bg-emerald-600 text-white rounded-lg text-sm hover:bg-emerald-700 disabled:opacity-50"
        >
          📷 拍照上传
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => galleryRef.current?.click()}
          className="px-3 py-2 bg-gray-100 text-gray-700 rounded-lg text-sm hover:bg-gray-200 disabled:opacity-50"
        >
          从相册选
        </button>
        <span className="text-xs text-gray-400 self-center">支持 JPG/PNG/WebP/HEIC，单张 ≤10MB，可多选</span>
      </div>

      {error && <p className="text-sm text-red-600">{error}</p>}
      {notice && <p className="text-sm text-emerald-700">{notice}</p>}

      {loading ? (
        <p className="text-sm text-gray-400">加载中...</p>
      ) : photos.length === 0 ? (
        <p className="text-sm text-gray-400">还没有实拍图，点「拍照上传」开始。</p>
      ) : (
        <>
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
            {active.map((photo, index) => (
              <div key={photo.id} className="border border-gray-200 rounded-lg overflow-hidden">
                <div className="relative bg-gray-50 aspect-square">
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img src={photo.thumb_url || photo.url} alt="" className="w-full h-full object-cover" />
                  {photo.is_primary && (
                    <span className="absolute left-1 top-1 px-1.5 py-0.5 rounded bg-emerald-600 text-white text-[11px]">主图</span>
                  )}
                </div>
                <div className="p-1.5 flex flex-wrap gap-1.5 text-xs">
                  {!photo.is_primary && (
                    <button
                      disabled={busy}
                      onClick={() => act(() => api.setPhotoPrimary(productId, photo.id), '已设为主图')}
                      className="text-emerald-700 hover:underline disabled:opacity-40"
                    >设主图</button>
                  )}
                  <button disabled={busy || index === 0} onClick={() => move(photo, -1)} className="text-gray-600 hover:underline disabled:opacity-30">←</button>
                  <button disabled={busy || index === active.length - 1} onClick={() => move(photo, 1)} className="text-gray-600 hover:underline disabled:opacity-30">→</button>
                  <button disabled={busy} onClick={() => remove(photo)} className="text-red-600 hover:underline disabled:opacity-40">删除</button>
                </div>
              </div>
            ))}
          </div>

          {archived.length > 0 && (
            <div className="border-t pt-3">
              <p className="text-xs text-gray-500 mb-2">历史图（换下/删除，文件仍在）</p>
              <div className="flex flex-wrap gap-2">
                {archived.map((photo) => (
                  <div key={photo.id} className="w-24 border border-gray-200 rounded-lg overflow-hidden opacity-80">
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img src={photo.thumb_url || photo.url} alt="" className="w-full aspect-square object-cover" />
                    <div className="p-1 flex items-center justify-between text-[11px]">
                      <span className="text-gray-400">{photo.status === 'deleted' ? '已删除' : '已换下'}</span>
                      <button
                        disabled={busy}
                        onClick={() => act(() => api.restorePhoto(productId, photo.id), '已恢复')}
                        className="text-emerald-700 hover:underline disabled:opacity-40"
                      >恢复</button>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </>
      )}
    </section>
  );
}
