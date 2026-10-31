/** 前台规格标签: 只展示真正的规格维度, 内部键与 URL 绝不渲染。 */

const INTERNAL_OPTION_KEYS = new Set([
  '来源',
  'store',
  'image_url',
  'detail_url',
  'source_id',
]);

export function displayOptionLabel(
  optionValues: Record<string, string> | null | undefined,
  fallback = '默认规格',
): string {
  const parts = Object.entries(optionValues || {})
    .filter(([key, value]) => {
      if (INTERNAL_OPTION_KEYS.has(key)) return false;
      const text = String(value ?? '').trim();
      if (!text) return false;
      return !/^https?:\/\//i.test(text);
    })
    .map(([, value]) => String(value).trim());
  return parts.join(' / ') || fallback;
}

/** 内部来源 sku (如 EXT-suning-...) 不在前台展示。 */
export function isInternalSku(sku: string | null | undefined): boolean {
  if (!sku) return false;
  return /^EXT-/i.test(sku.trim());
}
