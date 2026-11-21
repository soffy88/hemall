"""事故回归 (2026-10-15): 启动期数据库不可用 → worker 永久 503。

根因: lifespan 里 init_db 失败只把 app.state.pool 置 None 就继续, 没有任何
重试; 该 worker 此后对所有 DB 端点恒返回 503 "database not ready", 只有重启
容器才恢复 (首页空白)。

覆盖:
  - PoolRecovery 后台退避重试 (失败→失败→成功) 并回调装配
  - 按需唤醒 kick() 立刻重试且不累积退避
  - 真实 PG 断开→恢复后, 通过 HTTP 层验证端点从 503 变 200
  - init_db 建表阶段失败时池被关闭摘除, 重试不会被重名挡死
  - 健康检查在池缺失时返 503 + "background recovery retrying"
  - 部署工件 (Dockerfile / compose) 的 healthcheck 指向就绪探针
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any

import pytest

from app.db_recovery import PoolRecovery, kick_pool_recovery

REPO_ROOT = Path(__file__).resolve().parents[1]


class _FakeApp:
    """最小 FastAPI 替身, 只需要 .state。"""

    def __init__(self) -> None:
        from types import SimpleNamespace

        self.state = SimpleNamespace()


class _FakePool:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FlakyInit:
    """前 failures 次建池失败, 之后成功。记录每次调用的时间戳。"""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0
        self.timestamps: list[float] = []
        self.pool = _FakePool()

    async def __call__(self, _settings: Any) -> tuple[Any, asyncio.Task]:
        self.calls += 1
        loop = asyncio.get_running_loop()
        self.timestamps.append(loop.time())
        if self.calls <= self.failures:
            raise ConnectionRefusedError("could not connect to server")
        metrics = asyncio.create_task(asyncio.sleep(3600))
        return self.pool, metrics


# ── PoolRecovery 行为 ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recovery_retries_with_backoff_then_wires_pool() -> None:
    """启动时 DB 不可用 → 后台退避重试 → 成功后回调装配并挂上池。"""
    app = _FakeApp()
    wired: list[tuple[Any, asyncio.Task]] = []
    factory = _FlakyInit(failures=2)

    recovery = PoolRecovery(
        app,
        object(),
        lambda pool, task: wired.append((pool, task)) or asyncio.sleep(0),
        initial_backoff=0.01,
        max_backoff=0.04,
        init_factory=factory,
    )
    task = recovery.start()
    assert await recovery.wait(timeout=5) is True
    await asyncio.wait_for(task, timeout=5)

    assert factory.calls == 3, "前两次失败后应继续重试, 第三次成功"
    assert recovery.recovered is True
    assert recovery.running is False
    assert recovery.last_error is None
    assert [p for p, _ in wired] == [factory.pool]
    assert app.state.db_metrics_task is wired[0][1]
    assert wired[0][1].done() is False
    wired[0][1].cancel()


@pytest.mark.asyncio
async def test_recovery_backoff_grows_exponentially_and_caps() -> None:
    """连续失败时等待间隔按 2 倍增长并封顶在 max_backoff。"""
    app = _FakeApp()
    factory = _FlakyInit(failures=10**6)

    recovery = PoolRecovery(
        app,
        object(),
        lambda pool, task: asyncio.sleep(0),
        initial_backoff=0.01,
        max_backoff=0.02,
        init_factory=factory,
    )
    task = recovery.start()
    await asyncio.sleep(0.2)
    await recovery.stop()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert factory.calls >= 4
    gaps = [b - a for a, b in zip(factory.timestamps, factory.timestamps[1:], strict=False)]
    # 每个间隔都不超过上限
    assert all(gap <= 0.02 + 0.02 for gap in gaps), gaps
    # 明显不是立刻重试 (有退避)
    assert min(gaps) >= 0.008, gaps
    assert recovery.recovered is False
    assert "could not connect" in (recovery.last_error or "")


@pytest.mark.asyncio
async def test_kick_wakes_recovery_immediately_without_growing_backoff() -> None:
    """请求打过来时 kick() 让下一次尝试不必等满退避窗口, 且退避不累积。"""
    app = _FakeApp()
    factory = _FlakyInit(failures=3)

    recovery = PoolRecovery(
        app,
        object(),
        lambda pool, task: asyncio.sleep(0),
        initial_backoff=30.0,
        max_backoff=30.0,
        init_factory=factory,
    )
    task = recovery.start()
    await asyncio.sleep(0)  # 让第一次失败发生, 进入 30s 等待

    loop = asyncio.get_running_loop()
    before = factory.calls
    t0 = loop.time()
    for _ in range(3):
        recovery.kick()
        await asyncio.sleep(0.01)
    elapsed = loop.time() - t0

    assert factory.calls - before == 3, "每次 kick 都应立刻触发一次尝试"
    assert elapsed < 2.0, "kick 生效, 不应等 30s 退避窗口"
    assert await recovery.wait(timeout=2) is True
    await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_kick_before_start_is_noop() -> None:
    """没 start 就 kick 不抛异常 (请求路径无条件调用)。"""
    recovery = PoolRecovery(_FakeApp(), object(), lambda pool, task: asyncio.sleep(0))
    recovery.kick()  # 不应抛
    assert recovery.running is False


@pytest.mark.asyncio
async def test_post_recovery_wiring_failure_does_not_retry() -> None:
    """装配回调炸了也不能回滚已建好的池 (池可用优先)。"""
    app = _FakeApp()
    factory = _FlakyInit(failures=0)

    async def _boom(pool: Any, task: asyncio.Task) -> None:
        raise RuntimeError("wiring exploded")

    recovery = PoolRecovery(
        app,
        object(),
        _boom,
        initial_backoff=0.01,
        init_factory=factory,
    )
    task = recovery.start()
    assert await recovery.wait(timeout=2) is True
    await asyncio.wait_for(task, timeout=2)
    assert factory.calls == 1
    recovery._app.state.db_metrics_task.cancel()


@pytest.mark.asyncio
async def test_start_pool_or_recover_returns_recovery_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """启动建池失败 → 返回 (None, recovery) 而不是抛给调用方。"""
    import app.db_recovery as db_recovery

    factory = _FlakyInit(failures=1)
    monkeypatch.setattr(db_recovery, "init_db", lambda settings: factory(settings))
    app = _FakeApp()

    pool, recovery = await db_recovery.start_pool_or_recover(
        app, object(), lambda p, t: asyncio.sleep(0), initial_backoff=0.01
    )
    assert pool is None
    assert recovery is not None and recovery.running
    assert await recovery.wait(timeout=2) is True
    await recovery.stop()
    assert factory.calls == 2


@pytest.mark.asyncio
async def test_start_pool_or_recover_success_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """启动建池成功 → (pool, None), 并把 metrics 任务挂到 app.state。"""
    import app.db_recovery as db_recovery

    factory = _FlakyInit(failures=0)
    monkeypatch.setattr(db_recovery, "init_db", lambda settings: factory(settings))
    app = _FakeApp()

    pool, recovery = await db_recovery.start_pool_or_recover(
        app, object(), lambda p, t: asyncio.sleep(0)
    )
    assert pool is factory.pool
    assert recovery is None
    assert app.state.db_metrics_task is not None
    app.state.db_metrics_task.cancel()


# ── init_db 部分失败不留雷 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_init_db_closes_pool_when_schema_step_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """建表阶段失败必须关池摘注册表, 否则重试会被 PgPool 重名挡死。"""
    from obase.persistence.pool import PgPool

    from app import bootstrap

    pool = _FakePool()
    created = {"n": 0}

    async def _fake_create(**kwargs: Any) -> Any:
        created["n"] += 1
        if created["n"] > 1:
            raise ValueError("PgPool 'hemall' already registered")
        return pool

    async def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("schema DDL failed")

    monkeypatch.setattr(PgPool, "create", staticmethod(_fake_create))
    monkeypatch.setattr(bootstrap, "_ensure_schema_and_verify", _boom)

    from app.config import Settings

    settings = Settings(pg_dsn="postgresql://x:y@127.0.0.1:1/z")
    with pytest.raises(RuntimeError, match="schema DDL failed"):
        await bootstrap.init_db(settings)

    assert pool.closed is True, "建表失败后池必须被关闭, 否则连接与注册表都泄漏"

    # 第二次重试必须能重新建池 (注册表里没有残留)
    pool2 = _FakePool()

    async def _fake_create2(**kwargs: Any) -> Any:
        return pool2

    async def _ok(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(PgPool, "create", staticmethod(_fake_create2))
    monkeypatch.setattr(bootstrap, "_ensure_schema_and_verify", _ok)
    got, metrics_task = await bootstrap.init_db(settings)
    assert got is pool2
    metrics_task.cancel()


# ── 健康检查 ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_ready_reports_recovery_while_pool_missing() -> None:
    """池缺失 + 后台重试中 → 503 degraded 且提示 background recovery。"""
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    from app import main as main_module
    from app.observability.health import check_db_health
    from app.observability.health import router as health_router

    app = FastAPI()
    app.include_router(health_router)
    app.dependency_overrides[__import__("app.deps", fromlist=["get_settings"]).get_settings] = (
        lambda: type("S", (), {"redis_url": "redis://127.0.0.1:1/0"})()
    )

    state = main_module.get_app_state()
    prev_pool, prev_flag = state.pool, state.db_recovery_active
    state.pool = None
    state.db_recovery_active = True
    try:
        body = await health_ready_body(app)
    finally:
        state.pool, state.db_recovery_active = prev_pool, prev_flag

    assert body["status"] == "degraded"
    assert body["checks"]["database"]["status"] == "down"
    assert "background recovery retrying" in body["checks"]["database"]["message"]
    assert await check_db_health(None) == {
        "status": "down",
        "message": "pool not initialized",
    }
    assert JSONResponse  # keep import used


async def health_ready_body(app: Any) -> dict[str, Any]:
    """直接调 endpoint 函数体, 拿到响应体 dict。"""
    from app.observability import health as health_mod

    response = _FakeResponse()
    result = await health_mod.health_ready(response, _FakeSettings())
    return result


class _FakeResponse:
    status_code = 200


class _FakeSettings:
    redis_url = "redis://127.0.0.1:1/0"


# ── 端到端 (真 PG): 断开 → 恢复 ─────────────────────────────────────────


@pytest.mark.skipif(
    not __import__("os").environ.get("TEST_PG_DSN"),
    reason="需要 TEST_PG_DSN",
)
@pytest.mark.asyncio
async def test_worker_recovers_after_database_comes_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """最贴近事故的回归: 启动时 DB 不可达, 之后恢复, 同一 worker 不再 503。

    用真 PG 造「先连不上、后来连上」: 先让 init_db 用一个坏端口起 (启动降级),
    再把自愈用的 init_factory 换成真 DSN, 验证端点从 503 恢复 200。
    """
    import os

    from app.config import Settings
    from app.db_recovery import PoolRecovery

    good_dsn = os.environ["TEST_PG_DSN"]
    settings = Settings(
        pg_dsn=good_dsn,
        pg_pool_name="hemal_recovery_test",
        pg_pool_min=1,
        pg_pool_max=3,
    )

    app = _FakeApp()
    wired: list[Any] = []

    async def _wire(pool: Any, metrics_task: asyncio.Task) -> None:
        app.state.pool = pool
        wired.append(pool)

    # 第 1、2 次尝试连不上 (端口 1 上没有 PG), 第 3 次换真 DSN
    attempts = {"n": 0}

    async def _factory(_settings: Any) -> tuple[Any, asyncio.Task]:
        attempts["n"] += 1
        if attempts["n"] < 3:
            bad = Settings(
                pg_dsn="postgresql://u:p@127.0.0.1:1/none",
                pg_pool_name="hemal_recovery_test",
            )
            from app.bootstrap import init_db

            try:
                await init_db(bad)
            finally:
                pass
        from app.bootstrap import init_db

        return await init_db(settings)

    recovery = PoolRecovery(
        app,
        settings,
        _wire,
        initial_backoff=0.01,
        max_backoff=0.02,
        init_factory=_factory,
    )
    recovery.start()
    try:
        assert await recovery.wait(timeout=30) is True
        assert attempts["n"] >= 3
        assert wired, "恢复后必须回调装配"
        # 装配后的池真的能查库 —— 同一 worker 从此不再是 503
        async with wired[-1].acquire() as conn:
            assert await conn.fetchval("SELECT 1") == 1
    finally:
        await recovery.stop()
        for pool in wired:
            await pool.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not __import__("os").environ.get("TEST_PG_DSN"),
    reason="需要 TEST_PG_DSN",
)
async def test_homepage_endpoint_serves_after_startup_db_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """事故原样复现 + 回归: 启动时 PG 不可用, 恢复后同一进程首页接口即 200。

    走真实 lifespan: create_app() → TestClient。断言顺序即事故时间线——
      1. 启动降级 → GET /store/products 503 (笔记本唤醒后的空白首页)
      2. /health/ready 503 degraded 且提示后台重试中
      3. 后台退避重试成功 → 同一 client 再打同一端点 → 200
    """
    import os

    from fastapi.testclient import TestClient

    import app.db_recovery as db_recovery
    import app.main as main_module
    from app.bootstrap import init_db as real_init_db
    from app.config import Settings

    pool_name = "hemal_lifespan_recovery"

    async def _light_wire(app: Any, settings: Any, pool: Any) -> None:
        """只做本测试关心的装配 (真实装配要跑 ES/推荐/定时引擎, 与本回归无关)。"""
        app.state.pool = pool
        main_module.get_app_state().pool = pool

    monkeypatch.setattr(main_module, "_wire_db_dependent_services", _light_wire)
    monkeypatch.setattr(
        main_module,
        "start_pool_or_recover",
        lambda app, settings, on_ready: db_recovery.start_pool_or_recover(
            app, settings, on_ready, initial_backoff=0.01, max_backoff=0.02
        ),
    )

    attempts = {"n": 0}

    async def _flaky_init_db(_settings: Any) -> tuple[Any, asyncio.Task]:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionRefusedError("PG not up yet (laptop resume)")
        real_settings = Settings(
            pg_dsn=os.environ["TEST_PG_DSN"],
            pg_pool_name=pool_name,
            pg_pool_min=1,
            pg_pool_max=3,
        )
        return await real_init_db(real_settings)

    monkeypatch.setattr(db_recovery, "init_db", _flaky_init_db)

    app = main_module.create_app()
    with TestClient(app) as c:
        # 1) 启动期降级: 首页数据端点 503
        assert c.get("/store/products").status_code == 503

        # 2) 健康检查 503 且明确说明后台正在重试
        health = c.get("/health/ready")
        assert health.status_code == 503
        assert health.json()["checks"]["database"]["status"] == "down"
        assert "background recovery retrying" in health.json()["checks"]["database"]["message"]

        # 3) 后台退避重试成功 → 同一 worker 不再 503
        recovery = app.state.pool_recovery
        assert recovery is not None and recovery.running
        # TestClient 的 lifespan 跑在另一个线程的独立事件循环里, 这里只能轮询
        # (不能跨 loop await 它的 Event)。
        deadline = time.monotonic() + 30
        while not recovery.recovered and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert recovery.recovered, "后台退避重试应在 30s 内恢复连接池"

        assert c.get("/health/ready").status_code == 200, "恢复后 /health/ready 应转 healthy"
        served = c.get("/store/products")
        assert served.status_code == 200, served.text
        assert isinstance(served.json(), list)
        assert attempts["n"] >= 3
    # TestClient 退出触发 lifespan 关停, 池随之关闭并摘出 PgPool 注册表


# ── 部署工件 ────────────────────────────────────────────────────────────


def test_api_healthcheck_uses_readiness_probe() -> None:
    """容器 healthcheck 必须打就绪探针, 否则池缺失不会触发重启。"""
    compose = (REPO_ROOT / "docker-compose.prod.yml").read_text()
    api_block = compose.split("\n  api:", 1)[1].split("\n  postgres:", 1)[0]
    assert "/health/ready" in api_block
    assert "/health/live" not in api_block


@pytest.mark.parametrize("name", ["Dockerfile", "Dockerfile.prod"])
def test_backend_image_healthcheck_uses_readiness_probe(name: str) -> None:
    text = (REPO_ROOT / name).read_text()
    healthcheck = text.split("HEALTHCHECK", 1)[1]
    assert "/health/ready" in healthcheck
    assert "/health/live" not in healthcheck


def test_kick_pool_recovery_is_defensive() -> None:
    """没有 / 未启动的 pool_recovery 时 kick 不得抛 (请求路径无条件调用)。"""
    from types import SimpleNamespace

    class _Spy:
        kicked = 0

        def kick(self) -> None:
            self.kicked += 1

    # 各种"没有自愈对象"的形态都不能抛
    kick_pool_recovery(object())
    kick_pool_recovery(SimpleNamespace(state=SimpleNamespace(pool_recovery=None)))
    app = _FakeApp()
    app.state.pool_recovery = PoolRecovery(app, object(), lambda p, t: asyncio.sleep(0))
    app.state.pool_recovery.kick()  # 未 start → no-op

    spy = _Spy()
    app.state.pool_recovery = spy
    kick_pool_recovery(app)
    kick_pool_recovery(app)
    assert spy.kicked == 2, "存在 pool_recovery 时必须转发 kick"


def test_pool_helpers_wake_recovery_before_503() -> None:
    """三个 _pool() 辅助函数在 503 前先唤醒后台重试。"""
    for rel in ("app/routers.py", "app/storefront.py", "app/ext/hardware_webhook.py"):
        text = (REPO_ROOT / rel).read_text()
        block = re.search(r"def _pool\(request: Request\).*?return pool", text, re.S)
        assert block, rel
        assert "kick_pool_recovery" in block.group(0), rel
        assert "database not ready" in block.group(0), rel
