import type { StorefrontProduct } from '@/types/api';

/**
 * 取商品首图 (小象超市白底主图)。
 *
 * 优先级: 首个 variant 首个 batch 的 shelf_image_url → media_assets[0] → null
 * (null 由调用方渲染 emoji 占位图, 加载失败也回落占位而不是留白块)。
 */
/**
 * 取第一个媒体引用。后端两列历史上都可能存成 JSON 数组字符串
 * ('["<url>"]'，蜘蛛/传货写入曾共用同一份 JSON 串写 shelf 列），
 * 直接当地址用必加载失败、全站回落 emoji；这里与后端
 * _media_ref_from_value 保持同一容错：数组串先解析再取首项。
 */
function firstMediaRef(value: unknown): string | null {
  if (!value) return null;
  if (Array.isArray(value)) {
    for (const item of value) {
      const ref = firstMediaRef(item);
      if (ref) return ref;
    }
    return null;
  }
  if (typeof value === 'string') {
    const text = value.trim();
    if (!text) return null;
    if (text.startsWith('[')) {
      try {
        return firstMediaRef(JSON.parse(text));
      } catch {
        return null;
      }
    }
    return text;
  }
  return null;
}

export function productImage(p: StorefrontProduct): string | null {
  const b = p.variants?.[0]?.batches?.[0];
  return firstMediaRef(b?.shelf_image_url) ?? firstMediaRef(b?.media_assets) ?? null;
}
