"""端到端集成测试 — 需真实 Postgres (TEST_PG_DSN), 否则整体跳过。

走完整链路: 注册管理员 (公开) → 登录拿 token → 建区域 (受保护) → 建购物车。
omodul 层已有更细的集成测试; 本测试只验证服务层装配 (路由→omodul→DB→响应) 端到端通。
"""

from __future__ import annotations

import os

import pytest

TEST_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = pytest.mark.skipif(not TEST_DSN, reason="TEST_PG_DSN not set; skipping DB integration")


@pytest.fixture
def db_client(monkeypatch):
    monkeypatch.setenv("HEMALL_PG_DSN", TEST_DSN)
    from app.deps import get_settings

    get_settings.cache_clear()  # 拾取新 DSN

    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app()
    with TestClient(app) as c:
        # 确认 DB 真的连上, 否则跳过 (而非误报失败)
        db_health = c.get("/health/ready").json()["checks"]["database"]["status"]
        if db_health != "up":
            pytest.skip("database not reachable at TEST_PG_DSN")
        yield c
    get_settings.cache_clear()


def test_register_login_and_create_region(db_client):
    import uuid

    email = f"admin-{uuid.uuid4().hex[:8]}@hemall.test"
    # 1. 公开注册管理员
    r = db_client.post("/customers/create_user", json={"email": email, "password": "supersecret", "name": "Admin"})
    assert r.status_code == 201, r.text
    # 2. 登录
    r = db_client.post("/auth/login", json={"email": email, "password": "supersecret"})
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    # 3. 受保护: 建区域 (payment provider "manual" 已由 bootstrap 注册)
    r = db_client.post(
        "/settings/create_region",
        json={"code": f"r-{uuid.uuid4().hex[:6]}", "name": "华东", "currency": "CNY", "payment_provider_names": ["manual"]},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "completed"
    # 4. 建购物车
    r = db_client.post("/cart/create_cart", json={"currency": "CNY"}, headers=headers)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "completed"
