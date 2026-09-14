"""hemall 测试公共 fixtures。

绝大多数测试不依赖真实 Postgres: 应用启动期 init_db 失败会优雅降级 (pool=None),
DB 端点返回 503——这本身就是被测行为之一。需要真实 DB 的集成测试见
test_integration_optional.py (无 TEST_PG_DSN 时跳过)。
"""

from __future__ import annotations

import asyncio
import contextlib
import os

import pytest
from fastapi.testclient import TestClient
from obase.crypto.util import CryptoUtil

from app.deps import get_settings
from app.main import app


@pytest.fixture(scope="session", autouse=True)
async def _ensure_test_database_schema():
    """在启用 TEST_PG_DSN 时用生产 bootstrap 初始化真实测试库。

    各历史集成测试 fixture 只负责自己的 provider 和扩展表，但空的 CI
    PostgreSQL 还没有共享 commerce 表；直接调用 ensure_ext_schema 会在
    第一个 FK (stock_location) 处失败，并让后续 named pool setup 连锁报错。
    这里复用生产 init_db，确保共享表、扩展表、P0 表和本地补丁列以同一套
    DDL 建立；测试本身仍通过真实 asyncpg/PostgreSQL 事务执行。
    """
    test_dsn = os.environ.get("TEST_PG_DSN")
    if not test_dsn:
        yield
        return

    from app.bootstrap import init_db
    from app.config import Settings

    settings = Settings(
        pg_dsn=test_dsn,
        pg_pool_name="hemall_test_schema",
        pg_pool_min=1,
        pg_pool_max=5,
        redis_url=os.environ.get("HEMALL_REDIS_URL", "redis://localhost:6379/0"),
    )
    pool, metrics_task = await init_db(settings)
    try:
        yield
    finally:
        metrics_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await metrics_task
        await pool.close()


@pytest.fixture(autouse=True)
def _isolate_from_local_env(monkeypatch):
    """单测不依赖真实 Postgres, 也不受仓库根 .env (本地运行配置) 影响。

    强制一个不可达 DSN 并清配置缓存, 使 "DB 不可达 → 503" 这类被测行为稳定成立。
    集成测试 (test_integration_optional.py) 自带 db_client fixture, 在其后用
    monkeypatch 覆盖 HEMALL_PG_DSN, 不受此影响。
    """
    monkeypatch.setenv("HEMALL_PG_DSN", "postgresql://invalid:invalid@127.0.0.1:1/none")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client() -> TestClient:
    """触发 lifespan 的 TestClient (启动期建库失败 → 降级, 不阻止启动)。"""
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth_token() -> str:
    """用当前配置密钥签发一个合法 JWT (主体 test-user)。"""
    s = get_settings()
    return CryptoUtil.jwt_sign(
        payload={"sub": "test-user", "email": "t@t.com"},
        secret=s.jwt_secret,
        algorithm=s.jwt_algorithm,
    )


@pytest.fixture
def auth_headers(auth_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth_token}"}
