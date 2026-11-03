"""微信小程序登录配置的健康检查提示 — /health/ready 信息性检查。

覆盖（无需 AppID、无 DB、无外网）：
- AppID/Secret 均未配 → checks.wechat_mp_login.status == "unconfigured"，
  并提示配置变量名；overall 健康状态不受此检查影响（仍只由 DB/Redis 决定）。
- 两者配齐（测试用假值）→ status == "configured"。
- 只配其一 → 仍为 unconfigured（不按“半配置”放行）。

安全口径：健康检查只回配置与否的布尔状态，不回 AppID/Secret 的值；
调用 wechat-login 本身已能从 503/401 区分出同一信息，无新增泄漏面。
"""

from __future__ import annotations

from app.deps import get_settings


def _clear_mp_env(monkeypatch) -> None:
    monkeypatch.delenv("HEMALL_WECHAT_MP_APP_ID", raising=False)
    monkeypatch.delenv("HEMALL_WECHAT_MP_APP_SECRET", raising=False)
    monkeypatch.delenv("HEMALL_WECHAT_MP_APP_SECRET_FILE", raising=False)
    get_settings.cache_clear()


def test_health_ready_reports_wechat_mp_unconfigured(client, monkeypatch):
    _clear_mp_env(monkeypatch)
    body = client.get("/health/ready").json()
    check = body["checks"]["wechat_mp_login"]
    assert check["status"] == "unconfigured"
    assert "HEMALL_WECHAT_MP_APP_ID" in check["message"]


def test_health_ready_reports_wechat_mp_configured(client, monkeypatch):
    _clear_mp_env(monkeypatch)
    monkeypatch.setenv("HEMALL_WECHAT_MP_APP_ID", "wxmp-test-appid")
    monkeypatch.setenv("HEMALL_WECHAT_MP_APP_SECRET", "wxmp-test-secret")
    get_settings.cache_clear()
    body = client.get("/health/ready").json()
    assert body["checks"]["wechat_mp_login"] == {"status": "configured"}


def test_health_ready_wechat_mp_partial_config_is_unconfigured(client, monkeypatch):
    _clear_mp_env(monkeypatch)
    monkeypatch.setenv("HEMALL_WECHAT_MP_APP_ID", "wxmp-test-appid")
    get_settings.cache_clear()
    body = client.get("/health/ready").json()
    assert body["checks"]["wechat_mp_login"]["status"] == "unconfigured"


def test_health_ready_wechat_mp_check_never_degrades_overall(client, monkeypatch):
    """此检查只作提示：overall 状态仍只由 database/redis 决定。"""
    _clear_mp_env(monkeypatch)
    body = client.get("/health/ready").json()
    assert body["checks"]["database"]["status"] == "down"
    assert body["status"] == "degraded"
    assert body["checks"]["wechat_mp_login"]["status"] == "unconfigured"
