-- 20261110 core2 商品数据复核（可回滚：只改 status / search_aliases / 账号标记）。
-- 不删除任何商品、批次、订单、账号。
BEGIN;

-- 1) 重复上架：同一商品两条在线，保留较早/有实拍的一条，另一条转草稿。
UPDATE product SET status = 'draft', updated_at = NOW()
WHERE id IN (
  '73f98b4e-7cc0-4ec2-8a9e-a2050b2ca79b', -- [10月]伊利纯牛奶 21盒（重复，保留 f41042ea）
  'b0c95ebb-7adc-4159-bf4f-5dea67b02e52', -- [9月]伊利臻浓牛奶（重复，保留 2e87e7b4）
  '92dc08d9-634f-44cf-8add-c4cfcae4b49e', -- 凡士林润唇膏（重复且更贵，保留 01a800d5）
  '019fc6b1-851e-74d7-a52b-92f75aa58282'  -- 土豆 daily 类目重复（保留 vegetables fa1c99c5）
) AND status = 'published';

-- 2) 无在售批次 / 无分类（前台本就不可见；转草稿并注明原因，补货后再发布）。
UPDATE product SET status = 'draft', updated_at = NOW()
WHERE id IN (
  '2558e726-5a0f-4b24-a2be-1f4779aa20fc',
  'dc6bac99-3241-47d8-ac74-66cb05380813',
  'aa912788-0ff7-4c44-ab4a-11d6773ed263',
  '4c70d1ef-750a-4418-9039-fbbf02b710f9',
  '8b5b773e-2349-4045-a3fa-1c1dc4894117',
  '248e23f4-aaa3-4007-b7d3-5fb859dd4562',
  '9cc2178b-8327-45f3-b328-8ceb213b1dcc',
  'a6169895-c8c9-4adc-b74f-cd763a300baf',
  'cdcc5975-da08-430b-9a44-65130595c4ae',
  '2c52d693-2c48-4a78-9515-84eb35b943d3',
  '2f5e5d6a-fe16-4452-8cbe-285e8d7ede9a',
  'e77dc9c9-db83-487c-83c3-84f148ed6091',
  '2f5ee7ad-c8e5-4233-90b7-cebb4d0a4a1d',
  'e853bdbc-c08f-4af7-acef-c50ac0f2c15e',
  'eabd4dd6-b98e-4c80-9ea1-f40a035a5d02',
  'df02cab2-6667-436a-b0af-692105aba1c8',
  'b90976a3-5645-4de1-8220-7c33eebcaf45'
) AND status = 'published';

