"""P0-09 酒类默认下架、年龄确认与理性饮酒提示。"""

from __future__ import annotations

from datetime import date, timedelta

from app.alcohol import calculate_age, issue_age_token, verify_age_token
from app.config import Settings


def test_alcohol_defaults_to_disabled():
    settings = Settings(environment="staging")
    assert settings.alcohol_sales_enabled is False
    assert settings.alcohol_min_age == 18


def test_age_calculation_and_signed_cookie():
    today = date(2026, 11, 8)
    assert calculate_age(date(2008, 11, 8), today=today) == 18
    assert calculate_age(date(2008, 11, 9), today=today) == 17

    settings = Settings(environment="staging", jwt_secret="x" * 40)
    token = issue_age_token(settings, expires_at=4_000_000_000)
    assert verify_age_token(settings, token)
    assert not verify_age_token(settings, token + "x")
    assert not verify_age_token(settings, "1.deadbeef")


def test_age_verification_http_flow(client, monkeypatch):
    cfg = client.app.state.config
    monkeypatch.setattr(cfg, "alcohol_sales_enabled", True)
    monkeypatch.setattr(cfg, "alcohol_min_age", 18)

    underage = client.post(
        "/store/alcohol/verify-age",
        json={"birth_date": (date.today() - timedelta(days=365 * 16)).isoformat()},
    )
    assert underage.status_code == 403

    adult = client.post(
        "/store/alcohol/verify-age",
        json={"birth_date": (date.today() - timedelta(days=365 * 30)).isoformat()},
    )
    assert adult.status_code == 200
    assert adult.json()["verified"] is True
    assert "未成年人禁止购买和饮酒" in adult.json()["notice"]

    status = client.get("/store/alcohol/status")
    assert status.status_code == 200
    assert status.json()["enabled"] is True
    assert status.json()["verified"] is True
