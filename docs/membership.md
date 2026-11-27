# 会员补齐（缺口 4）：等级 + 积分 + 储值

状态：已实现（后端），未部署。规则依据：`~/workspace/hemal-cangzhu-rules.md`
（规则 2 合资格口径）。以下默认值均为 Soffy 可否决的授权默认值，改动须他拍板。

## 口径总表

| 机制 | 口径 | 关键约束 |
| --- | --- | --- |
| 等级 | 累计合资格净额（规则 2 口径，退款冲回后扣减）：0 元普通、满 500 元银卡、满 2000 元金卡 | 只作身份与统计，不给价格折扣；折扣仍只有会员 9 折，互斥规则一字不动 |
| 积分 | 合资格金额每满 1 元积 1 分，不足 1 元不计；买家本人得积分 | 独立账本 `points_ledger`；永不过期、不可转让、无现金价值；本批只积不兑 |
| 储值 | 商家记账式充值，必填外部支付凭证号，只记账 | 独立账本 `stored_value_ledger`；不可提现、不可转让；不调用任何支付网关 |
| 会费 | 199 元/人，一户一行（`membership_fee`，状态 unpaid/paid/waived） | 只记账；不缴费不影响推荐奖励与下单，不设门禁；减免须逐人记、必须带原因 |

配置常量集中在 `app/membership.py` 顶部（`POINTS_CENTS_PER_POINT`、
`TIER_THRESHOLDS`、`TIER_LABELS`），不写死在 SQL 里。

## 合资格口径（与券账本同一函数）

复用 `app.cangzhu.compute_eligible_base`：非排除商品行合计（`product.voucher_excluded`
标记特价/清仓/礼品卡/限促等）减券抵扣、减运费、减已成功退款，下限 0。
夜市价商品尚未接入，接入时须同步标记排除（与券账本同一待办）。
积分只在订单 `completed` 后结算；未完成、取消的订单不产生积分。

## 账本模式（仿 `voucher_ledger`，绝不复用）

- `points_ledger`：append-only，`kind` 为 `accrual`/`reversal`，`status` 恒为
  `posted`（本批无持有/冻结态）；`idempotency_key` 唯一，重复结算/冲回不重复记账。
  余额缓存列 `customer.points_balance` 与账本同事务维护；`balance_after` 回填。
- `stored_value_ledger`：append-only，`kind` 为 `topup`（充值）或 `spend`
  （柜台结算扣款，幂等键 `stored-spend:{ticket_id}`，同一单据不重复扣）；
  `amount_cents` 记变动绝对值，方向看 `kind`。同一顾客下
  `(customer_id, external_payment_ref)` 唯一，同一凭证号重复提交只返回已记账。
  余额缓存列 `customer.stored_value_balance_cents` 与账本同事务维护。
- 等级缓存：`customer.member_tier` 与 `customer.lifetime_eligible_cents`
  在积分累积/冲回的同一事务内更新；等级由 `tier_for_lifetime` 纯函数推导。
- 绝不复用 `customer.system_balance`（SLA/UGC 算力金）与 `voucher_ledger`（购物券）。
- `membership` 表是付费订阅（`process_subscription` 收算力费、给进场权限），
  与消费等级 `member_tier` 无关；本模块不读不写 `membership` 表。
  `calculate_membership_dividend`（`app/ext/oskill.py`）仍是零调用死代码，本批未动。

## 触发方式（柜台结算已挂钩子）

- 积分累积：柜台结算建单成功后在同一事务内自动累积
  （`app.storefront._create_counter_order` 调 `accrue_points_conn`，幂等键
  `points-accrual:{order_id}`）；散客无顾客关联时跳过，结算回滚时同滚。
  MP2 结算（`POST /admin/scale/settle`）复用同一建单函数，同样累积。
  商家端点 `POST /orders/{id}/points-accrue` 仍保留作补记/修复入口。
- 储值扣款：柜台结算选 `stored_value` 时在同一事务内扣顾客余额
  （`spend_stored_value_conn`，幂等键 `stored-spend:{ticket_id}`）；余额不足
  422，不建单不扣库存；绝不凭空生储值。
- 退款冲回：柜台无退款端点，自动冲回无挂点，不新建退款流程；冲回仍走商家端点
  `POST /orders/{id}/points-reverse` 显式触发（幂等，`refund_id` 唯一）。
储值充值是纯记账：商家在线下或外部渠道收到顾客付款后，凭外部支付凭证号
在系统内记账；系统不发起、不校验任何真实支付。

## 端点（`/store` 前缀）

- `GET /customers/me/membership`：本人总览（等级、积分余额、储值余额、累计合资格净额、等级进度、阈值表）。
- `GET /points`、 `GET /stored-value`：本人账本明细（分页 `limit`/`offset`）。
- `GET /customers/{id}/membership`：商家查询任意顾客总览（`is_merchant` 门禁）。
- `POST /orders/{id}/points-accrue`：商家触发积分结算（幂等，已结算返回 `accrued: false`）。
- `POST /orders/{id}/points-reverse`：商家触发退款冲回（body：`refund_id`、`refund_cents`；幂等，`refund_id` 唯一）。
- `POST /customers/{id}/stored-value/topup`：商家记账充值（body：`amount_cents`、
  `external_payment_ref`、`note` 可选；同一凭证号重复提交返回 `recorded: false`）。
- `POST /customers/{id}/membership-fee`：商家记会费（body：`status` 为 `paid`/`waived`，
  `note`，`amount_cents` 可选，缺省 19900；减免必须带原因，重复记同一状态返回
  `recorded: false`）。
- `GET /customers/me/membership-fee`：本人自查会费状态（未记过返回 `unpaid` 默认态）；
  `GET /customers/{id}/membership-fee`：商家查询任意顾客会费状态。

非商家调商家端点返回 403；未登录返回 401；顾客不存在/订单不存在返回 404；
非法参数（金额非正、凭证号为空、备注超长）返回 400。

## 不兑换、不支付、不短信

- 本批只积不兑：不接 checkout、不碰互斥规则、不发明兑换比例；`points_redeemable`
  在总览中恒为 `false`，`points_expire` 恒为 `null`（永不过期）。
- 不发明支付网关：储值无网关调用、无回调、无对账接口。
- 短信一律不做（blocked）：无短信网关、无凭证；不写发送代码、不接 provider。
  会员变动通知如需短信，待 Soffy 提供网关与凭证后另立批次。

## 会费账本（199 元/人）

- 一户一行：`membership_fee`，`customer_id` 唯一；`amount_cents` 缺省 19900
  （199 元），`status` 为 `unpaid`/`paid`/`waived` 三态，`recorded_by` 为记账
  商家，`note` 记原因，`created_at`/`updated_at`。
- 只记账，不设门禁：不缴费不影响推荐奖励与下单。
- 减免（`waived`）须逐人记、必须带原因；不自动全员 waived。已缴费（`paid`）
  不可再改减免；减免后可补缴（`waived → paid`）；重复记同一状态返回
  `recorded: false`，不重复写。

## DDL

`MEMBERSHIP_DDL` 在 `app/membership.py` 内，`app/p0_schema.py::ensure_p0_schema`
加一行注册调用（带“缺口 4”唯一标记注释），幂等。新增表：`points_ledger`、
`stored_value_ledger`、`membership_fee`；新增列：`customer.points_balance`、
`customer.stored_value_balance_cents`、`customer.lifetime_eligible_cents`、
`customer.member_tier`。