-- 3) 搜索别名（同品异名；商品名不变，只加搜索命中词）。
UPDATE product SET search_aliases = ARRAY['青菜','小油菜','小白菜'], updated_at = NOW() WHERE slug = '上海青';
UPDATE product SET search_aliases = ARRAY['蒜头','蒜'], updated_at = NOW() WHERE slug = 'store-veg-027';
UPDATE product SET search_aliases = ARRAY['番茄'], updated_at = NOW() WHERE title = '西红柿';
UPDATE product SET search_aliases = ARRAY['青瓜'], updated_at = NOW() WHERE title = '黄瓜';
UPDATE product SET search_aliases = ARRAY['马铃薯','洋芋'], updated_at = NOW() WHERE title = '土豆' AND slug = '土豆';
UPDATE product SET search_aliases = ARRAY['苹果'], updated_at = NOW() WHERE title = '红富士苹果';
UPDATE product SET search_aliases = ARRAY['橙子','脐橙'], updated_at = NOW() WHERE title = '赣南脐橙';
UPDATE product SET search_aliases = ARRAY['葡萄'], updated_at = NOW() WHERE title = '巨峰葡萄';
UPDATE product SET search_aliases = ARRAY['西瓜'], updated_at = NOW() WHERE title = '麒麟西瓜';
UPDATE product SET search_aliases = ARRAY['五花肉'], updated_at = NOW() WHERE title = '猪五花肉';
UPDATE product SET search_aliases = ARRAY['鸡胸肉'], updated_at = NOW() WHERE title = '冷鲜鸡胸肉';
UPDATE product SET search_aliases = ARRAY['活虾','虾'], updated_at = NOW() WHERE title = '基围虾';
UPDATE product SET search_aliases = ARRAY['普通鸡蛋','鸡蛋'], updated_at = NOW() WHERE title = '鲜鸡蛋';
UPDATE product SET search_aliases = ARRAY['大米'], updated_at = NOW() WHERE title = '东北大米';
UPDATE product SET search_aliases = ARRAY['生抽','酱油'], updated_at = NOW() WHERE title = '生抽酱油';
UPDATE product SET search_aliases = ARRAY['食盐','盐'], updated_at = NOW() WHERE title = '食用盐';
UPDATE product SET search_aliases = ARRAY['橘子','桔子'], updated_at = NOW() WHERE title = '柑橘';
UPDATE product SET search_aliases = ARRAY['文旦'], updated_at = NOW() WHERE title = '柚子';
UPDATE product SET search_aliases = ARRAY['雪梨'], updated_at = NOW() WHERE title = '梨';
UPDATE product SET search_aliases = ARRAY['奇异果'], updated_at = NOW() WHERE title = '猕猴桃';
UPDATE product SET search_aliases = ARRAY['苞米','粟米'], updated_at = NOW() WHERE title = '玉米' AND slug = 'store-veg-021';
UPDATE product SET search_aliases = ARRAY['姜'], updated_at = NOW() WHERE title = '生姜';
UPDATE product SET search_aliases = ARRAY['葱','香葱'], updated_at = NOW() WHERE title = '小葱';
UPDATE product SET search_aliases = ARRAY['蘑菇'], updated_at = NOW() WHERE title = '平菇';
UPDATE product SET search_aliases = ARRAY['鸡腿菇'], updated_at = NOW() WHERE title = '杏鲍菇';
UPDATE product SET search_aliases = ARRAY['金菇'], updated_at = NOW() WHERE title = '金针菇';
UPDATE product SET search_aliases = ARRAY['前腿猪肉'], updated_at = NOW() WHERE title = '前腿肉';
UPDATE product SET search_aliases = ARRAY['后腿肉','瘦肉'], updated_at = NOW() WHERE title = '后腿瘦肉';
UPDATE product SET search_aliases = ARRAY['猪排骨'], updated_at = NOW() WHERE title = '排骨';
UPDATE product SET search_aliases = ARRAY['猪蹄'], updated_at = NOW() WHERE title = '猪脚';
UPDATE product SET search_aliases = ARRAY['猪耳'], updated_at = NOW() WHERE title = '猪耳朵';
UPDATE product SET search_aliases = ARRAY['鸡','土鸡'], updated_at = NOW() WHERE title = '整鸡';
UPDATE product SET search_aliases = ARRAY['面条'], updated_at = NOW() WHERE title = '挂面';
UPDATE product SET search_aliases = ARRAY['小麦粉'], updated_at = NOW() WHERE title = '面粉';
UPDATE product SET search_aliases = ARRAY['生粉'], updated_at = NOW() WHERE title = '淀粉';
UPDATE product SET search_aliases = ARRAY['粟粉'], updated_at = NOW() WHERE title = '玉米淀粉';
UPDATE product SET search_aliases = ARRAY['白砂糖','糖'], updated_at = NOW() WHERE title = '白糖';
UPDATE product SET search_aliases = ARRAY['红砂糖'], updated_at = NOW() WHERE title = '红糖';

-- 4) 13+ 个 core-*/smoke 测试账号：停用并从报表/顾客统计排除（不删除）。
UPDATE customer
SET status = 'disabled', excluded_from_reports = TRUE, updated_at = NOW()
WHERE deleted_at IS NULL
  AND (email LIKE 'core-%@example.invalid'
       OR email LIKE '%smoke-%@example.invalid');

COMMIT;
