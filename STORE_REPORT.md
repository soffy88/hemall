# Hemall 门店第一阶段：SKU 补全 + 称重定价报告

- 工作树：`/data/soffy/wt/hemal-store`
- 分支：`feat/store-phase1`（基线 `fix/launch-p0@538d448`）
- 代码提交：`3371c92`（默认值/品类可见性）、`c3b16fa`（称重定价）、`0a2a146`（99 SKU + 脚本）
- 边界：未重启/替换线上容器，未改线上 compose/Caddy，未写生产库，未支付/发消息；临时容器 `hemal-st-pg`、`hemal-st-redis`、`hemal-st-web` 已清理。

## 1. 先修正上一轮默认值（与用户 2026-11-08 决定对齐）

| 项 | 旧行为 | 新行为 |
| --- | --- | --- |
| 酒类 | `alcohol_sales_enabled=False`，默认隐藏 | 默认 **true**，线上保留上架；年龄确认、未成年人禁售、理性饮酒提示全部保留 |
| 商品资料门禁 | 缺保质期/产地/储存条件即前台隐藏 | 默认 **不隐藏**；仅隐藏明确测试商品与质检 failed 批次；后台 `needs_data` 标「待补资料」 |
| 严格门禁 | 硬编码 | 新配置 `catalog_strict_data_gate`（`HEMALL_CATALOG_STRICT_DATA_GATE`），默认 **false**，可一键恢复旧行为 |
| 线上品类 | 前台只显示 5 类，酒水/零食/冷冻等被前端过滤 | `VISIBLE_CATEGORY_SLUGS` 扩到 11 类，线上现有品类保留展示，不删除、不下架 |

关键位置：`app/config.py`、`app/queries.py`、`web/src/lib/storefront-assortment.ts`；回归测试 `tests/test_p0_alcohol_gate.py`、`tests/test_p0_catalog_gate_db.py`。

## 2. 称重定价（已完成主链路）

### 数据模型（`app/weigh_price.py`，启动时幂等 DDL）
- `product_variant`：`pricing_mode`（piece/weight）、`unit_price_cents`（分/斤）、`price_basis_grams`（默认 500）、`weight_options`、`stock_unit`（piece/gram）。
- `cart_line_item`：计价方式、分/斤单价、预估重量、重量档、计价基准；唯一键改为 `(cart_id,batch_id,weight_step_grams)`，同批次不同档可并存。
- `order_line_item`：预估/实称重量、预估/实际金额、差额、结算状态、分/斤单价、计价基准。
- `weigh_settlement`：每订单行唯一结算单（实称克数、差额、容差、退款单号、状态）。
- `store_daily_inventory`：每日进货量/销售量/剩余量/报损量/进价/售价/毛利结构。

### 计价与结算规则
- 前台统一展示 **元/斤**；内部 **克 + 分**。金额 = 克 × 分/斤 ÷ 500，`Decimal` **ROUND_HALF_UP** 到分（`tests/test_weigh_price.py` 固定规则）。
- 下单按预估重量与预估金额；结算按实称重量。差额在 `weigh_diff_tolerance_percent`（默认 10%）内自动多退少补；超出容差进入 `needs_confirmation`，顾客接受后才处理，拒绝转人工争议。
- 少收（需退款）走既有 `refund_service`：gateway 模式退款成功；`manual` 模式停在 `refunding`，记录「线下确认」待人工处理。
- 多收（需补收）：未接预授权时只记 `charge_pending`，商家在商家端确认线下补收；**不伪造二次扣款**。
- 库存：称重商品 `inventory_batch.stock_qty/reserved_qty` 以 **克** 计，结算按克预留；按件商品完全兼容旧逻辑。

### 接口与界面
- 商家端：`GET /store/merchant/weigh-settlements`、`POST /store/merchant/weigh-settlements`（录实称）、`POST .../{id}/confirm-charge`、`GET .../{id}/slip`（称重单文本）。
- 顾客端：`GET /store/orders/{order_id}/weigh-settlements`、`POST .../{settlement_id}/confirm`。
- 前台：商品列表/详情显示 `¥x/斤`、预估档位、预估价与「称重后多退少补」；购物车/订单/商家页显示预估与实际、差额与状态。

## 3. SKU 补全：99 + 73 合并去重

