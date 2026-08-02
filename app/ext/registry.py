"""app.ext.registry — hemall 商城扩展层 omodul 目录，供 HTTP 路由层声明式生成端点。

跟 hemall 原有商城域的 app/registry.py 同一个模式 (EndpointSpec + respond.py
的 omodul_endpoint 工厂)，区别只在于 omodul 的模块来源：hemall 原有域的
omodul 来自共享包 platform/3O/omodul (``import omodul.xxx``)，本模块 (原
"ClearNode") 的 omodul 是本项目内的 app.ext.omodul 包。

**统一改造说明**：加车/运费/结账 (add_line_item_to_cart / cart_shipping_
method_set / complete_checkout) 以前在这里各自有一份跟共享包同名但完全不同
表结构的实现，是"两套平行系统"问题最直接的体现。统一之后这三个不再在这份
目录里注册——购物车/结账走的是同一套共享表，直接用 hemall 原有的
``/store/carts/*``、``/store/checkout`` 端点即可，不需要在这个前缀下再镜像
一份完全等价的端点。create_inventory_batch 保留在这里，是因为它需要
supplier_id/expiration_time 这两个共享包不认识的本地补丁列，属于真正的
额外能力，不是重复。

execute_ambient_intake_workflow 不在这份目录里——它的 Input 有一个 bytes
字段 (video_stream)，走这套"直接把 Input pydantic 模型当请求体 schema"的
声明式生成方式会碰到 JSON body 里 bytes 语义不清 (到底是 UTF-8 文本还是
base64) 的问题，改成在 app/routers.py 里手写一个 str->bytes 转换的 wrapper
路由，不硬塞进这套通用机制。

鉴权边界 (跟用户确认过的划分，见对话；v2.0 沿用同一个判断标准——顾客/供应商/
邻居这类"外部人"自助操作公开，仓管/仲裁/处罚这类"内部人"操作要 admin token)：
    顾客侧操作 (加车/运费/结账/集单/会员/押金退货/自动补货/客诉/比价) 公开
    无需登录，贴合 SPEC "零登录扫码即买" 的设计；供应商入驻 (claim_origin_
    workflow) 和邻居代送确认 (execute_peer_delivery_workflow) 同样公开——
    跟顾客一样是"外部人自助操作"，ClearNode 压根没有供应商/邻居的登录体系。
    仓管/结算/处罚/仲裁执行侧操作 (入库/销毁/结算/拣货确认/新节点注册/工资
    分润/斩仓/幽灵库存上报/CV入库/仲裁执行末端) 复用现有 admin Bearer JWT
    (跟 app/routers.py 的 admin_read_router 同一套 _require_admin 校验逻辑，
    respond.omodul_endpoint 的 require_auth=True 分支已经实现了这个校验)。
"""

from __future__ import annotations

import importlib

from ..registry import EndpointSpec

# 统一改造收尾：域名不再带 "clearnode/" 前缀。能对上 hemall 原有 registry
# 域名的 (名字不撞车) 直接复用同一个域名字符串——两份 registry 各自的
# all_endpoint_specs() 分别往同一个 FastAPI router 里挂路由，只要
# (method, path) 组合不重复，物理上是两个 Python 字典没关系，URL 上看
# 就是同一个域。撞车的 (create_inventory_batch 在 inventory 域已经被共享
# 版占了) 或者压根没有对应共享域的，新开一个不带 "clearnode" 字样的域名。
# load_endpoint 直接拼 f"/{domain}/{name}"，不需要额外加前缀逻辑。
DOMAINS: dict[str, list[str]] = {
    # create_inventory_batch 跟共享 inventory 域同名会撞车 (共享版没有
    # supplier_id/expiration_time 本地列)，这一组整体留在独立域名下。
    "supply-chain": [
        "create_inventory_batch",
        "mark_batch_for_disposal",
        "batch_settlement",
        "claim_origin_workflow",
        "execute_slashing_workflow",
        # submit_supplier_reverse_auction_workflow: 供应商 (果农) 自助竞标准
        # 入，跟 claim_origin_workflow 同类"外部人自助操作"，公开无需 admin。
        "submit_supplier_reverse_auction_workflow",
    ],
    "community": ["create_crowd_intent"],
    "membership": ["process_subscription"],
    # 跟共享 fulfillment 域 (create_fulfillment/cancel_fulfillment/
    # ship_fulfillment) 名字不撞，语义也贴合 (拣货确认/异常上报/邻居代送
    # 都是履约环节)，直接复用同一个域名。
    "fulfillment": [
        "confirm_batch_pick",
        "report_phantom_stock_workflow",
        "execute_peer_delivery_workflow",
    ],
    # 跟共享 aftersales 域 (capture_payment/refund_payment/create_return_
    # request/create_swap/create_claim 等) 名字不撞，语义也贴合 (押金退货/
    # RMA/责任路由都是售后)，直接复用同一个域名。
    "aftersales": [
        "tote_deposit_and_refund",
        "process_drop_return",
        "process_credit_gated_rma_workflow",
        "execute_liability_routing_workflow",
    ],
    # commission_new_location 是"新开一个门店节点"，跟共享 inventory 域的
    # create_stock_location 系列语义贴合，名字不撞，直接复用同一个域名。
    "inventory": ["commission_new_location", "execute_ambient_replenishment"],
    "settlement": ["dispatch_labor_payment", "dispatch_host_dividend"],
    # 跟共享 marketing 域 (create_discount/create_gift_card 等) 名字不撞，
    # 语义也贴合 (清仓喊单/渠道播报都是营销动作)，直接复用同一个域名。
    "marketing": [
        "generate_crushing_offer_workflow",
        "execute_channel_broadcast_workflow",
    ],
    "growth": [
        "process_cloud_franchise_claim_workflow",
        "bind_digital_lord_contract_workflow",
        "record_douyin_conversion_workflow",
    ],
}

