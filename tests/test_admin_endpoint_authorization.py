"""/admin/* 后台读端点的越权与角色基线回归。

六种主体逐一覆盖（任务要求的固定矩阵）：
  1. 访客购物车令牌 typ=cart
  2. 收据令牌 typ=receipt
  3. 顾客登录令牌 typ=customer
  4. 匿名（无 Authorization 头）
  5. 无 role claim 的 staff 令牌
  6. role 正确的 staff 令牌

审计 P0：`_require_admin` 曾经只判 `typ != "customer"`（黑名单），于是 1/2/4
里的能力令牌与裸令牌都能读全部后台读端点；5 的无角色令牌同样畅通。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from obase.crypto.util import CryptoUtil

from app.config import Settings
from app.deps import get_settings
from app.main import app
from app.routers import (
    ADMIN_PII_PATH_PREFIXES,
    ADMIN_PII_ROLES,
    ADMIN_STAFF_ROLES,
    admin_roles_for_path,
)
from app.security.tokens import (
    TOKEN_TYPE_CART,
    TOKEN_TYPE_CUSTOMER,
    TOKEN_TYPE_RECEIPT,
    TOKEN_TYPE_STAFF,
    sign_token,
)

#: 后台读端点全集（改路由时这份清单必须同步，否则新端点逃过回归）
ADMIN_ENDPOINTS = [
    ("GET", "/admin/products"),
    ("GET", "/admin/products/p1"),
    ("GET", "/admin/orders"),
    ("GET", "/admin/orders/o1"),
    ("GET", "/admin/orders/o1/fulfillments"),
    ("GET", "/admin/customers"),
    ("GET", "/admin/customers/c1/addresses"),
    ("GET", "/admin/users"),
    ("GET", "/admin/gift-cards"),
    ("GET", "/admin/dashboard/kpis"),
    ("GET", "/admin/discounts"),
    ("GET", "/admin/returns"),
    ("GET", "/admin/swaps"),
    ("GET", "/admin/claims"),
    ("GET", "/admin/price-lists"),
    ("GET", "/admin/stock-locations"),
    ("GET", "/admin/sales-channels"),
    ("GET", "/admin/regions"),
    ("GET", "/admin/tax-rates"),
    ("GET", "/admin/categories"),
    ("GET", "/admin/collections"),
    ("GET", "/admin/customer-groups"),
    ("GET", "/admin/batch-jobs"),
    ("GET", "/admin/supply-chain/suppliers"),
    ("GET", "/admin/aftersales/rma-claims"),
    ("GET", "/admin/marketing/broadcast-logs"),
    ("GET", "/admin/marketing/price-benchmarks"),
    ("GET", "/admin/marketing/probe-logs"),
    ("GET", "/admin/growth/affiliate-contracts"),
    ("GET", "/admin/growth/conversion-logs"),
]

PII_ENDPOINTS = [
    ep for ep in ADMIN_ENDPOINTS
    if admin_roles_for_path(ep[1]) == ADMIN_PII_ROLES
]
STAFF_TIER_ENDPOINTS = [
    ep for ep in ADMIN_ENDPOINTS
    if admin_roles_for_path(ep[1]) == ADMIN_STAFF_ROLES
]


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


def _token(payload: dict, *, typ: str | None = None, secret: str | None = None) -> str:
    settings = get_settings()
    if typ is None:
        return CryptoUtil.jwt_sign(
            payload=payload,
            secret=secret or settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
        )
    return sign_token(
        payload,
        typ=typ,
        secret=secret or settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        expires_in_minutes=10,
    )


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ── 角色分层表本身 ───────────────────────────────────────────────────────


def test_every_admin_endpoint_is_classified_into_exactly_one_tier():
    assert len(PII_ENDPOINTS) + len(STAFF_TIER_ENDPOINTS) == len(ADMIN_ENDPOINTS)
    assert not set(ep[1] for ep in PII_ENDPOINTS) & set(ep[1] for ep in STAFF_TIER_ENDPOINTS)


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/admin/customers", ADMIN_PII_ROLES),
        ("/admin/customers/c1/addresses", ADMIN_PII_ROLES),
        ("/admin/users", ADMIN_PII_ROLES),
        ("/admin/gift-cards", ADMIN_PII_ROLES),
        ("/admin/orders/o1", ADMIN_PII_ROLES),
        ("/admin/products", ADMIN_STAFF_ROLES),
        ("/admin/dashboard/kpis", ADMIN_STAFF_ROLES),
        ("/admin", ADMIN_STAFF_ROLES),
        ("/supply-chain/execute_ambient_intake_workflow", ADMIN_PII_ROLES),
    ],
)
def test_admin_roles_for_path(path, expected):
    assert admin_roles_for_path(path) == expected


def test_plain_staff_is_denied_on_every_pii_endpoint():
    assert "staff" not in ADMIN_PII_ROLES
    assert "staff" in ADMIN_STAFF_ROLES
    assert ADMIN_PII_ROLES <= ADMIN_STAFF_ROLES


def test_pii_prefix_list_covers_the_documented_sensitive_reads():
    for path in ("/admin/customers", "/admin/users", "/admin/gift-cards",
                 "/admin/customers/c1/addresses"):
        assert any(
            path == prefix or path.startswith(prefix + "/")
            for prefix in ADMIN_PII_PATH_PREFIXES
        ), path


# ── 六种主体的越权矩阵 ───────────────────────────────────────────────────


@pytest.mark.parametrize("method,path", ADMIN_ENDPOINTS)
def test_anonymous_is_rejected(client, method, path):
    assert client.request(method, path).status_code == 401


@pytest.mark.parametrize("method,path", ADMIN_ENDPOINTS)
def test_cart_capability_token_is_rejected(client, method, path):
    token = _token({"cart_id": "c1"}, typ=TOKEN_TYPE_CART)
    assert client.request(method, path, headers=_headers(token)).status_code in (401, 403)


@pytest.mark.parametrize("method,path", ADMIN_ENDPOINTS)
def test_receipt_capability_token_is_rejected(client, method, path):
    token = _token({"order_id": "o1"}, typ=TOKEN_TYPE_RECEIPT)
    assert client.request(method, path, headers=_headers(token)).status_code in (401, 403)


@pytest.mark.parametrize("method,path", ADMIN_ENDPOINTS)
def test_customer_token_is_rejected(client, method, path):
    token = _token({"customer_id": "c1", "email": "c@x.test"}, typ=TOKEN_TYPE_CUSTOMER)
    assert client.request(method, path, headers=_headers(token)).status_code in (401, 403)


@pytest.mark.parametrize("method,path", ADMIN_ENDPOINTS)
def test_staff_token_without_role_claim_is_rejected(client, method, path):
    token = _token({"sub": "u1", "email": "u@x.test"}, typ=TOKEN_TYPE_STAFF)
    assert client.request(method, path, headers=_headers(token)).status_code == 403


@pytest.mark.parametrize("method,path", ADMIN_ENDPOINTS)
def test_staff_token_with_unknown_role_is_rejected(client, method, path):
    token = _token({"sub": "u1", "role": "superuser"}, typ=TOKEN_TYPE_STAFF)
    assert client.request(method, path, headers=_headers(token)).status_code == 403


@pytest.mark.parametrize("method,path", ADMIN_ENDPOINTS)
def test_staff_token_missing_subject_is_rejected(client, method, path):
    token = _token({"role": "admin"}, typ=TOKEN_TYPE_STAFF)
    assert client.request(method, path, headers=_headers(token)).status_code == 401


@pytest.mark.parametrize("method,path", PII_ENDPOINTS)
def test_plain_staff_role_is_rejected_on_pii_endpoints(client, method, path):
    token = _token({"sub": "u1", "role": "staff"}, typ=TOKEN_TYPE_STAFF)
    assert client.request(method, path, headers=_headers(token)).status_code == 403


@pytest.mark.parametrize("method,path", STAFF_TIER_ENDPOINTS)
def test_support_role_is_accepted_on_staff_tier_endpoints(client, method, path):
    token = _token({"sub": "u1", "role": "support"}, typ=TOKEN_TYPE_STAFF)
    response = client.request(method, path, headers=_headers(token))
    assert response.status_code != 403, f"{path} 应放行 support 角色"


@pytest.mark.parametrize("method,path", ADMIN_ENDPOINTS)
def test_admin_role_is_accepted_everywhere(client, method, path):
    token = _token({"sub": "u1", "role": "admin"}, typ=TOKEN_TYPE_STAFF)
    response = client.request(method, path, headers=_headers(token))
    assert response.status_code not in (401, 403), (
        f"admin 应放行 {path}，实得 {response.status_code}"
    )


@pytest.mark.parametrize("method,path", PII_ENDPOINTS)
def test_support_role_is_accepted_on_pii_endpoints(client, method, path):
    token = _token({"sub": "u1", "role": "support"}, typ=TOKEN_TYPE_STAFF)
    response = client.request(method, path, headers=_headers(token))
    assert response.status_code not in (401, 403), (
        f"{path} 应放行 support 角色，实得 {response.status_code}"
    )


# ── 角色签发 ────────────────────────────────────────────────────────────


def test_support_role_is_issued_for_configured_support_ids():
    from app.auth import STAFF_ROLES, _staff_role_for_user

    settings = Settings(
        privileged_staff_ids="boss", support_staff_ids="helper1, helper2"
    )
    assert _staff_role_for_user("boss", settings) == "admin"
    assert _staff_role_for_user("helper1", settings) == "support"
    assert _staff_role_for_user("helper2", settings) == "support"
    assert _staff_role_for_user("nobody", settings) == "staff"
    assert {_staff_role_for_user(u, settings) for u in
            ("boss", "helper1", "helper2", "nobody")} <= STAFF_ROLES