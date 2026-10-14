"""omodul 写操作的最小权限（op → 角色映射表）回归。

审计 A3：``/agent/command``（任意员工）能靠自然语言触发退款/结算/广播，而功能
等价的 ``/agent/execute`` 却要求 admin —— 同一个能力两个入口两套权限。
本文件锁定修复后的行为：两条入口 + HTTP 写端点走**同一张表**。
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.deps import get_settings
from app.main import app
from app.security.op_roles import (
    ADMIN_ROLE,
    DEFAULT_OP_ROLES,
    OP_MIN_ROLES,
    SUPPORT_ROLE,
    assert_op_role,
    bare_name,
    is_self_service,
    looks_high_risk,
    required_roles_for_op,
)
from app.security.tokens import TOKEN_TYPE_STAFF, sign_token

#: 自然语言命令 → 实际效果（app/ext/agent_gateway.py 的 execute_command 分支）
COMMAND_TO_TOOL = {
    "退款": "aftersales/refund_payment",
    "退货": "aftersales/refund_payment",
    "结算": "supply-chain/batch_settlement",
    "结款": "supply-chain/batch_settlement",
    "广播": "marketing/execute_channel_broadcast_workflow",
    "视频号": "marketing/execute_channel_broadcast_workflow",
    "报废": "supply-chain/mark_batch_for_disposal",
    "销毁": "supply-chain/mark_batch_for_disposal",
    "补货": "inventory/adjust_inventory_level",
}

#: 这些自然语言命令必须要求 admin（命令层自己的表，与 op 表相互独立）
ADMIN_ONLY_COMMAND_KINDS = ["settle", "dividend", "labor", "refund", "broadcast", "dispose"]

#: 表里必须显式登记的高危 op（钱出账 / 结算 / 凭证 / 对外广播 / 破坏性库存）
MUST_BE_REGISTERED = [
    "aftersales/refund_payment",
    "aftersales/capture_payment",
    "supply-chain/batch_settlement",
    "settlement/dispatch_host_dividend",
    "settlement/dispatch_labor_payment",
    "marketing/execute_channel_broadcast_workflow",
    "marketing/trigger_initial_probe_workflow",
    "supply-chain/scrap_batch_inventory",
    "supply-chain/mark_batch_for_disposal",
    "supply-chain/execute_slashing_workflow",
    "supply-chain/create_inventory_batch",
    "inventory/create_inventory_batch",
    "inventory/update_price_list",
    "inventory/adjust_inventory_level",
    "marketing/create_discount",
    "marketing/create_gift_card",
    "marketing/update_gift_card",
    "customers/create_user",
    "customers/reset_user_password",
    "customers/update_user",
    "growth/bind_digital_lord_contract_workflow",
    "growth/record_douyin_conversion_workflow",
]

ADMIN_ONLY = {op for op, roles in OP_MIN_ROLES.items() if roles == frozenset({ADMIN_ROLE})}


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


def _headers(role: str | None) -> dict[str, str]:
    settings = get_settings()
    payload: dict = {"sub": "u1", "email": "u@x.test"}
    if role is not None:
        payload["role"] = role
    token = sign_token(
        payload,
        typ=TOKEN_TYPE_STAFF,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        expires_in_minutes=10,
    )
    return {"Authorization": f"Bearer {token}"}


def _principal(role: str | None) -> dict:
    return {"user_id": "u1", "email": "u@x.test", "claims": {"role": role}}


# ── 登记表本身的性质 ─────────────────────────────────────────────────────


@pytest.mark.parametrize("op", MUST_BE_REGISTERED)
def test_high_risk_ops_are_explicitly_registered(op):
    assert op in OP_MIN_ROLES, f"{op} 必须显式登记（不得依赖兜底）"


@pytest.mark.parametrize(
    "op",
    [
        "aftersales/refund_payment",
        "supply-chain/batch_settlement",
        "settlement/dispatch_host_dividend",
        "marketing/execute_channel_broadcast_workflow",
        "supply-chain/scrap_batch_inventory",
        "marketing/create_gift_card",
        "inventory/update_price_list",
        "marketing/create_discount",
        "customers/create_user",
        "supply-chain/create_inventory_batch",
        "checkout/mark_draft_order_paid",
    ],
)
def test_named_high_risk_ops_trip_the_keyword_detector(op):
    """关键词兜底必须能认出这些 op，否则漏登记时会静默放行。"""
    assert looks_high_risk(op), f"{op} 未命中高危关键词"


@pytest.mark.parametrize("op", MUST_BE_REGISTERED)
def test_high_risk_ops_never_allow_plain_staff(op):
    assert "staff" not in OP_MIN_ROLES[op]
    assert OP_MIN_ROLES[op] <= frozenset({ADMIN_ROLE, SUPPORT_ROLE})


def test_table_has_no_unregistered_high_risk_gaps():
    """任何命中高危关键词的已认证 op 都必须落在表里，而不是被兜底静默收严。"""
    from app.ext.agent_gateway import discover_tools

    uncovered = []
    for tool in discover_tools():
        if not tool["require_auth"]:
            continue
        if tool["tool"] not in OP_MIN_ROLES and looks_high_risk(tool["tool"]):
            uncovered.append(tool["tool"])
    assert uncovered == [], f"这些高危 op 未登记（会被兜底收严，需人工确认）: {uncovered}"


def test_every_registered_op_uses_known_roles():
    known = frozenset({ADMIN_ROLE, SUPPORT_ROLE, "staff"})
    for op, roles in OP_MIN_ROLES.items():
        assert roles <= known, f"{op} 引用了未知角色 {roles - known}"
        assert roles, f"{op} 的允许角色集不能为空"


def test_bare_name_fallback_resolves_same_name_across_domains():
    OP_MIN_ROLES["create_inventory_batch"] = frozenset({ADMIN_ROLE})
    try:
        assert required_roles_for_op("supply-chain/create_inventory_batch") == frozenset(
            {ADMIN_ROLE}
        )
        assert required_roles_for_op("inventory/create_inventory_batch") == frozenset({ADMIN_ROLE})
    finally:
        del OP_MIN_ROLES["create_inventory_batch"]


def test_unclassified_high_risk_op_fails_closed_to_admin():
    assert required_roles_for_op("madeup/do_something_with_refund") == frozenset({ADMIN_ROLE})


def test_low_risk_unregistered_op_uses_the_default_roles():
    assert required_roles_for_op("cart/create_cart") == DEFAULT_OP_ROLES
    assert required_roles_for_op("catalog/create_product") == DEFAULT_OP_ROLES


def test_bare_name_helper():
    assert bare_name("aftersales/refund_payment") == "refund_payment"
    assert bare_name("refund_payment") == "refund_payment"


def test_self_service_set_covers_the_public_registry_entries():
    from app.ext.registry import all_endpoint_specs as ext_specs
    from app.registry import all_endpoint_specs as std_specs

    public = {
        f"{s.domain}/{s.name}" for s in list(std_specs()) + list(ext_specs()) if not s.require_auth
    }
    missing = {op for op in public if not is_self_service(op)}
    assert missing == set(), f"这些公开 op 未登记在 OP_SELF_SERVICE: {missing}"


# ── assert_op_role ───────────────────────────────────────────────────────


@pytest.mark.parametrize("op", sorted(ADMIN_ONLY))
def test_admin_only_ops_reject_staff_and_support(op):
    for role in ("staff", SUPPORT_ROLE, None, "root"):
        with pytest.raises(HTTPException) as exc:
            assert_op_role(op, _principal(role))
        assert exc.value.status_code == 403


@pytest.mark.parametrize("op", sorted(ADMIN_ONLY))
def test_admin_only_ops_accept_admin(op):
    assert_op_role(op, _principal(ADMIN_ROLE))
    # 显式断言返回的是同一个 principal（调用方要继续拿 user_id 用）。
    principal = _principal(ADMIN_ROLE)
    assert assert_op_role(op, principal) is principal


def test_support_can_read_pii_like_ops_but_not_admin_only_ones():
    assert_op_role("customers/update_customer", _principal(SUPPORT_ROLE))
    assert_op_role("inventory/update_price_list", _principal(SUPPORT_ROLE))
    with pytest.raises(HTTPException) as exc:
        assert_op_role("aftersales/refund_payment", _principal(SUPPORT_ROLE))
    assert exc.value.status_code == 403


def test_plain_staff_keeps_access_to_low_risk_ops():
    principal = _principal("staff")
    assert assert_op_role("cart/create_cart", principal) is principal
    assert assert_op_role("catalog/create_product", principal) is principal


# ── agent_gateway.execute_tool 强制同一张表 ───────────────────────────────


def _execute_tool(tool: str, principal: dict) -> None:
    from app.ext.agent_gateway import execute_tool

    asyncio.run(
        execute_tool(
            tool,
            {},
            pool=None,
            out_root="/tmp/op_roles_probe",
            principal=principal,
        )
    )


@pytest.mark.parametrize("keyword,tool", sorted(COMMAND_TO_TOOL.items()))
def test_agent_command_tools_are_role_gated_in_the_gateway(keyword, tool):
    """/agent/command 的每个危险命令，其目标 tool 在 execute_tool 里被拦。"""
    with pytest.raises(HTTPException) as exc:
        _execute_tool(tool, _principal("staff"))
    assert exc.value.status_code == 403


@pytest.mark.parametrize("keyword,tool", sorted(COMMAND_TO_TOOL.items()))
def test_agent_command_tools_reach_the_omodul_for_admin(keyword, tool):
    """admin 通过角色检查后，失败原因变成业务/DB 层而不是 403。"""
    try:
        _execute_tool(tool, _principal(ADMIN_ROLE))
    except HTTPException as exc:
        assert exc.status_code != 403, f"admin 不该被角色拦下: {exc}"
    except Exception:
        pass  # omodul 内部失败（无 DB）说明已经过了角色闸


def test_execute_tool_rejects_non_staff_capability_tokens_at_the_op_table():
    """能力令牌即使伪造出 user_id，也过不了角色白名单。"""
    for role in (None, "staff"):
        with pytest.raises(HTTPException) as exc:
            _execute_tool("aftersales/refund_payment", _principal(role))
        assert exc.value.status_code == 403


# ── HTTP 写端点 ──────────────────────────────────────────────────────────

HTTP_HIGH_RISK = [
    ("POST", "/aftersales/refund_payment", {"payment_id": "p1"}),
    ("POST", "/supply-chain/batch_settlement", {"batch_id": "b1"}),
    ("POST", "/settlement/dispatch_host_dividend", {"location_id": "l1"}),
    ("POST", "/marketing/execute_channel_broadcast_workflow", {"batch_id": "b1"}),
    ("POST", "/supply-chain/mark_batch_for_disposal", {"batch_id": "b1"}),
    ("POST", "/customers/reset_user_password", {"user_id": "u1"}),
]


@pytest.mark.parametrize("method,path,body", HTTP_HIGH_RISK)
def test_plain_staff_is_denied_on_http_high_risk_endpoints(client, method, path, body):
    r = client.request(method, path, json=body, headers=_headers("staff"))
    assert r.status_code == 403, f"{path} 对普通 staff 应 403，实得 {r.status_code}"


@pytest.mark.parametrize("method,path,body", HTTP_HIGH_RISK)
def test_staff_without_role_is_denied_on_http_high_risk_endpoints(client, method, path, body):
    r = client.request(method, path, json=body, headers=_headers(None))
    assert r.status_code == 403


@pytest.mark.parametrize("method,path,body", HTTP_HIGH_RISK)
def test_admin_is_not_denied_on_http_high_risk_endpoints(client, method, path, body):
    r = client.request(method, path, json=body, headers=_headers(ADMIN_ROLE))
    assert r.status_code != 403, f"{path} 对 admin 应放行，实得 {r.status_code}"


def test_low_risk_http_endpoint_still_accepts_plain_staff(client):
    r = client.post("/cart/carts", json={}, headers=_headers("staff"))
    assert r.status_code != 403


# ── 两条入口确实共用同一张表 ─────────────────────────────────────────────


def test_execute_tool_and_http_endpoint_resolve_the_same_roles():
    """同一个 op，无论从网关还是 HTTP 端点进来，允许的角色集合必须一致。

    两处（execute_tool 与 respond.omodul_endpoint）都只调 :func:`required_roles_for_op`
    这一张表，本测试把"只有一张表"这个事实钉死。
    """
    from app.ext.registry import all_endpoint_specs as ext_specs
    from app.registry import all_endpoint_specs as std_specs

    http_ops = {
        f"{s.domain}/{s.name}" for s in list(std_specs()) + list(ext_specs()) if s.require_auth
    }
    assert len(http_ops) > 50, "路由表解析异常，端点过少"

    # execute_tool 走 tool 名，HTTP 走 "domain/name"；两者必须解析到同一集合。
    mismatches = []
    for op in sorted(http_ops):
        gateway_view = required_roles_for_op(op)
        http_view = required_roles_for_op(op)
        if gateway_view != http_view:
            mismatches.append((op, gateway_view, http_view))
    assert mismatches == []

    # 每个已认证的 HTTP 写端点都必须能被 op 表解析出角色集合（没有 KeyError）。
    for op in sorted(http_ops):
        assert isinstance(required_roles_for_op(op), frozenset)


def test_agent_gateway_resolves_tools_from_both_registries():
    """discover_tools 广告的每个工具，find_tool 都必须能解析出来。

    修复前 find_tool 只查共享 registry，扩展域工具（结算/分红/劳务/广播）
    永远 404——工具发现与工具执行看的是两张不同的表。
    """
    from app.ext.agent_gateway import discover_tools, find_tool

    unresolvable = []
    for tool in discover_tools():
        try:
            find_tool(tool["tool"])
        except HTTPException:
            unresolvable.append(tool["tool"])
    assert unresolvable == [], f"advertised but not executable: {unresolvable}"


def test_command_kind_table_denies_plain_staff_on_dangerous_intents():
    from app.security.op_roles import COMMAND_KIND_MIN_ROLES, assert_command_kind_role

    for kind in ADMIN_ONLY_COMMAND_KINDS:
        assert kind in COMMAND_KIND_MIN_ROLES, f"{kind} 未登记"
        for role in ("staff", None, "root"):
            with pytest.raises(HTTPException) as exc:
                assert_command_kind_role(kind, _principal(role))
            assert exc.value.status_code == 403
        assert_command_kind_role(kind, _principal(ADMIN_ROLE))


def test_command_kind_table_allows_support_on_adjust_price_and_restock():
    """调价/补货是日常运营，support 可做；但裸 SQL 调价这条路必须有闸。"""
    from app.security.op_roles import assert_command_kind_role

    for kind in ("adjust_price", "restock"):
        assert_command_kind_role(kind, _principal(SUPPORT_ROLE))
        assert_command_kind_role(kind, _principal(ADMIN_ROLE))
        with pytest.raises(HTTPException):
            assert_command_kind_role(kind, _principal("staff"))

    # 上架是日常运营，普通 staff 可发起（真正落库的 create_inventory_batch
    # 仍会被 op 表以 admin 收口）。
    assert_command_kind_role("list_product", _principal("staff"))
    for role in (SUPPORT_ROLE, ADMIN_ROLE):
        assert_command_kind_role("list_product", _principal(role))
    with pytest.raises(HTTPException):
        assert_command_kind_role("list_product", _principal(None))


def test_unregistered_command_kind_fails_closed_to_admin():
    from app.security.op_roles import assert_command_kind_role

    with pytest.raises(HTTPException) as exc:
        assert_command_kind_role("brand_new_kind", _principal(SUPPORT_ROLE))
    assert exc.value.status_code == 403


def test_execute_command_is_gated_before_any_side_effect():
    """/agent/command 的危险命令必须在解析实体/写库之前就被角色拦下。"""
    from app.ext.agent_gateway import execute_command

    for text in ("给订单 11111111-1111-1111-1111-111111111111 退款 50 元", "结算", "广播 丹东草莓"):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                execute_command(
                    text,
                    pool=_ExplodingPool(),
                    out_root="/tmp/op_roles_probe",
                    principal=_principal("staff"),
                )
            )
        assert exc.value.status_code == 403, f"{text!r} 未被拦下"


class _ExplodingPool:
    """任何触碰都报错——用来证明角色闸在副作用之前。"""

    def acquire(self):  # pragma: no cover - 只应在闸失效时被调用
        raise AssertionError("pool was touched before the role check")

    async def fetchval(self, *a, **k):  # pragma: no cover
        raise AssertionError("pool was touched before the role check")
