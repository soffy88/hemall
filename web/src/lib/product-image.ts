import type { StorefrontProduct } from '@/types/api';

/**
 * 取商品首图 (小象超市白底主图)。
 *
 * 优先级: 首个 variant 首个 batch 的 shelf_image_url → media_assets[0] → null
 * (null 由调用方渲染 emoji 占位图, 加载失败也回落占位而不是留白块)。
 */
export function productImage(p: StorefrontProduct): string | null {
  const b = p.variants?.[0]?.batches?.[0];
  if (b?.shelf_image_url) return b.shelf_image_url;
  if (b?.media_assets?.length) return b.media_assets[0];
  return null;
}
