# 分拣交付表 + 损耗表（V1.0 落地任务 C）

商家端点，须商家身份（与既有商家端点一致：无鉴权 401、非商家 403）。

## 表

- `sorting_delivery`：id / order_id（UUID，外键 customer_order(id)，一单一表头，
  UNIQUE）/ delivery_method（pickup 自提 / delivery 配送）/ status /
  operator_id（建单商家）/ note / created_at / updated_at。
  状态机：pending（待分拣）-> sorting（分拣中）-> ready_pickup（待提货，
  限自提）或 out_for_delivery（待配送，限配送）-> completed（已完成）；
  进行中任何状态可进 exception（异常），异常只能回 sorting 返工；
  completed 为终态。非法流转 409（应用层校验，见 `app/sorting.py`
  的 `ALLOWED_TRANSITIONS`）。
- `sorting_delivery_line`：id / sorting_id（外键，级联删除）/ product_id
  （UUID，外键 product(id)，商品须存在）/ expected_qty（应配数量，> 0）/
  actual_qty（实配数量，>= 0，缺省 0）/ reviewer（复核人）/ note /
  created_at / updated_at。一单一品一行（UNIQUE(sorting_id, product_id)）。
  加行与复核只允许在 pending / sorting 状态下做。
- `spoilage_record`：id / product_id（UUID，外键 product(id)，商品须存在）/
  sku（必填）/ batch_no（批次，缺省空）/ quantity（数量，> 0）/
  cost_cents（成本金额，分，>= 0）/ reason（闭合词表：expired 过期 /
  spoiled 变质 / damaged 破损 / weigh_error 称重误差 / other 其他）/
  handling（处理结果，自由文本）/ recorded_by（记录人，商家 id）/
  recorded_date（损耗日期，缺省当天）/ created_at。
  只记账，不碰库存：损耗不自动扣库存，不写任何 stock / inventory 表，
  现有库存口径不动。

## 复核规则

改实配数量（actual_qty）须留复核人：本次请求传 reviewer，或行上已有
reviewer；两者皆无时 400。只改备注不用复核人。

## 端点（`/store` 前缀）

- POST /sorting-deliveries：按订单建分拣单；订单不存在 404，同一订单
  已有分拣单 409，交付方式非法 400。
- GET /sorting-deliveries?status=&delivery_method=：列表。
- GET /sorting-deliveries/{id}：表头 + 行明细（lines）。
- PATCH /sorting-deliveries/{id}：{status} 流转（非法流转 409）或 {note}
  改备注（不碰状态）。
- POST /sorting-deliveries/{id}/lines：加行；商品不存在 404，同一品
  重复加行 409，非待分拣/分拣中状态 409。
- GET /sorting-deliveries/{id}/lines：行明细列表。
- PATCH /sorting-deliveries/{id}/lines/{line_id}：复核（见上）。
- POST /spoilage：记一笔损耗；原因不在词表内 400，商品不存在 404。
- GET /spoilage?recorded_date=&reason=&product_id=：损耗列表。
- GET /spoilage/daily-summary?date=：每日汇总 {record_count,
  total_quantity, total_cost_cents, by_category[]}；按品类统计取
  product.category_id -> product_category.name，无品类记“未分类”。
  （daily-summary 路由须在 /spoilage/{id} 之前注册。）
- GET /spoilage/{id}：取一笔损耗记录。

## Schema

启动时 `ensure_p0_schema` 幂等建表（`app/sorting.py` 的
`SORTING_DDL`）；新表初始 0 行；不写假数据。
