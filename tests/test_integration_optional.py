"""端到端集成测试 — 需真实 Postgres (TEST_PG_DSN), 否则整体跳过。

走完整链路: 公开引导注册 (create_user) → 登录拿 token → 建区域 (受保护) → 建购物车。
omodul 层已有更细的集成测试; 本测试只验证服务层装配 (路由→omodul→DB→响应) 端到端通。

权限模型 (audit A3, op→角色映射表): 公开引导注册出来的账号默认只有 staff 角色,
`settings/create_region` 这类配置 op 要求 admin/support; 引导流程需要把注册出的
账号 id 写进 HEMALL_PRIVILEGED_STAFF_IDS 再重新登录。本测试同时覆盖"未提权 403"
与"提权后 201"两侧。
"""

from __future__ import annotations

import os

import pytest

TEST_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = pytest.mark.skipif(not TEST_DSN, reason="TEST_PG_DSN not set; skipping DB integration")


@pytest.fixture
def db_client(monkeypatch):
    monkeypatch.setenv("HEMALL_PG_DSN", TEST_DSN)
    # This test explicitly exercises the temporary first-admin bootstrap path.
    # Production keeps public administrator registration disabled by default.
    monkeypatch.setenv("HEMALL_ALLOW_PUBLIC_ADMIN_REGISTRATION", "true")
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


def test_register_login_and_create_region(db_client, monkeypatch):
    import uuid

    email = f"admin-{uuid.uuid4().hex[:8]}@hemall.test"
    # 1. 公开引导注册 (fixture 临时打开 HEMALL_ALLOW_PUBLIC_ADMIN_REGISTRATION)
    r = db_client.post(
        "/customers/create_user",
        json={"email": email, "password": "supersecret", "name": "Admin"},
    )
    assert r.status_code == 201, r.text
    # 2. 登录 —— 账号还没写进 HEMALL_PRIVILEGED_STAFF_IDS, 按映射表角色只是 staff
    r = db_client.post("/auth/login", json={"email": email, "password": "supersecret"})
    assert r.status_code == 200, r.text
    user_id = r.json()["user_id"]
    staff_headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    region_body = {
        "code": f"r-{uuid.uuid4().hex[:6]}",
        "name": "华东",
        "currency": "CNY",
        "payment_provider_names": ["manual"],
    }
    # 2a. 最小权限: settings/create_region 只认 admin/support, staff 一律 403 (audit A3 op→role 表)
    r = db_client.post("/settings/create_region", json=region_body, headers=staff_headers)
    assert r.status_code == 403, r.text
    # 2b. 引导流程: 把注册出的账号登记为 privileged, 重新登录才是 admin
    from app.deps import get_settings

    monkeypatch.setenv("HEMALL_PRIVILEGED_STAFF_IDS", user_id)
    get_settings.cache_clear()
    r = db_client.post("/auth/login", json={"email": email, "password": "supersecret"})
    assert r.status_code == 200, r.text
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    # 3. 受保护: 建区域 (payment provider "manual" 已由 bootstrap 注册)
    r = db_client.post("/settings/create_region", json=region_body, headers=headers)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "completed"
    # 4. 建购物车
    r = db_client.post("/cart/create_cart", json={"currency": "CNY"}, headers=headers)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "completed"
