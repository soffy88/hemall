/**
 * 前台陈列品类 (社区小超市模型, 2026-11-01 定):
 *   生鲜引流 + 热食窗口, 先跑通一个模型。山姆货、火锅堂食、"别家没有"的
 *   招牌品全部先放下。
 * 前台只显示这 5 类; 其余品类的数据保留在库里, 仅前台不显示 (不删数据)。
 *   显示: 蔬菜豆品 / 时令水果(生鲜引流, 计划 SKU 表未单列, 先留) /
 *         肉禽蛋 / 乳品烘焙 / 粮油调味
 *   隐藏: 冷冻速食 / 海鲜水产 / 休闲零食 / 酒水饮料 / 日用百货 / 数码家电
 * 恢复显示: 把 slug 加回 VISIBLE_CATEGORY_SLUGS 即可, 无需动数据。
 */
export const VISIBLE_CATEGORY_SLUGS: readonly string[] = [
  'vegetables',
  'fruits',
  'meat-eggs',
  'dairy-bakery',
  'grain-oil',
];

export function isVisibleCategorySlug(slug: string | null | undefined): boolean {
  return !!slug && VISIBLE_CATEGORY_SLUGS.includes(slug);
}

export function isAssortmentProduct(product: {
  category_slug?: string | null;
}): boolean {
  return isVisibleCategorySlug(product.category_slug);
}
