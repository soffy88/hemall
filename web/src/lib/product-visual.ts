/** 商品视觉占位 — 系统里没有任何商品图片数据，用关键词图标 + 哈希渐变色代替
 * 千篇一律的单一 📦，不编造不存在的商品图片。 */

const KEYWORD_ICONS: [string, string][] = [
  ['耳机', '🎧'],
  ['音箱', '🔊'],
  ['键盘', '⌨️'],
  ['显示器', '🖥️'],
  ['手表', '⌚'],
  ['椅', '🪑'],
  ['手机', '📱'],
  ['电脑', '💻'],
  ['相机', '📷'],
  ['灯', '💡'],
  ['包', '👜'],
  ['鞋', '👟'],
];

export function productIcon(title: string): string {
  const hit = KEYWORD_ICONS.find(([keyword]) => title.includes(keyword));
  return hit ? hit[1] : '📦';
}

const GRADIENTS = [
  'from-emerald-50 to-teal-100',
  'from-sky-50 to-indigo-100',
  'from-amber-50 to-orange-100',
  'from-rose-50 to-pink-100',
  'from-violet-50 to-purple-100',
  'from-lime-50 to-emerald-100',
];

function hashString(value: string): number {
  let hash = 0;
  for (let i = 0; i < value.length; i++) {
    hash = (hash * 31 + value.charCodeAt(i)) | 0;
  }
  return Math.abs(hash);
}

/** Tailwind `from-* to-*` 渐变片段，配合 `bg-gradient-to-br` 使用。 */
export function productGradient(id: string): string {
  return GRADIENTS[hashString(id) % GRADIENTS.length];
}
