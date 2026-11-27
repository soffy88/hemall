# 供应商管理（B 部分）

商家端点，须商家身份（与既有商家端点一致：无鉴权 401、非商家 403）。

## 表

> 注意：`supplier` 这个表名已被仓内既有表占用（钱包/空间域，
> `inventory_batch.supplier_id` 外键指向它），故本模块三张表用
> `procurement_` 前缀；端点与字段名仍用 supplier。


- `procurement_supplier`：id / name / contact_name / phone / category（核心品类）/
  license_note（资质/证照备注）/ status（active/inactive）/
  is_primary / is_backup / created_at / updated_at。
  同一品类的主要、备用各唯一：部分唯一索引（仅 active 行）+ 应用层
  校验双保险；同一供应商不能同时是主要和备用。
- `procurement_supplier_price`：id / supplier_id / product_id（UUID，外键
  product(id)，不用 sku；商品须存在）/ price_cents（分）/
  quoted_date（报价日期）/ note / created_at。
- `procurement_supplier_evaluation`：id / supplier_id / week（`YYYY-Www` 字符串，
  如 2026-W47）/ price_score / quality_score / punctuality_score /
  stability_score / aftersales_score / cooperation_score /
  total_score（服务端算，两位小数）/ created_at / updated_at。
  同一供应商每周一条（UNIQUE(supplier_id, week)）。

## 加权总分

价格竞争力 25%、商品质量 30%、到货准时率 15%、供货稳定性 15%、
售后处理 10%、配合程度 5%。六项各 0-100，总分由服务端算，客户端
不传总分（请求模型里没有该字段）。

## 端点（`/store` 前缀）

- POST /suppliers：新建供应商；品类主备冲突 409。
- GET /suppliers?category=&status=：列表。
- GET /suppliers/by-category/{category}：{primary, backup}（缺位为 null）。
- GET /suppliers/{id} / PATCH /suppliers/{id} / DELETE /suppliers/{id}：
  删除有报价或评分记录时 409（应改走停用）。
- POST /suppliers/{id}/prices：录报价；商品不存在 404。
- GET /suppliers/{id}/prices?product_id=：报价列表。
- POST /suppliers/{id}/evaluations：录一周评分；重复周次 409。
- GET /suppliers/{id}/evaluations?week=：评分列表。
- GET /suppliers/{id}/evaluations/{week}：取某一周评分。
- PUT /suppliers/{id}/evaluations/{week}：改分，总分重算。

## Schema

启动时 `ensure_p0_schema` 幂等建表（`app/supplier.py` 的
`SUPPLIER_DDL`）；新表初始 0 行；不写假数据。
