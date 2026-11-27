"""P0-13: /metrics 纵深防御、TOTP 开关与最小权限 SQL。"""

from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.middleware.metrics import metrics_access_allowed


def test_metrics_denied_without_token_for_public_client():
    assert not metrics_access_allowed(
        client_host="8.8.8.8",
        expected_token="",
        supplied_token="",
        allow_private=True,
    )


def test_metrics_allows_private_client_when_no_token_configured():
    assert metrics_access_allowed(
        client_host="172.18.0.5",
        expected_token="",
        supplied_token="",
        allow_private=True,
    )
    assert metrics_access_allowed(
        client_host="127.0.0.1",
        expected_token="",
        supplied_token="",
        allow_private=True,
    )


def test_metrics_token_takes_precedence():
    assert metrics_access_allowed(
        client_host="8.8.8.8",
        expected_token="metrics-secret",
        supplied_token="metrics-secret",
        allow_private=True,
    )
    assert not metrics_access_allowed(
        client_host="127.0.0.1",
        expected_token="metrics-secret",
        supplied_token="wrong",
        allow_private=True,
    )


def test_metrics_endpoint_rejects_testclient_without_token(client):
    response = client.get("/metrics")
    assert response.status_code == 403


def test_admin_totp_is_required_by_default():
    assert Settings().require_admin_totp is True


def test_runtime_role_sql_is_minimal_privilege_and_not_executed():
    sql = Path("scripts/sql/hemall-runtime-role.sql").read_text(encoding="utf-8")
    assert "CREATE ROLE hemal_runtime NOLOGIN" in sql
    assert "GRANT SELECT, INSERT, UPDATE, DELETE" in sql
    assert "REVOKE CREATE ON SCHEMA public" in sql
    assert "REVOKE TRUNCATE, REFERENCES, TRIGGER" in sql
    assert "仅生成，不在本次修复中执行" in sql
