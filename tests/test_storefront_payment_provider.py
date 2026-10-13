import pytest
from fastapi import HTTPException

from app.config import Settings
from app.storefront import _resolve_store_payment_provider


def test_production_store_payment_uses_only_configured_real_provider():
    settings = Settings(environment="production", payment_gateway_provider="stripe")
    assert _resolve_store_payment_provider(settings, None) == "stripe"
    assert _resolve_store_payment_provider(settings, "stripe") == "stripe"
    with pytest.raises(HTTPException) as exc:
        _resolve_store_payment_provider(settings, "manual")
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        _resolve_store_payment_provider(settings, "wechat")
    assert exc.value.status_code == 400


def test_production_store_payment_fails_closed_when_provider_is_manual_or_unknown():
    for provider in ("manual", "bogus", ""):
        settings = Settings(environment="production", payment_gateway_provider=provider)
        with pytest.raises(HTTPException) as exc:
            _resolve_store_payment_provider(settings, None)
        assert exc.value.status_code == 503


def test_nonproduction_store_payment_rejects_unknown_provider():
    settings = Settings(environment="development", payment_gateway_provider="manual")
    with pytest.raises(HTTPException) as exc:
        _resolve_store_payment_provider(settings, "bogus")
    assert exc.value.status_code == 400