- 交付文件：`store-sku-pricing.csv`（99 行，UTF-8 BOM，Excel 可直接打开）。
- 实际核对：73 清单原表为 **74 项**（原描述“约 73”）；99 清单新增 **25 项**，并集 **99**，同品只建一个。`来源` 列：`两者` 74 / `99` 25。
- 同品异名合并：青菜→上海青、蒜头→大蒜、鲜鸡→整鸡、鸭腿→鸭翅、面条→鲜湿面条、千张/豆皮→千张豆皮、西红柿→番茄、马铃薯→土豆、卷心菜→包菜、蛤蜊→花甲（CSV 备注保留两个来源）。
- 品类映射：蔬菜/水果/水产/粮油沿用旧类目；新增 `pork`（猪肉）、`poultry`（禽肉）、`eggs-tofu`（蛋及豆制品），**不删旧类目**。
- 计价方式：蔬果肉禽鱼、豆干、千张、散装面条等按重量；大米 5kg、油 5L、瓶装调味、豆腐盒、火龙果、西瓜、柚子、猕猴桃、预包装糖/面粉等按件。
- 预算沿用 99 表：蔬菜 1700 / 水果 700 / 猪肉 1700 / 禽肉 800 / 水产 900 / 蛋豆 600 / 粮油调味 1000 = **7400**，另 600 周转金，总上限 8000。
- 价格：CSV 进价/售价全部留空，**没有编造任何报价**；图片列统一「待拍照」，不使用网络图片。
- 导入脚本 `scripts/import_store_sku_pricing.py`：默认 dry-run；`--apply` 需显式 DSN，DSN 含 `prod` 默认拒绝；按 slug/SKU 幂等；同名同品复用现有商品（保留实拍图/批次）；无售价商品保持 `draft`，只有填价且 `--publish` 才发布；不建批次。
- 辅助报表：`scripts/store_daily_inventory_report.py`（每日进销存 CSV）、`scripts/store_data_completion_report.py`（待补资料清单）。

## 4. 验证

| 项 | 结果 | 证据 |
| --- | --- | --- |
| 后端全量（临时 PG + Redis） | **2224 passed, 37 skipped**（1 个 release 权限用例修复后 6 passed） | `/tmp/store_full_pytest.log` |
| 称重单元 | 16 passed（金额四舍五入、kg→斤、容差边界、档位解析） | `tests/test_weigh_price.py` |
| 称重 DB 集成 | 3 passed：下单按克预留、实称差额退款、超容差需顾客确认、幂等 | `tests/test_weigh_settlement_db.py` |
| 商品门禁 DB | 通过；默认非严格可见、`strict_data_gate=True` 隐藏 | `tests/test_p0_catalog_gate_db.py` |
| 前端类型检查 | `tsc --noEmit` 通过 | `/tmp/store_fe_build.log` |
| 前端单测 | 9 files / **135 tests passed** | 同日志 |
| 前端构建 | `next build` 成功（Next.js 16.4.5） | `/tmp/store_fe_build.log` |
| 后端 lint/类型 | `ruff check/format` 通过；新模块 mypy 无新增错误 | 命令输出 |
| SKU CSV | 99 行、SKU 唯一、价格空、预算合计 7400 | `tests/test_store_sku_csv.py` |
| 导入脚本 | 临时库首次 99 商品/99 规格、二次运行 0 新建 99 复用；99 条均 draft | 临时 `hemall_test` 查询 |
| UI 截图（浏览器拦截 API，真实构建产物） | 商品列表/详情/购物车 | `../hemal-store-run/evidence/store-shop-weight.png`、`store-product-weight.png`、`store-cart-weight.png` |

截图说明：使用 `next start` 加载本分支构建产物，Playwright 拦截 `/api/store/*` 返回称重商品样例；页面显示 `¥3.98/斤`、预估档位、预估金额、称重后多退少补说明。生产后端未参与截图。

## 5. 未完成 / 需用户

1. **真实进价/售价/保质期/产地/储存条件/供应商/实拍图**：CSV 留空，需用户或采购填写后再导入并上架。未填价商品不会出现在前台。
2. **预授权/二次扣款**：当前多收部分只记 `charge_pending`，由商家线下补收确认；接入微信/支付宝预授权后可改为自动补扣。
3. **61 个实拍商品去重**：脚本按“同名/别名”匹配现有商品，实际合并结果需人工复核（尤其青菜/上海青、蒜头/大蒜等）。
4. **称重单打印**：目前返回可展示/打印的文本称重单，未对接小票打印机/电子秤硬件。
5. **每日进销存报表**：表结构与只读 CSV 脚本已就绪；尚未做后台页面与定时任务，毛利按进货均价估算，需财务口径复核。
6. **图片**：全部“待拍照”，未使用网络图片；需门店实拍后按 SKU 上传。
7. 数据变更脚本仅需在临时环境演练；生产导入需用户确认并显式提供 DSN（脚本默认拒绝 prod）。
