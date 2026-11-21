"""数据库连接池自愈 — 启动期 DB 不可达时的后台退避重试。

线上事故 (2026-10-15): 笔记本休眠唤醒后容器内 PostgreSQL 尚未就绪，lifespan
里的 init_db 抛错被降级吞掉，app.state.pool 永久停在 None。此后该 worker 对
所有 DB 端点恒返回 503 "database not ready"（首页空白），只有重启容器才能恢复。

三层对策:

1. **后台退避重试** — :class:`PoolRecovery` 在池缺失时接管, 按指数退避
   (默认 1s 起、上限 30s) 反复尝试建池; 成功后回调 ``on_ready`` 补齐所有依赖
   DB 的装配 (支付对账 / eventsourcing / analytics / AI / 推荐灌特征 /
   ext_oservi 定时引擎), 与启动成功走同一条代码路径。
2. **按需唤醒** — 请求真的打过来时调 :meth:`PoolRecovery.kick` 立刻唤醒重试,
   不必干等下一个退避窗口 (被唤醒的这一次不累积退避)。
3. **健康检查** — 池缺失时 ``/health/ready`` 返 503 ``degraded``, 容器编排
   (docker healthcheck / K8s readinessProbe) 据此摘流并最终重启兜底。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .bootstrap import init_db

logger = logging.getLogger("hemall.db_recovery")

#: 首次重试前的等待秒数 (给 PG 一点启动时间, 避免立刻打一次必然失败的连接)
DEFAULT_INITIAL_BACKOFF_SECONDS = 1.0
#: 指数退避上限秒数
DEFAULT_MAX_BACKOFF_SECONDS = 30.0

#: on_ready 回调签名: (pool, metrics_task) -> None
OnReady = Callable[[Any, asyncio.Task], Awaitable[None]]


class PoolRecovery:
    """后台重试直到连接池建立成功。

    Parameters
    ----------
    app:
        FastAPI 实例, 用于记录 ``state.db_metrics_task``。
    settings:
        传给 ``init_factory`` 的 Settings。
    on_ready:
        建池成功后的装配回调, 签名 ``(pool, metrics_task)``。必须幂等。
    init_factory:
        建池工厂, 默认 :func:`app.bootstrap.init_db`；测试注入假实现。
    """

    def __init__(
        self,
        app: Any,
        settings: Any,
        on_ready: OnReady,
        *,
        initial_backoff: float = DEFAULT_INITIAL_BACKOFF_SECONDS,
        max_backoff: float = DEFAULT_MAX_BACKOFF_SECONDS,
        init_factory: Callable[[Any], Awaitable[tuple[Any, asyncio.Task]]] | None = None,
    ) -> None:
        self._app = app
        self._settings = settings
        self._on_ready = on_ready
        self._init_factory = init_factory or init_db
        self._initial_backoff = initial_backoff
        self._max_backoff = max_backoff
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wakeup = asyncio.Event()
        self._done = asyncio.Event()
        #: 已尝试建池的次数 (含首次失败)
        self.attempts = 0
        #: 最近一次失败原因, 供健康检查暴露
        self.last_error: str | None = None

    # ── 状态 ────────────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        """后台重试任务是否还在跑。"""
        return self._task is not None and not self._task.done()

    @property
    def recovered(self) -> bool:
        """是否已经成功建池。"""
        return self._done.is_set()

    @property
    def backoff(self) -> float:
        """当前退避间隔 (测试可读)。"""
        return self._initial_backoff

    # ── 生命周期 ────────────────────────────────────────────────────────

    def start(self) -> asyncio.Task:
        """启动后台重试任务。必须在事件循环内调用。"""
        if self.running:
            assert self._task is not None
            return self._task
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._run(), name="hemal-db-pool-recovery")
        return self._task

    def kick(self) -> None:
        """立刻唤醒一次重试 (不阻塞调用方, 任何线程可调)。"""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(self._wakeup.set)

    async def stop(self) -> None:
        """停止后台重试 (应用关停时调用)。"""
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def wait(self, timeout: float) -> bool:
        """等待恢复完成; ``timeout`` 秒内未恢复返回 False。"""
        try:
            await asyncio.wait_for(self._done.wait(), timeout)
        except TimeoutError:
            return False
        return True

    # ── 重试循环 ────────────────────────────────────────────────────────

    async def _run(self) -> None:
        delay = self._initial_backoff
        while True:
            self.attempts += 1
            try:
                pool, metrics_task = await self._init_factory(self._settings)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - DB 不可达是预期的, 继续退避重试
                self.last_error = str(exc)
                logger.warning(
                    "database still unavailable (attempt %d), retry in %.1fs: %s",
                    self.attempts,
                    delay,
                    exc,
                )
            else:
                self._app.state.db_metrics_task = metrics_task
                try:
                    await self._on_ready(pool, metrics_task)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - 池已可用, 装配失败只降级
                    logger.warning("post-recovery DB wiring failed: %s", exc)
                self.last_error = None
                logger.info("database pool recovered after %d attempt(s)", self.attempts)
                self._done.set()
                return

            # 等退避窗口; 被 kick() 唤醒则立刻重试且不累积退避
            self._wakeup.clear()
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=delay)
            except TimeoutError:
                delay = min(delay * 2, self._max_backoff)
            else:
                delay = self._initial_backoff


async def start_pool_or_recover(
    app: Any,
    settings: Any,
    on_ready: OnReady,
    *,
    initial_backoff: float = DEFAULT_INITIAL_BACKOFF_SECONDS,
    max_backoff: float = DEFAULT_MAX_BACKOFF_SECONDS,
) -> tuple[Any, PoolRecovery | None]:
    """启动期建池；不可达则挂后台自愈。

    Returns
    -------
    (pool, recovery)
        成功时 ``(pool, None)``; 失败时 ``(None, recovery)`` —— 调用方应把
        ``recovery`` 挂到 ``app.state.pool_recovery``, 供 :func:`kick_pool_recovery`
        按需唤醒。
    """
    try:
        pool, metrics_task = await init_db(settings)
    except Exception as exc:  # noqa: BLE001 - DB 不可达不阻止应用启动
        logger.warning(
            "database unavailable at startup (%s); starting background recovery with %.1fs backoff",
            exc,
            initial_backoff,
        )
        recovery = PoolRecovery(
            app,
            settings,
            on_ready,
            initial_backoff=initial_backoff,
            max_backoff=max_backoff,
        )
        recovery.start()
        return None, recovery

    app.state.db_metrics_task = metrics_task
    return pool, None


def kick_pool_recovery(app: Any) -> None:
    """池缺失时唤醒后台自愈 (由请求路径同步调用, 不阻塞本次请求)。"""
    recovery = getattr(getattr(app, "state", None), "pool_recovery", None)
    if recovery is None:
        return
    recovery.kick()
