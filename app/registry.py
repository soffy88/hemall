"""hemall omodul 目录 — 声明式登记全部商务 omodul, 供路由层批量生成端点。

每个商务 omodul 遵守统一契约 (async; Config 无参实例化; Input 为 pydantic 模型;
返回含 status 的 dict)。本模块把"omodul 名 → (可调用, Config 类, Input 类)"的
映射收敛到一处, 路由层据此声明式生成 REST 端点, 不手写样板。

命名约定: Config/Input 类名 = CamelCase(omodul 名) + "Config"/"Input"。
盘点确认 85/88 严格遵守; 3 个购物车行元素偏差, 在 _NAME_OVERRIDES 硬编码。
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Callable

# ── 领域 → omodul 名清单 (URL 前缀即字典键) ─────────────────────────────
DOMAINS: dict[str, list[str]] = {
    "settings": [
        "create_region",
        "update_region",
        "delete_region",
        "create_tax_rate",
        "update_tax_rate",
        "delete_tax_rate",
    ],
    "customers": [
        "create_user",
        "update_user",
        "reset_user_password",
        "create_customer",
        "update_customer",
        "add_customer_address",
        "update_customer_address",
        "delete_customer_address",
        "create_customer_group",
        "assign_customer_to_group",
    ],
    "catalog": [
        "create_product",
        "update_product",
        "delete_product",
        "create_product_variant",
        "update_product_variant",
        "delete_product_variant",
        "create_product_option",
        "update_product_option",
        "delete_product_option",
        "create_product_category",
        "update_product_category",
        "delete_product_category",
        "create_product_collection",
        "update_product_collection",
        "delete_product_collection",
    ],
    "inventory": [
        "create_price_list",
        "update_price_list",
        "delete_price_list",
        "add_prices_to_list",
        "remove_prices_from_list",
        "create_stock_location",
        "update_stock_location",
        "delete_stock_location",
        "adjust_inventory_level",
        "create_sales_channel",
        "update_sales_channel",
        "delete_sales_channel",
        "publish_products_to_channel",
        "unpublish_products_from_channel",
        "create_inventory_batch",
    ],
    "marketing": [
        "create_discount",
        "update_discount",
        "delete_discount",
        "create_discount_rule",
        "update_discount_rule",
        "create_discount_condition",
        "delete_discount_condition",
        "create_gift_card",
        "update_gift_card",
        "delete_gift_card",
    ],
    "cart": [
        "create_cart",
        "update_cart",
        "add_line_item_to_cart",
        "update_line_item_in_cart",
        "delete_line_item_from_cart",
        "set_cart_region",
        "set_cart_customer",
        "set_cart_billing_address",
        "set_cart_shipping_address",
        "add_shipping_method_to_cart",
        "apply_discount_to_cart",
        "remove_discount_from_cart",
        "apply_gift_card_to_cart",
        "remove_gift_card_from_cart",
        "create_payment_sessions",
        "update_payment_sessions",
        "set_payment_session",
    ],
    "checkout": [
        "authorize_payment_for_cart",
        "complete_checkout",
        "update_order",
        "cancel_order",
        "archive_order",
        "create_draft_order",
        "update_draft_order",
        "delete_draft_order",
        "mark_draft_order_paid",
    ],
    "fulfillment": [
        "create_fulfillment",
        "cancel_fulfillment",
        "ship_fulfillment",
    ],
    "aftersales": [
        "capture_payment",
        "refund_payment",
        "create_return_request",
        "receive_return",
        "cancel_return",
        "create_swap",
        "cancel_swap",
        "fulfill_swap",
        "process_swap_payment",
        "create_claim",
        "cancel_claim",
        "fulfill_claim",
    ],
    "batch": [
        "create_batch_job",
        "cancel_batch_job",
    ],
}

# 命名偏差: omodul 名 → (Config 类名, Input 类名)。其余按 CamelCase 约定派生。
_NAME_OVERRIDES: dict[str, tuple[str, str]] = {
    "add_line_item_to_cart": ("AddLineItemConfig", "AddLineItemInput"),
    "update_line_item_in_cart": ("UpdateLineItemConfig", "UpdateLineItemInput"),
    "delete_line_item_from_cart": ("DeleteLineItemConfig", "DeleteLineItemInput"),
}

# 恒公开端点 (无需 Bearer JWT):
#  - create_customer: 买家自助注册。
# 注意: create_user (管理员注册) 默认 *不* 公开——公开创建 app_user 等于任何人自助
# 拿管理员权限。引导首个管理员时把 HEMALL_ALLOW_PUBLIC_ADMIN_REGISTRATION=true 临时
# 打开 (见 _is_public_op), 建成后立即关回。
PUBLIC_OPS: set[str] = {"create_customer"}

#: 仅当 allow_public_admin_registration 打开时才追加为公开的引导端点。
_BOOTSTRAP_PUBLIC_OPS: set[str] = {"create_user"}


def _is_public_op(name: str) -> bool:
    """判定端点是否公开 (无需鉴权)。create_user 受配置开关门控。"""
    if name in PUBLIC_OPS:
        return True
    if name in _BOOTSTRAP_PUBLIC_OPS:
        # 惰性读配置避免与 deps/config 的导入环; 默认 False → create_user 需管理员 JWT。
        from .deps import get_settings

        return get_settings().allow_public_admin_registration
    return False


@dataclass(frozen=True)
class EndpointSpec:
    """单个 omodul 端点的装配描述。"""

    domain: str
    name: str
    fn: Callable[..., Any]
    config_cls: type
    input_cls: type
    path: str
    success_status: int
    require_auth: bool


def _camel(snake: str) -> str:
    return "".join(part.capitalize() for part in snake.split("_"))


def load_endpoint(domain: str, name: str) -> EndpointSpec:
    """按名加载一个 omodul 的 (可调用, Config, Input) 并生成端点描述。

    Raises:
        AttributeError/ModuleNotFoundError: 名字或类名解析失败 (说明目录需修正)。
    """
    module = importlib.import_module(f"omodul.{name}")
    fn = getattr(module, name)
    cfg_name, inp_name = _NAME_OVERRIDES.get(
        name, (f"{_camel(name)}Config", f"{_camel(name)}Input")
    )
    config_cls = getattr(module, cfg_name)
    input_cls = getattr(module, inp_name)
    return EndpointSpec(
        domain=domain,
        name=name,
        fn=fn,
        config_cls=config_cls,
        input_cls=input_cls,
        path=f"/{domain}/{name}",
        success_status=201 if name.startswith("create_") else 200,
        require_auth=not _is_public_op(name),
    )


def all_endpoint_specs() -> list[EndpointSpec]:
    """加载全部领域的全部 omodul 端点描述。"""
    specs: list[EndpointSpec] = []
    for domain, names in DOMAINS.items():
        for name in names:
            specs.append(load_endpoint(domain, name))
    return specs
