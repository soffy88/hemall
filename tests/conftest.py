"""hemall 测试公共 fixtures。

绝大多数测试不依赖真实 Postgres: 应用启动期 init_db 失败会优雅降级 (pool=None),
DB 端点返回 503——这本身就是被测行为之一。需要真实 DB 的集成测试见
test_integration_optional.py (无 TEST_PG_DSN 时跳过)。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from obase.crypto.util import CryptoUtil

from app.deps import get_settings
from app.main import app


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
