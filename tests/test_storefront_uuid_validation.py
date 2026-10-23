"""非法 UUID 格式的 id 参数必须回 400，不能 500。

背景（2026-10-23 生产实测）：POST /store/carts/line-items 传空 cart_id 时，
asyncpg 的 UUID 编码抛 ValueError 一路冒到 ASGI 层 → 500。修复口径：
storefront 所有把客户端 id 绑定到 UUID 列的入口先过 _require_uuid
校验（_authorize_cart 入口覆盖整个购物车家族，其余端点逐个在入口校验）；
合法但不存在的 id 仍走原有 404 语义。

本文件两组：
- 无 DB 组：校验在任何 DB 访问之前触发，用 conftest 的 client 即可断言 400。
- DB 组：需真实 Postgres（TEST_PG_DSN，未设跳过），断言登录态端点的
  400 以及「合法格式但未知 cart 仍 404」不被误伤；自清理（FK 安全）。
"""

from __future__ import annotations

import asyncio
import os

import asyncpg
import pytest
from obase.uuid7 import uuid7

VALID_UUID = "00000000-0000-4000-8000-000000000099"


# ── 无 DB 组：格式校验先于 DB ────────────────────────────────────────


def test_add_line_item_empty_cart_id_is_400_not_500(client):
    r = client.post(
        "/store/carts/line-items",
        json={"cart_id": "", "batch_id": VALID_UUID, "quantity": 1},
    )
    assert r.status_code == 400


def test_add_line_item_garbage_cart_id_is_400(client):
    r = client.post(
        "/store/carts/line-items",
        json={"cart_id": "not-a-uuid", "batch_id": VALID_UUID, "quantity": 1},
    )
    assert r.status_code == 400


def test_get_cart_garbage_id_is_400(client):
    r = client.get("/store/carts/not-a-uuid")
    assert r.status_code == 400


def test_update_line_item_garbage_cart_id_is_400(client):
    r = client.put(
        "/store/carts/line-items",
        json={"cart_id": "bad", "line_item_id": "also-bad", "quantity": 1},
    )
    assert r.status_code == 400


def test_product_garbage_id_is_400(client):
    r = client.get("/store/products/not-a-uuid")
    assert r.status_code == 400


# ── DB 组：登录态端点 + 合法格式未知 id 仍 404 ──────────────────────

TEST_DSN = os.environ.get("TEST_PG_DSN")

pytestmark_db = pytest.mark.skipif(not TEST_DSN, reason="TEST_PG_DSN not set")


@pytest.fixture
def db_client(monkeypatch):
    monkeypatch.setenv("HEMALL_PG_DSN", TEST_DSN)
    monkeypatch.setenv(
        "HEMALL_REDIS_URL", os.environ.get("HEMALL_REDIS_URL", "redis://127.0.0.1:56379/0")
    )
    from app.deps import get_settings

    get_settings.cache_clear()
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app()
    with TestClient(app) as c:
        if c.get("/health/ready").json()["checks"]["database"]["status"] != "up":
            pytest.skip("database not reachable at TEST_PG_DSN")
        yield c
    get_settings.cache_clear()


@pytest.fixture
def customer_token(db_client):
    email = f"uuid-{uuid7()}@test.local"
    r = db_client.post(
        "/store/customers/register",
        json={"email": email, "password": "UuidTest123", "name": "uuid校验"},
    )
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    yield token
    asyncio.run(_delete_customer(email))


async def _delete_customer(email: str) -> None:
    conn = await asyncpg.connect(TEST_DSN)
    try:
        await conn.execute(
            "DELETE FROM cart WHERE customer_id IN (SELECT id FROM customer WHERE email = $1)",
            email,
        )
        await conn.execute("DELETE FROM customer WHERE email = $1", email)
    finally:
        await conn.close()


@pytestmark_db
def test_payment_query_garbage_id_is_400(db_client, customer_token):
    r = db_client.get(
        "/store/payments/not-a-uuid",
        headers={"Authorization": f"Bearer {customer_token}"},
    )
    assert r.status_code == 400


@pytestmark_db
def test_payment_create_garbage_order_id_is_400(db_client, customer_token):
    r = db_client.post(
        "/store/payments/create",
        json={"order_id": "bad"},
        headers={"Authorization": f"Bearer {customer_token}"},
    )
    assert r.status_code == 400


@pytestmark_db
def test_checkout_garbage_cart_id_is_400(db_client, customer_token):
    r = db_client.post(
        "/store/checkout",
        json={"cart_id": "bad"},
        headers={"Authorization": f"Bearer {customer_token}"},
    )
    assert r.status_code == 400


@pytestmark_db
def test_draft_order_garbage_batch_id_is_400(db_client, customer_token):
    r = db_client.post(
        "/store/draft-orders",
        json={"line_items": [{"batch_id": "bad", "quantity": 1}]},
        headers={"Authorization": f"Bearer {customer_token}"},
    )
    assert r.status_code == 400


@pytestmark_db
def test_valid_format_unknown_cart_still_404(db_client):
    r = db_client.post(
        "/store/carts/line-items",
        json={"cart_id": VALID_UUID, "batch_id": VALID_UUID, "quantity": 1},
    )
    assert r.status_code == 404
