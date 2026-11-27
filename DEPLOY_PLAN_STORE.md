# 门店 SKU + 称重定价上线计划（与 fix/launch-p0 一并）

## 0. 前置

- 代码：`feat/store-phase1`（`3371c92` + `c3b16fa` + `0a2a146`），基线 `fix/launch-p0@538d448`。
- 上线前先完成 `fix/launch-p0` 的 P0 上线检查（DEPLOY_PLAN.md）：备份、镜像、支付开关、告警接收人。
- 本轮 **不改线上 compose/Caddy**，沿用 P0 的发布流程与镜像仓库。
- 生产数据变更必须先导出/备份；本文所有 SQL 只给步骤，不自动执行。

## 1. 迁移顺序

1. **数据库备份**：执行 P0 备份脚本，确认 `pg_dump` 与恢复核验通过（93 表）。
2. **发布新镜像**：后端 + 前端同一 sha；启动时 `ensure_p0_schema` 自动执行幂等 DDL：
   - 商品/购物车/订单行新增称重列；`cart_line_item` 唯一键改为含 `weight_step_grams`；
   - 新建 `weigh_settlement`、`store_daily_inventory`；
   - 均为 `ADD COLUMN IF NOT EXISTS` / `CREATE TABLE IF NOT EXISTS`，对旧数据向后兼容（旧行默认 `piece` / 500 克基准）。
3. **先只切流量，不导入新 SKU**：验证旧商品、按件下单、支付、退款、称重旧柜台流程正常。
4. **SKU 导入（可选、需用户确认）**：
   - 用户填写 `store-sku-pricing.csv` 的售价（元/单位）、进价、保质期、产地、储存条件、供应商。
   - 在预发/临时库执行：`HEMALL_PG_DSN=<staging> python scripts/import_store_sku_pricing.py --apply`。
   - 校验：商品数为 99、全部 `draft`、无售价为空的行；再对生产执行同一命令。
   - 填价并确认后加 `--publish` 才会发布；未填价商品保持草稿，前台不可见。
   - 导入脚本默认拒绝库名含 `prod` 的 DSN；生产执行需显式 `HEMALL_IMPORT_ALLOW_PROD=1`（仅限人工确认后）。
5. **称重商品逐项启用**：后台把对应变体设为 `pricing_mode=weight`、`unit_price_cents`（分/斤）、`price_basis_grams=500`、`weight_options`，并建立库存批次（`stock_qty` 单位为克）。
6. **冒烟**：见第 3 节。
7. **观察**：首日重点看称重订单的预估/实称差额、退款状态、`charge_pending` 数量、库存克数与报损。

## 2. 回滚

- **应用回滚**：按 P0 原生流程回滚到上一个 sha 镜像。新增列/表对旧镜像无影响（旧代码不 SELECT 新列），旧镜像仍按 `(cart_id,batch_id)` 使用现有数据；回滚前先停止新的称重下单。
- **唯一索引**：新索引 `uq_line_item_cart_batch_weight` 对按件行等价于旧键，旧镜像可继续读写；如必须完全恢复旧索引：
  ```sql
  DROP INDEX IF EXISTS uq_line_item_cart_batch_weight;
  CREATE UNIQUE INDEX uq_line_item_cart_batch
    ON cart_line_item(cart_id, batch_id) WHERE deleted_at IS NULL;
  ```
  仅在确认没有同批次多重量档的活跃购物车行时执行。
- **数据回滚（仅本次导入的草稿）**：
  ```sql
  -- 先核对：只删除 slug 以 store- 开头、无批次、无订单引用的草稿商品
  SELECT p.id, p.title FROM product p
  WHERE p.slug LIKE 'store-%' AND p.status = 'draft'
    AND NOT EXISTS (SELECT 1 FROM inventory_batch ib
                    JOIN product_variant pv ON pv.id = ib.variant_id
                    WHERE pv.product_id = p.id);
  -- 确认后按 id 删除 product_variant → product；分类可保留（新增类目不影响旧数据）
  ```
- **功能开关回滚**：`HEMALL_CATALOG_STRICT_DATA_GATE=true` 可恢复资料不完整即隐藏；`alcohol_sales_enabled=false` 可临时下架酒类（年龄门禁仍在）。
- **称重回滚**：把相关变体 `pricing_mode` 改回 `piece` 前，必须结清所有 `pending/needs_confirmation/charge_pending` 的 `weigh_settlement`。

## 3. 上线冒烟清单

1. `/health/ready` 200，数据库/Redis up。
2. 商品列表：酒水、零食、冷冻等线上类目仍可见；缺资料商品未被隐藏（除非显式开严格门禁）。
3. 酒类：默认可见；未年龄确认时加购被拦；确认后可见年龄提示。
4. 称重商品详情：`¥x/斤`、档位、预估金额、说明齐全；加购后购物车显示预估重量/金额。
5. 下单：订单行 `pricing_mode=weight`、`estimated_weight_grams`、`estimated_amount_cents` 正确；批次 `reserved_qty` 按克增加。
6. 商家端录实称：差额在 10% 内自动退款/待补收；manual 退款为 `refunding` 待线下确认；超 10% 为 `needs_confirmation`。
7. 顾客确认/拒绝接口可用；称重单文本可打印。
8. 按件商品下单、退款、库存完全不受影响。
9. `store_daily_inventory_report.py` 可生成当日 CSV；`store_data_completion_report.py` 能列出待补资料。

## 4. 回滚演练建议

在临时环境把镜像从旧 sha → 新 sha → 旧 sha 各跑一次冒烟第 1、2、5、8 项；确认旧镜像在新增列存在时可正常下单/退款后再上生产。
