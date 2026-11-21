"""omodul 写操作的 op → 最小角色映射（集中一张表）。

为什么需要这张表
----------------
`require_auth=True` 只回答"是不是员工"，不回答"这个员工有没有资格做这件事"。
于是下面这些全部畅通无阻：

* ``/agent/command``（``Depends(get_current_user)``，任意员工）能靠一句话触发
  ``退款`` → ``aftersales/refund_payment``、``结算`` → ``supply-chain/batch_settlement``、
  ``广播`` → ``marketing/execute_channel_broadcast_workflow``、``佣金`` →
  ``settlement/dispatch_host_dividend``；而功能等价的 ``/agent/execute`` 却要求
  admin 角色。同一个能力，两个入口，两套权限。
* ``respond.py`` 的通用 omodul 写端点工厂对所有 op 一视同仁。

策略（默认拒绝，不默认放行）
----------------------------
1. **显式登记表**：``OP_MIN_ROLES`` 逐条登记每个高危 op 的最低角色集合。
   同一张表被 ``/agent/execute``（``agent_gateway.execute_tool``）与 HTTP 写端点
   （``respond.omodul_endpoint``）共用，两条入口不可能漂移。
2. **模式兜底（fail-closed）**：op 名里出现钱/凭证/外部通信关键词、却**没有**在
   登记表里出现的，收紧到 ``{admin}`` 并告警——新增 omodul 时忘记分类，会被自动
   收严而不是被自动放行。
3. **其余 op**：``DEFAULT_OP_ROLES``（admin/support/staff 都可）。这些是购物车、
   商品目录、购物车地址这类日常运营操作，不涉及钱出账与凭证。

登记表的键是 ``"domain/name"``；同名 omodul 出现在多个域时（例如
``create_inventory_batch`` 同属 ``inventory`` 与 ``supply-chain``），两个键都要登记，
查询按 **完整键 → 裸 name → 兜底** 三级解析。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from fastapi import HTTPException

from .tokens import TokenTypeError  # noqa: F401  (保持与鉴权子模块的显式关联)

logger = logging.getLogger("hemall.security.op_roles")

ADMIN_ROLE = "admin"
SUPPORT_ROLE = "support"
STAFF_ROLE = "staff"

#: 未被分类为高危的 op：任何员工角色都可执行（购物车、商品目录、地址簿等日常运营）。
DEFAULT_OP_ROLES: frozenset[str] = frozenset({ADMIN_ROLE, SUPPORT_ROLE, STAFF_ROLE})

#: 高危 op 登记表：op → 允许的角色集合。白名单，角色不足 403。
OP_MIN_ROLES: dict[str, frozenset[str]] = {
    # ── 钱出账 / 结算 / 分润（admin 专属）────────────────────────────────
    "aftersales/refund_payment": frozenset({ADMIN_ROLE}),
    "aftersales/capture_payment": frozenset({ADMIN_ROLE}),
    "aftersales/process_swap_payment": frozenset({ADMIN_ROLE}),
    "checkout/authorize_payment_for_cart": frozenset({ADMIN_ROLE}),
    "checkout/mark_draft_order_paid": frozenset({ADMIN_ROLE}),
    "cart/create_payment_sessions": frozenset({ADMIN_ROLE}),
    "cart/set_payment_session": frozenset({ADMIN_ROLE}),
    "cart/update_payment_sessions": frozenset({ADMIN_ROLE}),
    "supply-chain/batch_settlement": frozenset({ADMIN_ROLE}),
    "settlement/dispatch_host_dividend": frozenset({ADMIN_ROLE}),
    "settlement/dispatch_labor_payment": frozenset({ADMIN_ROLE}),
    "growth/bind_digital_lord_contract_workflow": frozenset({ADMIN_ROLE}),
    "growth/record_douyin_conversion_workflow": frozenset({ADMIN_ROLE}),
    # 押金退还同样是钱出账（顾客自助入口不受本表约束，见 OP_SELF_SERVICE）。
    "aftersales/tote_deposit_and_refund": frozenset({ADMIN_ROLE}),
    # 购物车里的礼品卡 = 可兑付凭证，抵扣/移除同样要 admin。
    "cart/apply_gift_card_to_cart": frozenset({ADMIN_ROLE}),
    "cart/remove_gift_card_from_cart": frozenset({ADMIN_ROLE}),
    # ── 凭证 / 账号（admin 专属）────────────────────────────────────────
    "customers/create_user": frozenset({ADMIN_ROLE}),
    "customers/update_user": frozenset({ADMIN_ROLE}),
    "customers/reset_user_password": frozenset({ADMIN_ROLE}),
    # 礼品卡 = 可兑付凭证，读写都收严到 admin。
    "marketing/create_gift_card": frozenset({ADMIN_ROLE}),
    "marketing/update_gift_card": frozenset({ADMIN_ROLE}),
    "marketing/delete_gift_card": frozenset({ADMIN_ROLE}),
    # ── 对外通信 / 广播（admin 专属）────────────────────────────────────
    "marketing/execute_channel_broadcast_workflow": frozenset({ADMIN_ROLE}),
    # ── 破坏性库存动作（admin 专属）────────────────────────────────────
    "supply-chain/scrap_batch_inventory": frozenset({ADMIN_ROLE}),
    "supply-chain/mark_batch_for_disposal": frozenset({ADMIN_ROLE}),
    "supply-chain/execute_slashing_workflow": frozenset({ADMIN_ROLE}),
    "supply-chain/create_inventory_batch": frozenset({ADMIN_ROLE}),
    "inventory/create_inventory_batch": frozenset({ADMIN_ROLE}),
    # ── 调价 / 定价（admin + support）──────────────────────────────────
    "inventory/add_prices_to_list": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "inventory/remove_prices_from_list": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "inventory/create_price_list": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "inventory/update_price_list": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "inventory/delete_price_list": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "marketing/trigger_initial_probe_workflow": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    # ── 折扣（直接让利，support 也可，但普通 staff 不行）───────────────
    "marketing/create_discount": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "marketing/update_discount": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "marketing/delete_discount": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "marketing/create_discount_rule": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "marketing/update_discount_rule": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "marketing/delete_discount_rule": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "marketing/create_discount_condition": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "marketing/delete_discount_condition": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    # 购物车里直接抵扣折扣 = 直接让利，同样不能由普通 staff 施加。
    "cart/apply_discount_to_cart": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "cart/remove_discount_from_cart": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    # ── 顾客资料 / 地址（admin + support）──────────────────────────────
    "customers/update_customer": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "customers/assign_customer_to_group": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "customers/create_customer_group": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "customers/add_customer_address": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "customers/update_customer_address": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "customers/delete_customer_address": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    # ── 订单状态机（admin + support）──────────────────────────────────
    "checkout/complete_checkout": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "checkout/update_order": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "checkout/cancel_order": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "checkout/archive_order": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "aftersales/cancel_claim": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "aftersales/cancel_return": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "aftersales/cancel_swap": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "aftersales/fulfill_claim": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "aftersales/fulfill_swap": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "aftersales/receive_return": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "aftersales/create_claim": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "aftersales/create_return_request": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "aftersales/create_swap": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "aftersales/execute_liability_routing_workflow": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    # ── 库存调账（admin + support）──────────────────────────────────
    "inventory/adjust_inventory_level": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "inventory/confirm_batch_pick": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "inventory/report_phantom_stock_workflow": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "fulfillment/report_phantom_stock_workflow": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    # ── 目录删除 / 渠道变更（admin + support）──────────────────────────
    "catalog/delete_product": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "catalog/delete_product_variant": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "catalog/delete_product_option": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "catalog/delete_product_category": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "catalog/delete_product_collection": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "inventory/delete_stock_location": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "inventory/delete_sales_channel": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "inventory/publish_products_to_channel": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "inventory/unpublish_products_from_channel": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "settings/create_tax_rate": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "settings/update_tax_rate": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "settings/delete_tax_rate": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "settings/create_region": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "settings/update_region": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "settings/delete_region": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
}

#: 顾客/供应商自助入口（require_auth=False），不走角色表——它们的授权在各自
#: omodul 内部按主体判定（如"订单必须属于本顾客"）。
OP_SELF_SERVICE: frozenset[str] = frozenset(
    {
        "customers/create_customer",
        "customers/create_user",
        "community/create_crowd_intent",
        "store/submit_battle_report_workflow",
        "membership/process_subscription",
        "supply-chain/submit_supplier_reverse_auction_workflow",
        "supply-chain/claim_origin_workflow",
        "aftersales/process_credit_gated_rma_workflow",
        "aftersales/process_drop_return",
        "aftersales/tote_deposit_and_refund",
        "growth/process_cloud_franchise_claim_workflow",
        "marketing/generate_crushing_offer_workflow",
        "inventory/execute_ambient_replenishment",
        "fulfillment/execute_peer_delivery_workflow",
    }
)

#: op 名里出现这些关键词却没登记在 OP_MIN_ROLES 里 → 视为未分类高危 op，
#: 自动收严到 admin 并告警（fail-closed 兜底）。
_HIGH_RISK_NAME_MARKERS: tuple[str, ...] = (
    "refund",
    "payout",
    "dividend",
    "payment",
    "settlement",
    "scrap",
    "slashing",
    "password",
    "broadcast",
    "gift_card",
    "adjust_inventory",
    "price",
    "labor",
    "contract",
    "conversion",
    "create_user",
    "update_user",
    "discount",
    "inventory_batch",
    "disposal",
    "mark_draft_order_paid",
    "archive_order",
    "cancel_order",
    "bind_digital_lord",
)

#: 自然语言命令 (``/agent/command``) 的意图 → 允许角色。
#:
#: 编排器里 ``adjust_price`` 分支是**直接 UPDATE SQL 改批次零售价**，根本不经过
#: omodul，也就绕开了 execute_tool 的 op 表——只按 op 表收口会漏掉这一条路径。
#: 因此命令层需要自己一张按"意图"的表，在 execute_command 入口先卡一次。
COMMAND_KIND_MIN_ROLES: dict[str, frozenset[str]] = {
    "list_product": frozenset({ADMIN_ROLE, SUPPORT_ROLE, STAFF_ROLE}),
    "restock": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "adjust_price": frozenset({ADMIN_ROLE, SUPPORT_ROLE}),
    "dispose": frozenset({ADMIN_ROLE}),
    "settle": frozenset({ADMIN_ROLE}),
    "dividend": frozenset({ADMIN_ROLE}),
    "labor": frozenset({ADMIN_ROLE}),
    "crowd_intent": frozenset({ADMIN_ROLE, SUPPORT_ROLE, STAFF_ROLE}),
    "refund": frozenset({ADMIN_ROLE}),
    "broadcast": frozenset({ADMIN_ROLE}),
    "benchmark": frozenset({ADMIN_ROLE, SUPPORT_ROLE, STAFF_ROLE}),
    "rma": frozenset({ADMIN_ROLE, SUPPORT_ROLE, STAFF_ROLE}),
}

_UNCLASSIFIED_HIGH_RISK_ROLES: frozenset[str] = frozenset({ADMIN_ROLE})


def bare_name(op: str) -> str:
    """``"aftersales/refund_payment"`` → ``"refund_payment"``。"""
    return op.rsplit("/", 1)[-1]


def looks_high_risk(op: str) -> bool:
    name = bare_name(op)
    return any(marker in name for marker in _HIGH_RISK_NAME_MARKERS)


def required_roles_for_op(op: str) -> frozenset[str]:
    """返回该 op 允许的角色集合。

    解析顺序：完整键 → 裸 name → 高危关键词兜底 → 默认集合。
    """
    if op in OP_MIN_ROLES:
        return OP_MIN_ROLES[op]
    bare = bare_name(op)
    if bare in OP_MIN_ROLES:
        return OP_MIN_ROLES[bare]
    if looks_high_risk(op):
        logger.warning(
            "op %r 命中高危关键词但未登记在 OP_MIN_ROLES，按 admin 收严（fail-closed）", op
        )
        return _UNCLASSIFIED_HIGH_RISK_ROLES
    return DEFAULT_OP_ROLES


def is_self_service(op: str) -> bool:
    return op in OP_SELF_SERVICE or bare_name(op) in {bare_name(s) for s in OP_SELF_SERVICE}


def assert_op_role(op: str, principal: dict[str, Any]) -> dict[str, Any]:
    """按 :data:`OP_MIN_ROLES` 检查主体角色；不足 → 403。返回 principal。

    主体本身必须是 staff 令牌（由 ``get_current_user`` 保证）；这里只管角色。
    """
    allowed = required_roles_for_op(op)
    role = principal.get("claims", {}).get("role")
    if role not in allowed:
        raise HTTPException(
            status_code=403,
            detail=(f"operation {op!r} requires one of {sorted(allowed)}; current role {role!r}"),
        )
    return principal


def registered_ops() -> Iterable[str]:
    return tuple(OP_MIN_ROLES)


def assert_command_kind_role(kind: str, principal: dict[str, Any]) -> dict[str, Any]:
    """自然语言命令入口 (/agent/command) 的意图级最小权限。

    覆盖那些**不经过** :func:`assert_op_role` 的路径（编排器里的裸 SQL 调价、
    以及多步编排里只有第一步过 op 表的情况）。未登记的意图按 admin 收严。
    """
    allowed = COMMAND_KIND_MIN_ROLES.get(kind, _UNCLASSIFIED_HIGH_RISK_ROLES)
    role = principal.get("claims", {}).get("role")
    if role not in allowed:
        raise HTTPException(
            status_code=403,
            detail=(f"command {kind!r} requires one of {sorted(allowed)}; current role {role!r}"),
        )
    return principal
