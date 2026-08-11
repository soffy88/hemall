"""后台/内部 API 鉴权基线回归测试。

安全加固后，这些非顾客面路由必须：无 token → 401，顾客 token → 401，
只有员工 (app_user) JWT 才放行到 handler。防止后续改动无意中重开敞口。

顾客面 (/store/*) 与网关回调 (/payments/*/notify, HMAC 验签) 不在此列。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from obase.crypto.util import CryptoUtil

from app.deps import get_settings
from app.main import app

# (method, path, json_body) — 每个受保护路由取一个代表性端点。
PROTECTED_ENDPOINTS = [
    ("POST", "/orders/x/confirm", {}),
    ("GET", "/orders/", None),
    ("POST", "/inventory/reserve", {"product_id": "p1"}),
    ("POST", "/payments/refund", {"payment_id": "x"}),
    ("POST", "/payments/create", {"order_id": "o1", "amount": 1, "provider": "manual"}),
    ("POST", "/push/send", {"target_device_ids": ["x"], "title": "t", "body": "b"}),
    ("POST", "/ai/chat", {"message": "hi"}),
    ("POST", "/search/sync/product", {"product_id": "p1"}),
    ("DELETE", "/cache/prefix/foo", None),
    ("GET", "/risk/rules", None),
]


@pytest.fixture
def customer_headers() -> dict[str, str]:
    s = get_settings()
    tok = CryptoUtil.jwt_sign(
        payload={"typ": "customer", "customer_id": "c1", "email": "c@c.c"},
        secret=s.jwt_secret,
        algorithm=s.jwt_algorithm,
    )
    return {"Authorization": f"Bearer {tok}"}


@pytest.mark.parametrize("method,path,body", PROTECTED_ENDPOINTS)
def test_backoffice_endpoint_rejects_anonymous(client, method, path, body):
    r = client.request(method, path, json=body)
    assert r.status_code == 401, f"{method} {path} 应对匿名请求返回 401"


@pytest.mark.parametrize("method,path,body", PROTECTED_ENDPOINTS)
def test_backoffice_endpoint_rejects_customer_token(
    client, customer_headers, method, path, body
):
    r = client.request(method, path, json=body, headers=customer_headers)
    assert r.status_code == 401, f"{method} {path} 不应接受顾客 token"


@pytest.mark.parametrize("method,path,body", PROTECTED_ENDPOINTS)
def test_backoffice_endpoint_accepts_staff_token(
    client, auth_headers, method, path, body
):
    # 员工 token 应越过鉴权到达 handler：不再是 401 (可能 200/422/503/404 视 DB/校验)。
    r = client.request(method, path, json=body, headers=auth_headers)
    assert r.status_code != 401, (
        f"{method} {path} 应接受员工 token, 实得 {r.status_code}"
    )


# 顾客自助报案：需顾客 token，身份/信誉/订单归属服务端认定，绝不信客户端自报。
_RMA_BODY = {"order_id": "o1", "batch_id": "b1", "evidence_image_url": "http://x/y.jpg"}


def test_rma_claim_rejects_anonymous(client):
    r = client.post("/aftersales/submit_rma_claim", json=_RMA_BODY)
    assert r.status_code == 401


def test_rma_claim_rejects_staff_token(client, auth_headers):
    # 员工 token 不是顾客主体，get_current_customer 拒之。
    r = client.post(
        "/aftersales/submit_rma_claim", json=_RMA_BODY, headers=auth_headers
    )
    assert r.status_code == 401


def test_rma_claim_ignores_client_supplied_trust_and_identity(client, customer_headers):
    # 请求体夹带 user_id/user_trust_score 应被忽略 (模型无此字段)，顾客 token 过鉴权
    # 后进入 handler（无 DB → 503，而非被自报 trust 骗过）。
    r = client.post(
        "/aftersales/submit_rma_claim",
        json={**_RMA_BODY, "user_id": "victim", "user_trust_score": 100},
        headers=customer_headers,
    )
    assert r.status_code != 401
