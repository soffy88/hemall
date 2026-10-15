"""数据库最小权限 + 秘密文件化测试 (Task 1 核验补测)。

覆盖:
1. Settings 支持 *_FILE 变量读取 (jwt/pg_password/encryption_key/webhook/hardware/支付密钥)
2. 显式环境变量优先于 *_FILE; 秘密文件缺失时 fail-closed
3. PG 连接非本机/非容器内网时默认 sslmode=require, 可显式关闭
4. grant-runtime 后 runtime 角色执行 CREATE/DROP/ALTER TABLE 被拒 (TEST_PG_DSN 真测)
5. grant-runtime 幂等; runtime 角色 DML (SELECT/INSERT/UPDATE/DELETE) 可用
6. docker-compose.mall-sxueji.yml 含一次性 migrate/grant-runtime 服务与 docker secrets
7. production 下连接池 SET ROLE hemall_runtime (TEST_PG_DSN 真测)
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from obase.persistence.pool import PgPool

from app.cli import GRANT_RUNTIME_SQL, RUNTIME_ROLE
from app.config import Settings

TEST_DSN = os.environ.get("TEST_PG_DSN")
_needs_pg = pytest.mark.skipif(not TEST_DSN, reason="TEST_PG_DSN not set; skipping PG integration")

# 有效 Fernet key (32 字节 urlsafe base64), 用于 ENCRYPTION_KEY_FILE 测试
_FERNET_KEY = "ZmDfcTF7_60GrrY167zsiPd67pEvs0aGOv2oasOM1Pg="


def _write_secret(content: str) -> str:
    """把内容写成临时秘密文件, 返回路径 (调用方负责删除)。"""
    fd, path = tempfile.mkstemp(suffix=".secret")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    return path


# ── *_FILE 读取测试 ─────────────────────────────────────────────────────────────


def test_settings_reads_jwt_secret_from_file():
    path = _write_secret("file-based-jwt-secret-32-bytes-min")
    try:
        os.environ["HEMALL_JWT_SECRET_FILE"] = path
        os.environ.pop("HEMALL_JWT_SECRET", None)
        try:
            assert Settings().jwt_secret == "file-based-jwt-secret-32-bytes-min"
        finally:
            os.environ.pop("HEMALL_JWT_SECRET_FILE", None)
    finally:
        Path(path).unlink(missing_ok=True)


def test_settings_reads_pg_password_from_file():
    path = _write_secret("super-secret-pg-password")
    try:
        os.environ["HEMALL_PG_PASSWORD_FILE"] = path
        os.environ.pop("HEMALL_PG_PASSWORD", None)
        try:
            assert Settings().pg_password == "super-secret-pg-password"
        finally:
            os.environ.pop("HEMALL_PG_PASSWORD_FILE", None)
    finally:
        Path(path).unlink(missing_ok=True)


def test_settings_reads_encryption_key_from_file():
    path = _write_secret(_FERNET_KEY)
    try:
        os.environ["ENCRYPTION_KEY_FILE"] = path
        os.environ.pop("ENCRYPTION_KEY", None)
        try:
            settings = Settings()
            assert settings.encryption_key == _FERNET_KEY
            # 读到的 key 必须是可直接使用的 Fernet key
            from cryptography.fernet import Fernet

            Fernet(settings.encryption_key.encode("ascii"))
        finally:
            os.environ.pop("ENCRYPTION_KEY_FILE", None)
    finally:
        Path(path).unlink(missing_ok=True)


def test_settings_reads_webhook_and_hardware_secrets_from_file():
    path = _write_secret("webhook-secret-from-file-32bytes-xx")
    path2 = _write_secret("hardware-secret-from-file-32bytes-x")
    try:
        os.environ["HEMALL_WEBHOOK_SECRET_FILE"] = path
        os.environ["HEMALL_HARDWARE_SECRET_FILE"] = path2
        os.environ.pop("HEMALL_WEBHOOK_SECRET", None)
        os.environ.pop("HEMALL_HARDWARE_SECRET", None)
        try:
            settings = Settings()
            assert settings.webhook_secret == "webhook-secret-from-file-32bytes-xx"
            assert settings.hardware_secret == "hardware-secret-from-file-32bytes-x"
        finally:
            os.environ.pop("HEMALL_WEBHOOK_SECRET_FILE", None)
            os.environ.pop("HEMALL_HARDWARE_SECRET_FILE", None)
    finally:
        Path(path).unlink(missing_ok=True)
        Path(path2).unlink(missing_ok=True)


def test_settings_reads_payment_key_from_file():
    path = _write_secret("wechat-api-v3-key-from-file-32bytes")
    try:
        os.environ["HEMALL_WECHAT_PAY_API_V3_KEY_FILE"] = path
        os.environ.pop("HEMALL_WECHAT_PAY_API_V3_KEY", None)
        try:
            assert Settings().wechat_pay_api_v3_key == "wechat-api-v3-key-from-file-32bytes"
        finally:
            os.environ.pop("HEMALL_WECHAT_PAY_API_V3_KEY_FILE", None)
    finally:
        Path(path).unlink(missing_ok=True)


def test_settings_strips_whitespace_from_secret_file():
    """docker secrets 文件常带尾随换行, 读取时必须 strip。"""
    path = _write_secret("jwt-secret-with-trailing-newline\n\n")
    try:
        os.environ["HEMALL_JWT_SECRET_FILE"] = path
        os.environ.pop("HEMALL_JWT_SECRET", None)
        try:
            assert Settings().jwt_secret == "jwt-secret-with-trailing-newline"
        finally:
            os.environ.pop("HEMALL_JWT_SECRET_FILE", None)
    finally:
        Path(path).unlink(missing_ok=True)


def test_settings_explicit_env_overrides_file():
    path = _write_secret("from-file")
    try:
        os.environ["HEMALL_JWT_SECRET_FILE"] = path
        os.environ["HEMALL_JWT_SECRET"] = "from-env"
        try:
            assert Settings().jwt_secret == "from-env"
        finally:
            os.environ.pop("HEMALL_JWT_SECRET_FILE", None)
            os.environ.pop("HEMALL_JWT_SECRET", None)
    finally:
        Path(path).unlink(missing_ok=True)


def test_settings_missing_secret_file_raises():
    os.environ["HEMALL_JWT_SECRET_FILE"] = "/nonexistent/path/secret.txt"
    os.environ.pop("HEMALL_JWT_SECRET", None)
    try:
        with pytest.raises(RuntimeError, match="Secret file not found"):
            Settings()
    finally:
        os.environ.pop("HEMALL_JWT_SECRET_FILE", None)


# ── sslmode 默认 require 测试 ────────────────────────────────────────────────────


def test_external_pg_dsn_defaults_to_sslmode_require():
    """连接非本机/非容器内网 PG 时, 未显式配置 sslmode 默认 require。"""
    settings = Settings(pg_dsn="postgresql://user:pass@external-db.example.com:5432/mydb")
    assert "sslmode=require" in settings.effective_pg_dsn


def test_external_pg_dsn_explicit_disable_is_allowed():
    """可显式关闭 (HEMALL_PG_SSLMODE=disable); 显式值被尊重。"""
    settings = Settings(
        pg_dsn="postgresql://user:pass@external-db.example.com:5432/mydb",
        pg_sslmode="disable",
    )
    assert "sslmode=disable" in settings.effective_pg_dsn


def test_internal_pg_dsn_keeps_prefer_default():
    """本机/容器内网 (localhost / compose 服务名) 保持默认 prefer, 不强求 require。"""
    for dsn in (
        "postgresql://user:pass@localhost:5432/mydb",
        "postgresql://user:pass@127.0.0.1:5432/mydb",
        "postgresql://user:pass@db:5432/mydb",
        "postgresql://user:pass@pg.internal:5432/mydb",
    ):
        assert "sslmode=prefer" in Settings(pg_dsn=dsn).effective_pg_dsn


def test_dsn_with_explicit_sslmode_is_not_duplicated():
    settings = Settings(
        pg_dsn="postgresql://user:pass@localhost:5432/mydb?sslmode=verify-full",
        pg_sslmode="prefer",
    )
    dsn = settings.effective_pg_dsn
    assert "sslmode=verify-full" in dsn
    assert dsn.count("sslmode=") == 1


def test_pg_password_from_file_replaces_dsn_password():
    path = _write_secret("s3cret-from-file")
    try:
        os.environ["HEMALL_PG_PASSWORD_FILE"] = path
        os.environ.pop("HEMALL_PG_PASSWORD", None)
        try:
            settings = Settings(pg_dsn="postgresql://user:placeholder@localhost:5432/mydb")
            dsn = settings.effective_pg_dsn
            assert "s3cret-from-file" in dsn
            assert "placeholder" not in dsn
        finally:
            os.environ.pop("HEMALL_PG_PASSWORD_FILE", None)
    finally:
        Path(path).unlink(missing_ok=True)


def test_production_external_pg_without_sslmode_is_rejected():
    """production 外部连接 sslmode 非 require/verify-* → 拒绝启动。"""
    settings = Settings(
        environment="production",
        pg_dsn="postgresql://user:pass@external-db.example.com:5432/mydb",
        pg_sslmode="disable",
        jwt_secret="x" * 40,
        webhook_secret="y" * 40,
        hardware_secret="z" * 40,
        encryption_key=_FERNET_KEY,
        privileged_staff_ids="11111111-1111-1111-1111-111111111111",
        payment_gateway_provider="wechat",
        wechat_pay_appid="wxapp",
        wechat_pay_mchid="mch",
        wechat_pay_serial_no="serial",
        wechat_pay_private_key_path="/tmp/k.pem",
        wechat_pay_api_v3_key="k" * 32,
        wechat_pay_platform_cert_path="/tmp/c.pem",
    )
    with pytest.raises(RuntimeError, match="HEMALL_PG_SSLMODE"):
        settings.validate_production_security()


def test_production_internal_pg_prefer_is_accepted():
    """production 容器内网连接允许 prefer (内网信任, 不强求 TLS)。"""
    settings = Settings(
        environment="production",
        pg_dsn="postgresql://user:pass@db:5432/mydb",
        pg_sslmode="prefer",
        jwt_secret="x" * 40,
        webhook_secret="y" * 40,
        hardware_secret="z" * 40,
        encryption_key=_FERNET_KEY,
        privileged_staff_ids="11111111-1111-1111-1111-111111111111",
        payment_gateway_provider="wechat",
        wechat_pay_appid="wxapp",
        wechat_pay_mchid="mch",
        wechat_pay_serial_no="serial",
        wechat_pay_private_key_path="/tmp/k.pem",
        wechat_pay_api_v3_key="k" * 32,
        wechat_pay_platform_cert_path="/tmp/c.pem",
    )
    # 不得抛; 且内网连接不会被强制升级成 require
    settings.validate_production_security()
    assert "sslmode=prefer" in settings.effective_pg_dsn


# ── Runtime 角色权限测试 (需真实 PG) ─────────────────────────────────────────────


@pytest.fixture
async def pg_pool():
    pool = await PgPool.create(name="hemall_test_roles", dsn=TEST_DSN, min_size=1, max_size=3)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def granted_runtime(pg_pool: PgPool):
    """执行 grant-runtime (等价 python -m app.cli grant-runtime)。"""
    async with pg_pool.acquire() as conn:
        await conn.execute(GRANT_RUNTIME_SQL)
        from app.cli import REVOKE_DDL_SQL

        await conn.execute(REVOKE_DDL_SQL)
    return pg_pool


@_needs_pg
async def test_runtime_role_cannot_create_table(granted_runtime: PgPool):
    """grant-runtime 后, runtime 角色 CREATE TABLE (DDL) 必须被拒。"""
    async with granted_runtime.acquire() as conn:
        await conn.execute(f"SET ROLE {RUNTIME_ROLE}")
        try:
            with pytest.raises(Exception, match="permission denied"):
                await conn.execute("CREATE TABLE test_runtime_ddl_probe (id INT)")
        finally:
            await conn.execute("RESET ROLE")


@_needs_pg
async def test_runtime_role_cannot_drop_or_alter_table(granted_runtime: PgPool):
    """grant-runtime 后, runtime 角色 DROP/ALTER TABLE 必须被拒。"""
    async with granted_runtime.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS test_runtime_ddl_guard")
        await conn.execute("CREATE TABLE test_runtime_ddl_guard (id INT)")
        await conn.execute(f"SET ROLE {RUNTIME_ROLE}")
        try:
            with pytest.raises(Exception, match="permission denied|must be owner"):
                await conn.execute("DROP TABLE test_runtime_ddl_guard")
            with pytest.raises(Exception, match="permission denied|must be owner"):
                await conn.execute("ALTER TABLE test_runtime_ddl_guard ADD COLUMN name TEXT")
        finally:
            await conn.execute("RESET ROLE")
            await conn.execute("DROP TABLE test_runtime_ddl_guard")


@_needs_pg
async def test_runtime_role_dml_allowed(granted_runtime: PgPool):
    """runtime 角色保留 DML: INSERT/SELECT/UPDATE/DELETE 全部可用。"""
    async with granted_runtime.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS test_runtime_dml")
        await conn.execute("CREATE TABLE test_runtime_dml (id INT)")
        await conn.execute(f"SET ROLE {RUNTIME_ROLE}")
        try:
            await conn.execute("INSERT INTO test_runtime_dml (id) VALUES (1)")
            assert await conn.fetchval("SELECT COUNT(*) FROM test_runtime_dml") == 1
            await conn.execute("UPDATE test_runtime_dml SET id = 2 WHERE id = 1")
            assert await conn.fetchval("SELECT id FROM test_runtime_dml") == 2
            await conn.execute("DELETE FROM test_runtime_dml WHERE id = 2")
            assert await conn.fetchval("SELECT COUNT(*) FROM test_runtime_dml") == 0
        finally:
            await conn.execute("RESET ROLE")
            await conn.execute("DROP TABLE test_runtime_dml")


@_needs_pg
async def test_grant_runtime_is_idempotent(pg_pool: PgPool):
    """多次执行 grant-runtime 不应报错 (部署重跑安全)。"""
    async with pg_pool.acquire() as conn:
        await conn.execute(GRANT_RUNTIME_SQL)
        await conn.execute(GRANT_RUNTIME_SQL)
        # 幂等落地: 角色存在且仍无 schema CREATE 权限
        exists = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = $1)", RUNTIME_ROLE
        )
        assert exists is True
        can_create = await conn.fetchval(
            "SELECT has_schema_privilege($1, 'public', 'CREATE')", RUNTIME_ROLE
        )
        assert can_create is False


@_needs_pg
async def test_production_pool_sets_runtime_role(granted_runtime: PgPool):
    """production 下 init_db 装配的连接池, 每次 acquire 都 SET ROLE hemall_runtime。"""
    from app.bootstrap import init_db

    settings = Settings(
        environment="production",
        pg_dsn=TEST_DSN,
        pg_pool_name="hemall_test_prod_role",
        pg_pool_min=1,
        pg_pool_max=2,
    )
    pool, task = await init_db(settings)
    try:
        async with pool.acquire() as conn:
            current = await conn.fetchval("SELECT current_user")
            assert current == RUNTIME_ROLE
    finally:
        task.cancel()
        import asyncio
        import contextlib

        with contextlib.suppress(asyncio.CancelledError):
            await task
        await pool.close()


# ── Docker Compose 验证 (静态) ───────────────────────────────────────────────────


def _compose_text() -> str:
    return (Path(__file__).parent.parent / "docker-compose.mall-sxueji.yml").read_text(
        encoding="utf-8"
    )


def test_docker_compose_has_migrate_service():
    content = _compose_text()
    assert "migrate:" in content
    assert "app.cli" in content
    assert "migrate" in content


def test_docker_compose_has_grant_runtime_service():
    content = _compose_text()
    assert "grant-runtime:" in content
    assert "grant-runtime" in content


def test_docker_compose_uses_secrets():
    content = _compose_text()
    assert "secrets:" in content
    assert "jwt_secret:" in content
    assert "encryption_key:" in content
    assert "external: true" in content