#: 仓管/结算/处罚/仲裁执行/播报侧需要 admin Bearer JWT；不在这个集合里的
#: (顾客/供应商/邻居自助操作) 公开。
ADMIN_OPS: set[str] = {
    "create_inventory_batch",
    "mark_batch_for_disposal",
    "batch_settlement",
    "execute_slashing_workflow",
    "confirm_batch_pick",
    "report_phantom_stock_workflow",
    "commission_new_location",
    "dispatch_labor_payment",
    "dispatch_host_dividend",
    "execute_liability_routing_workflow",
    # execute_channel_broadcast_workflow: 顾客不该能自己触发一次微信视频号
    # 发版 (那是运营/引擎的动作，不是顾客自助操作)，跟 report_phantom_stock_
    # workflow 这类"内部人操作"归一类。
    "execute_channel_broadcast_workflow",
    # bind_digital_lord_contract_workflow: "500 单点火达标"的判定不在这个
    # omodul 内部 (调用方自行核实)，能触发这个端点等于能白嫖一个节点的永久
    # 领主税——必须是运营后台核实过阈值后才能调，不能顾客自己点。
    "bind_digital_lord_contract_workflow",
    # record_douyin_conversion_workflow: 如果公开，任何人都能拿别人的
    # order_id 编一个 douyin_uid 调这个端点，把根本不存在的"推荐关系"骗
    # 成一笔真实分润——这是"钱从哪儿来"的归因记账，不是顾客自助操作，
    # 必须走运营/未来的抖音回调网关 (这轮还没有签名校验机制)。
    "record_douyin_conversion_workflow",
}


def _camel(snake: str) -> str:
    return "".join(part.capitalize() for part in snake.split("_"))


def load_endpoint(domain: str, name: str) -> EndpointSpec:
    """按名加载一个 ClearNode omodul 的 (可调用, Config, Input) 并生成端点描述。

    命名全部严格遵守 CamelCase(name) + "Config"/"Input" 约定 (跟 hemall 原有
    registry 不同，这里不需要 _NAME_OVERRIDES——14 个 omodul 逐一核对过，无例外)。
    """
    module = importlib.import_module(f"app.ext.omodul.{name}")
    fn = getattr(module, name)
    config_cls = getattr(module, f"{_camel(name)}Config")
    input_cls = getattr(module, f"{_camel(name)}Input")
    return EndpointSpec(
        domain=domain,
        name=name,
        fn=fn,
        config_cls=config_cls,
        input_cls=input_cls,
        path=f"/{domain}/{name}",
        success_status=201 if name.startswith("create_") else 200,
        require_auth=name in ADMIN_OPS,
    )


def all_endpoint_specs() -> list[EndpointSpec]:
    """加载全部 ClearNode omodul 端点描述。"""
    specs: list[EndpointSpec] = []
    for domain, names in DOMAINS.items():
        for name in names:
            specs.append(load_endpoint(domain, name))
    return specs
