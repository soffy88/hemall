-- 回滚 20261110 core2 数据复核：恢复被转草稿的商品、清空别名、恢复测试账号。
-- 注意：若这些商品期间被人工改过状态，本脚本会覆盖为 published。
BEGIN;
UPDATE product SET status = 'published', updated_at = NOW()
WHERE id IN (
  '73f98b4e-7cc0-4ec2-8a9e-a2050b2ca79b','b0c95ebb-7adc-4159-bf4f-5dea67b02e52',
  '92dc08d9-634f-44cf-8add-c4cfcae4b49e','019fc6b1-851e-74d7-a52b-92f75aa58282',
  '2558e726-5a0f-4b24-a2be-1f4779aa20fc','dc6bac99-3241-47d8-ac74-66cb05380813',
  'aa912788-0ff7-4c44-ab4a-11d6773ed263','4c70d1ef-750a-4418-9039-fbbf02b710f9',
  '8b5b773e-2349-4045-a3fa-1c1dc4894117','248e23f4-aaa3-4007-b7d3-5fb859dd4562',
  '9cc2178b-8327-45f3-b328-8ceb213b1dcc','a6169895-c8c9-4adc-b74f-cd763a300baf',
  'cdcc5975-da08-430b-9a44-65130595c4ae','2c52d693-2c48-4a78-9515-84eb35b943d3',
  '2f5e5d6a-fe16-4452-8cbe-285e8d7ede9a','e77dc9c9-db83-487c-83c3-84f148ed6091',
  '2f5ee7ad-c8e5-4233-90b7-cebb4d0a4a1d','e853bdbc-c08f-4af7-acef-c50ac0f2c15e',
  'eabd4dd6-b98e-4c80-9ea1-f40a035a5d02','df02cab2-6667-436a-b0af-692105aba1c8',
  'b90976a3-5645-4de1-8220-7c33eebcaf45'
);
UPDATE product SET search_aliases = '{}', updated_at = NOW()
WHERE search_aliases <> '{}';
UPDATE customer SET status = 'active', excluded_from_reports = FALSE, updated_at = NOW()
WHERE excluded_from_reports = TRUE
  AND (email LIKE 'core-%@example.invalid' OR email LIKE '%smoke-%@example.invalid');
COMMIT;
