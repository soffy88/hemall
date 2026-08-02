"""应用启动 / 路由注册 / OpenAPI 完整性测试。"""

from __future__ import annotations

from app.main import app


def test_health_returns_ok(client):
    # Phase 0: 健康检查端点拆分为 /health/live + /health/ready
    r = client.get("/health/live")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "alive"

    r = client.get("/health/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == "0.1.0"
    # 无 Postgres 的环境里 database 应为 down (优雅降级), 但端点仍响应
    assert body["checks"]["database"]["status"] == "down"


def test_all_commerce_paths_registered(client):
    """OpenAPI 应含全部 99 个商务端点 + 登录; 抽查各域代表路径。"""
    paths = app.openapi()["paths"]
    # 99 商务 + /auth/login = 100 个 POST 路径 (+ /health GET)
    post_paths = [p for p, ops in paths.items() if "post" in ops]
    assert len(post_paths) >= 100, f"expected >=100 POST paths, got {len(post_paths)}"

    sample = [
        "/settings/create_region",
        "/customers/create_customer",
        "/catalog/create_product",
        "/inventory/create_inventory_batch",
        "/marketing/create_discount",
        "/cart/add_line_item_to_cart",
        "/checkout/complete_checkout",
        "/fulfillment/create_fulfillment",
        "/aftersales/create_swap",
        "/batch/create_batch_job",
        "/auth/login",
    ]
    for p in sample:
        assert p in paths, f"missing path {p}"


def test_openapi_includes_nested_models(client):
    """嵌套请求模型 (盘点 §嵌套子模型) 应出现在 schema 组件里。"""
    schemas = app.openapi()["components"]["schemas"]
    for nested in [
        "FulfillmentItem",
        "ReturnItem",
        "SwapReturnItem",
        "SwapNewItem",
        "ClaimItem",
        "DraftOrderLineItem",
        "PriceListPriceItem",
    ]:
        assert nested in schemas, f"missing nested schema {nested}"


def test_update_cart_input_is_strict():
    """update_cart 的 Input 是 extra='forbid' — 多余字段在模型层即拒。

    直接在模型层测 (不经 HTTP): 无 DB 时 get_pool 会先于请求体校验抛 503,
    掩盖 422; 模型层断言才是该约束的精确验证。
    """
    import pytest
    from pydantic import ValidationError

    from omodul.update_cart import UpdateCartInput

    ok = UpdateCartInput(cart_id="c1", status="abandoned")
    assert ok.cart_id == "c1"
    with pytest.raises(ValidationError):
        UpdateCartInput(cart_id="c1", totally_unknown_field=1)
