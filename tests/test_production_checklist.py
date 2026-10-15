"""docs/PRODUCTION_CHECKLIST.md 与代码的一致性门禁。

Task 3 要求把切换生产需要的每个环境变量/秘密文件写进
docs/PRODUCTION_CHECKLIST.md。文档最容易的失败模式是"代码加了新变量、文档
没跟上"，或者"文档写了代码里根本不认的变量"。这里把两者钉在一起。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.config import _SECRET_FILE_MAP, Settings

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECKLIST = REPO_ROOT / "docs" / "PRODUCTION_CHECKLIST.md"
DOC = CHECKLIST.read_text()

#: validate_production_security() 真正会拦、因而必须写进清单的变量
REQUIRED_IN_PRODUCTION = [
    "HEMALL_ENVIRONMENT",
    "HEMALL_PG_DSN",
    "HEMALL_JWT_SECRET",
    "HEMALL_WEBHOOK_SECRET",
    "HEMALL_HARDWARE_SECRET",
    "ENCRYPTION_KEY",
    "HEMALL_CORS_ORIGINS",
    "HEMALL_PRIVILEGED_STAFF_IDS",
    "HEMALL_PAYMENT_GATEWAY_PROVIDER",
]

#: 生产启动会直接拒绝的遗留开关
LEGACY_REJECTED = ["WECHAT_PAY_MCH_ID", "ALIPAY_APP_ID"]


def test_checklist_exists_and_is_substantial() -> None:
    assert CHECKLIST.is_file()
    assert len(DOC) > 3000, "清单不能退化成占位文件"


@pytest.mark.parametrize("env_name", REQUIRED_IN_PRODUCTION)
def test_required_env_var_documented(env_name: str) -> None:
    assert env_name in DOC, f"{env_name} 是生产必填项但清单里没写"


@pytest.mark.parametrize("env_name", LEGACY_REJECTED)
def test_legacy_rejected_switch_documented(env_name: str) -> None:
    assert env_name in DOC, (
        f"{env_name} 在生产会导致拒绝启动, 清单必须提醒不要设置它"
    )


@pytest.mark.parametrize("env_name", sorted(_SECRET_FILE_MAP))
def test_every_secret_file_env_documented(env_name: str) -> None:
    """代码支持的每个 *_FILE 变量都必须在清单的表格里。"""
    assert env_name in DOC, f"{env_name} 是受支持的密钥文件变量但清单里没写"


@pytest.mark.parametrize("env_name", sorted(_SECRET_FILE_MAP))
def test_every_secret_file_env_maps_to_a_real_field(env_name: str) -> None:
    """清单里的 *_FILE 不能指向已不存在的 Settings 字段。"""
    field = _SECRET_FILE_MAP[env_name]
    assert field in Settings.model_fields, (
        f"{env_name} 映射到已删除的字段 {field}"
    )


@pytest.mark.parametrize(
    "env_name",
    [
        "HEMALL_WECHAT_PAY_APPID",
        "HEMALL_WECHAT_PAY_MCHID",
        "HEMALL_WECHAT_PAY_SERIAL_NO",
        "HEMALL_WECHAT_PAY_NOTIFY_URL",
        "HEMALL_ALIPAY_APP_ID",
        "HEMALL_ALIPAY_NOTIFY_URL",
        "HEMALL_STRIPE_NOTIFY_URL",
        "HEMALL_PG_SSLMODE",
        "HEMALL_ALLOW_PUBLIC_ADMIN_REGISTRATION",
        "HEMALL_ALERT_WEBHOOK_URL",
        "OTEL_TRACES_EXPORTER",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
    ],
)
def test_other_production_env_vars_documented(env_name: str) -> None:
    assert env_name in DOC, f"{env_name} 未写进生产清单"


def test_callback_endpoints_documented() -> None:
    """四个回调入口都要给出可直接对照商户后台填的地址。"""
    for path in (
        "/api/payments/wechat/notify",
        "/api/payments/alipay/notify",
        "/api/payments/stripe/webhook",
        "/api/refunds/gateway/notify",
    ):
        assert path in DOC, f"回调地址 {path} 未写进生产清单"


def test_documents_readiness_probe_and_recovery() -> None:
    """2026-10-15 事故的两条恢复路径必须留在清单里 (运维排障要看这里)。"""
    assert "/health/ready" in DOC
    assert "background recovery retrying" in DOC
    assert "HOSTNAME=0.0.0.0" in DOC


def test_documents_runtime_role_has_no_ddl() -> None:
    assert "hemall_runtime" in DOC
    assert "grant-runtime" in DOC
    assert re.search(r"(不能|无 DDL|不能.*CREATE)", DOC), "必须写明 runtime 角色没有 DDL"


def test_checked_test_files_exist() -> None:
    """清单里引用的测试文件必须真的存在 (防止文档指向已删除的测试)。"""
    for path in re.findall(r"`(tests/[a-z0-9_]+\.py)`", DOC):
        assert (REPO_ROOT / path).is_file(), path
    for path in re.findall(r"`(web/e2e/[a-z0-9_-]+\.spec\.ts)`", DOC):
        assert (REPO_ROOT / path).is_file(), path


def test_checked_config_files_exist() -> None:
    for path in re.findall(r"`((?:docker-compose|prometheus|\.gitleaks)[\w.\-]*)`", DOC):
        assert (REPO_ROOT / path).exists(), path


def test_documented_ssh_key_generation_is_at_least_32_bytes() -> None:
    """openssl rand -base64 48 产出 64 字节, 满足 ≥32 字节的硬性要求。"""
    match = re.search(r"openssl rand -base64 (\d+)", DOC)
    assert match, "清单必须给出生成强密钥的命令"
    assert int(match.group(1)) >= 32
