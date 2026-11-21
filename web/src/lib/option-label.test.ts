import { describe, expect, it } from 'vitest';

import { displayOptionLabel, isInternalSku } from './option-label';

describe('displayOptionLabel', () => {
  it('只保留真正的规格维度, 内部键与 URL 不渲染', () => {
    expect(
      displayOptionLabel({
        颜色: '曜石黑',
        来源: '苏宁自有品牌苏宁自营旗舰店',
        image_url: 'https://imgservice4.suning.cn/x.jpg',
        detail_url: 'https://m.suning.com/product/1.html',
        source_id: '12441345917',
      }),
    ).toBe('曜石黑');
  });

  it('纯内部键时回落到默认规格', () => {
    expect(
      displayOptionLabel({
        来源: 'xx',
        detail_url: 'https://m.suning.com/product/1.html',
      }),
    ).toBe('默认规格');
  });

  it('空值回落', () => {
    expect(displayOptionLabel(null)).toBe('默认规格');
    expect(displayOptionLabel({}, '')).toBe('');
  });
});

describe('isInternalSku', () => {
  it('识别内部来源 sku', () => {
    expect(isInternalSku('EXT-suning-12441345917')).toBe(true);
    expect(isInternalSku('KB87-BK')).toBe(false);
    expect(isInternalSku(null)).toBe(false);
  });
});
