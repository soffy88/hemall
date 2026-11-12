"""P0-03: 支付宝生产回调验签链路与支付入口诚实展示。"""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid

import pytest
from fastapi import HTTPException

from app.config import Settings
from app.main import _webhook_protected_paths
from app.storefront import _payment_options, _resolve_store_payment_provider


def _webhook_headers(body: bytes) -> dict[str, str]:
    from app.deps import get_settings

    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex
    signature = hmac.new(get_settings().webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    return {
        "content-type": "application/x-www-form-urlencoded",
        "x-hemall-signature": signature,
        "x-hemall-timestamp": timestamp,
        "x-hemall-nonce": nonce,
    }


def test_payment_options_do_not_advertise_manual_as_online():
    settings = Settings(
        environment="staging",
        payment_gateway_provider="manual",
        manual_payment_enabled=True,
    )
    options = _payment_options(settings)
    assert options["online_payment_available"] is False
    assert options["online_provider"] is None
    assert options["methods"] == [{"code": "manual", "name": "人工确认收款", "mode": "offline"}]


def test_real_gateway_callbacks_bypass_hemall_hmac_only_for_selected_provider():
    alipay = _webhook_protected_paths(
        Settings(environment="staging", payment_gateway_provider="alipay")
    )
    assert "/payments/alipay/notify" not in alipay
    assert "/payments/wechat/notify" in alipay
    assert "/growth/douyin_callback" in alipay

    wechat = _webhook_protected_paths(
        Settings(environment="staging", payment_gateway_provider="wechat")
    )
    assert "/payments/wechat/notify" not in wechat
    assert "/payments/alipay/notify" in wechat

    manual = _webhook_protected_paths(
        Settings(environment="staging", payment_gateway_provider="manual")
    )
    assert "/payments/alipay/notify" in manual
    assert "/payments/wechat/notify" in manual


def test_nonproduction_cannot_select_unconfigured_online_provider():
    settings = Settings(
        environment="staging",
        payment_gateway_provider="manual",
        manual_payment_enabled=True,
    )
    with pytest.raises(HTTPException) as exc:
        _resolve_store_payment_provider(settings, "wechat")
    assert exc.value.status_code == 503


def test_manual_payment_switch_is_enforced():
    settings = Settings(
        environment="staging",
        payment_gateway_provider="manual",
        manual_payment_enabled=False,
    )
    with pytest.raises(HTTPException) as exc:
        _resolve_store_payment_provider(settings, None)
    assert exc.value.status_code == 503


def test_production_alipay_callback_verifies_then_settles(client, monkeypatch):
    from obase.provider_registry import ProviderRegistry

    from app.payments import router as payment_router

    class FakeGateway:
        async def verify_callback(self, headers, body):
            assert b"out_trade_no=" in body
            return {
                "app_id": "configured-app",
                "out_trade_no": "intent-1",
                "trade_no": "trade-1",
                "trade_status": "TRADE_SUCCESS",
                "total_amount": "15.00",
            }

    class FakeRegistry:
        def generic(self, category, name):
            assert (category, name) == ("payment_gateway", "alipay")
            return FakeGateway()

    settled: list[tuple] = []

    async def fake_settle(*args, **kwargs):
        settled.append((args, kwargs))
        return True

    monkeypatch.setattr(
        payment_router,
        "get_settings",
        lambda: Settings(
            environment="production",
            payment_gateway_provider="alipay",
        ),
    )
    monkeypatch.setattr(ProviderRegistry, "get", classmethod(lambda cls: FakeRegistry()))
    monkeypatch.setattr(payment_router, "_settle_payment_and_order", fake_settle)

    body = b"out_trade_no=intent-1&trade_status=TRADE_SUCCESS"
    response = client.post(
        "/payments/alipay/notify",
        content=body,
        headers=_webhook_headers(body),
    )

    assert response.status_code == 200
    assert response.text == "success"
    assert settled
    assert settled[0][1]["expected_provider"] == "alipay"
    assert settled[0][1]["paid_currency"] == "CNY"


def test_production_alipay_callback_fails_closed_on_bad_signature(client, monkeypatch):
    from obase.provider_registry import ProviderRegistry

    from app.payments import router as payment_router

    class FakeGateway:
        async def verify_callback(self, headers, body):
            return None

    class FakeRegistry:
        def generic(self, category, name):
            return FakeGateway()

    monkeypatch.setattr(
        payment_router,
        "get_settings",
        lambda: Settings(
            environment="production",
            payment_gateway_provider="alipay",
        ),
    )
    monkeypatch.setattr(ProviderRegistry, "get", classmethod(lambda cls: FakeRegistry()))

    body = b"out_trade_no=intent-1&trade_status=TRADE_SUCCESS"
    response = client.post(
        "/payments/alipay/notify",
        content=body,
        headers=_webhook_headers(body),
    )
    assert response.text == "fail"


def test_production_alipay_callback_does_not_require_custom_hmac_headers(monkeypatch):
    """支付宝真实回调只有 RSA2 表单签名，不应被本域 X-Hemall-* 中间件拦截。"""
    from fastapi.testclient import TestClient
    from obase.provider_registry import ProviderRegistry

    from app.deps import get_settings
    from app.main import create_app
    from app.payments import router as payment_router

    class FakeGateway:
        async def verify_callback(self, headers, body):
            return {
                "app_id": "configured-app",
                "out_trade_no": "intent-1",
                "trade_no": "trade-1",
                "trade_status": "TRADE_SUCCESS",
                "total_amount": "15.00",
            }

    class FakeRegistry:
        def generic(self, category, name):
            return FakeGateway()

    async def fake_settle(*args, **kwargs):
        return True

    monkeypatch.setenv("HEMALL_ENVIRONMENT", "staging")
    monkeypatch.setenv("HEMALL_PAYMENT_GATEWAY_PROVIDER", "alipay")
    get_settings.cache_clear()
    app = create_app()

    with TestClient(app) as app_client:
        monkeypatch.setattr(ProviderRegistry, "get", classmethod(lambda cls: FakeRegistry()))
        monkeypatch.setattr(payment_router, "_settle_payment_and_order", fake_settle)
        monkeypatch.setattr(
            payment_router,
            "get_settings",
            lambda: Settings(environment="production", payment_gateway_provider="alipay"),
        )
        body = b"out_trade_no=intent-1&trade_status=TRADE_SUCCESS"
        response = app_client.post(
            "/payments/alipay/notify",
            content=body,
            headers={"content-type": "application/x-www-form-urlencoded"},
        )

    assert response.status_code == 200
    assert response.text == "success"
