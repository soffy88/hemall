/** lib/product-image 纯逻辑单测（fix-img 核验：商品首图取值优先级）。
 *
 *  - shelf_image_url 优先于 media_assets；
 *  - shelf_image_url 缺失时回落 media_assets[0]；
 *  - 两者皆空 / 无 variant / 无 batch 返回 null（调用方渲染 emoji 占位图）。
 */
import { describe, expect, it } from 'vitest';
import { productImage } from '@/lib/product-image';
import type { StorefrontProduct } from '@/types/api';

function product(batches: unknown): StorefrontProduct {
  return {
    id: 'p1',
    title: '测试商品',
    variants: [{ id: 'v1', batches }],
  } as unknown as StorefrontProduct;
}

describe('productImage 取值优先级', () => {
  it('shelf_image_url 存在时优先返回', () => {
    const p = product([
      {
        shelf_image_url: '/products/apple.png',
        media_assets: ['/media/other.png'],
      },
    ]);
    expect(productImage(p)).toBe('/products/apple.png');
  });

  it('shelf_image_url 缺失时回落 media_assets[0]', () => {
    const p = product([{ shelf_image_url: null, media_assets: ['/media/a.png', '/media/b.png'] }]);
    expect(productImage(p)).toBe('/media/a.png');
  });

  it('两者皆空返回 null', () => {
    expect(productImage(product([{ shelf_image_url: null, media_assets: [] }]))).toBeNull();
    expect(productImage(product([{ shelf_image_url: null, media_assets: null }]))).toBeNull();
  });

  it('无 batch / 无 variant 返回 null', () => {
    expect(productImage(product([]))).toBeNull();
    expect(
      productImage({ id: 'p2', title: 'x', variants: [] } as unknown as StorefrontProduct),
    ).toBeNull();
    expect(
      productImage({ id: 'p3', title: 'x' } as unknown as StorefrontProduct),
    ).toBeNull();
  });
});
