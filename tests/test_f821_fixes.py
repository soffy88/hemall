"""6 处 F821 未定义名的回归锁（audit F821 项）。

每一条都在断言"代码路径真的跑得通"，而不是"ruff 干净"：修好之前这些测试
会失败或（更糟）被 ``except Exception`` 静默吞掉、返回空结果而测试照样通过。

锁住的 6 处：
  1. app/cache/service.py:168            ``score`` 未定义
  2. app/push/service.py:131/132/135     ``user_id`` 未定义
  3. app/eventsourcing/service.py:176    ``datetime`` 未定义
  4. app/ext/omodul/ignite_ghost_node.py:183  ``write_report`` 未定义
另外还锁了 app/main.py:72 的重复 import 与 cache 里吞掉编码错误的宽泛 except。
"""

from __future__ import annotations

import ast
import asyncio
import pathlib

import pytest

APP_DIR = pathlib.Path(__file__).resolve().parent.parent / "app"


# ── 1. app/cache/service.py:get_rank ──────────────────────────────────────


class _FakeRedis:
    """最小 Redis 假实现: zrevrange 返回 [(member, score), ...]。"""

    def __init__(self, payload):
        self.payload = payload

    async def zrevrange(self, key, start, end, withscores=False):
        assert withscores is True
        return self.payload


def _cache_manager(pool):
    from app.cache.service import CacheManager

    mgr = CacheManager.__new__(CacheManager)
    mgr._settings = None
    mgr._redis_url = "redis://fake"
    mgr._pool = pool
    mgr._started = True
    mgr._lock_tokens = {}
    return mgr


def test_get_rank_returns_member_score_pairs():
    from app.cache.service import CacheManager

    pool = _FakeRedis([(b"alice", 9.5), (b"bob", 3.0)])
    ranks = asyncio.run(_cache_manager(pool).get_rank("k"))
    assert ranks == [("alice", 9.5), ("bob", 3.0)]
    assert CacheManager is not None


def test_get_rank_handles_str_members_too():
    pool = _FakeRedis([("alice", 1.0)])
    assert asyncio.run(_cache_manager(pool).get_rank("k")) == [("alice", 1.0)]


def test_get_rank_does_not_swallow_unexpected_errors():
    """宽泛 except 会把真 bug 变成"排行榜永远是空的"。

    这里要求：只有明确的 Redis/解码类异常才降级为空列表，
    其它异常（这里是 RuntimeError）必须冒出来。
    """

    class _Boom:
        async def zrevrange(self, *a, **k):
            raise RuntimeError("unexpected failure")

    with pytest.raises(RuntimeError):
        asyncio.run(_cache_manager(_Boom()).get_rank("k"))


def test_get_rank_surfaces_broken_payload_shape_instead_of_returning_empty():
    """元素不是 (member, score) 二元组时必须报错，而不是静默返回 []。"""

    class _BadShape:
        async def zrevrange(self, *a, **k):
            return [b"alice"]  # 缺 score

    with pytest.raises((TypeError, ValueError)):
        asyncio.run(_cache_manager(_BadShape()).get_rank("k"))


def test_get_rank_degrades_to_empty_on_redis_error():
    import redis.asyncio as redis

    class _Down:
        async def zrevrange(self, *a, **k):
            raise redis.RedisError("connection refused")

    assert asyncio.run(_cache_manager(_Down()).get_rank("k")) == []


def test_get_rank_uses_replace_for_undecodable_member_not_silent_empty():
    """成员名不是合法 utf-8 时：用 replace 降级而不是把整个排行榜吞成空。"""

    class _BadBytes:
        async def zrevrange(self, *a, **k):
            return [(b"\xff\xfe", 1.0)]

    ranks = asyncio.run(_cache_manager(_BadBytes()).get_rank("k"))
    assert len(ranks) == 1
    assert ranks[0][1] == 1.0


def test_get_rank_returns_empty_when_cache_unavailable():
    from app.cache.service import CacheManager

    mgr = CacheManager.__new__(CacheManager)
    mgr._pool = None
    mgr._started = False
    assert asyncio.run(mgr.get_rank("k")) == []


# ── 2. app/push/service.py:register_device ────────────────────────────────


def _push_manager():
    from app.config import Settings
    from app.push.service import PushManager

    return PushManager(Settings())


def _device_token(user_id="u1", token="tok-1", platform="fcm"):
    from app.push.models import DeviceToken, PushPlatform

    return DeviceToken(
        user_id=user_id,
        device_token=token,
        platform=PushPlatform(platform),
    )


def test_register_device_registers_a_new_device():
    mgr = _push_manager()
    assert asyncio.run(mgr.register_device(_device_token())) is True
    tokens = mgr._device_tokens["u1"]
    assert len(tokens) == 1
    assert tokens[0].device_token == "tok-1"


def test_register_device_is_idempotent_for_same_platform_and_token():
    mgr = _push_manager()
    asyncio.run(mgr.register_device(_device_token()))
    asyncio.run(mgr.register_device(_device_token()))
    assert len(mgr._device_tokens["u1"]) == 1


def test_register_device_keeps_distinct_tokens_on_the_same_platform():
    """同一 platform 下不同 device_token = 不同设备，必须都留着。

    （原注释写的是"移除旧 token（同一平台）"，但代码去重条件是
    platform **且** device_token 都相同；F821 修复保持既有语义，只订正注释。）
    """
    mgr = _push_manager()
    asyncio.run(mgr.register_device(_device_token(token="phone-a")))
    asyncio.run(mgr.register_device(_device_token(token="phone-b")))
    assert [t.device_token for t in mgr._device_tokens["u1"]] == ["phone-a", "phone-b"]


def test_register_device_keeps_other_platforms():
    mgr = _push_manager()
    asyncio.run(mgr.register_device(_device_token(token="a-token", platform="fcm")))
    asyncio.run(mgr.register_device(_device_token(token="b-token", platform="apns")))
    tokens = mgr._device_tokens["u1"]
    assert {t.device_token for t in tokens} == {"a-token", "b-token"}


def test_register_device_keeps_users_separate():
    mgr = _push_manager()
    asyncio.run(mgr.register_device(_device_token(user_id="u1")))
    asyncio.run(mgr.register_device(_device_token(user_id="u2", token="tok-2")))
    assert len(mgr._device_tokens) == 2
    assert len(mgr._device_tokens["u2"]) == 1


# ── 3. app/eventsourcing/service.py:get_latest_snapshot ───────────────────


def _event_store_with_row(row):
    from app.eventsourcing.service import EventStore

    store = EventStore.__new__(EventStore)
    store._initialized = True

    class _Conn:
        async def fetchrow(self, sql, aggregate_id):
            return row

    class _Ctx:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *exc):
            return False

    class _Pool:
        def acquire(self):
            return _Ctx()

    store._pool = _Pool()
    return store


def test_get_latest_snapshot_parses_iso_created_at():
    from datetime import datetime

    store = _event_store_with_row(
        {
            "aggregate_id": "agg-1",
            "aggregate_type": "order",
            "version": 7,
            "state": '{"a": 1}',
            "created_at": "2026-10-13T05:00:00+00:00",
        }
    )
    snapshot = asyncio.run(store.get_latest_snapshot("agg-1"))
    assert snapshot is not None
    assert snapshot.version == 7
    assert isinstance(snapshot.created_at, datetime)
    assert snapshot.created_at.year == 2026


def test_get_latest_snapshot_returns_none_when_missing():
    store = _event_store_with_row(None)
    assert asyncio.run(store.get_latest_snapshot("agg-1")) is None


# ── 4. app/ext/omodul/ignite_ghost_node.py:write_report ───────────────────


def test_ignite_ghost_node_module_imports_write_report():
    """报告写入是点火流程的产出步骤，名字没导入 = 成功路径直接 NameError。"""
    import app.ext.omodul.ignite_ghost_node as mod

    assert hasattr(mod, "write_report"), "ignite_ghost_node 未导入 write_report"
    assert callable(mod.write_report)


def test_ignite_ghost_node_reports_write_report_in_source():
    source = (APP_DIR / "ext" / "omodul" / "ignite_ghost_node.py").read_text()
    tree = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "write_report" in imported


# ── 5. app/main.py 重复 import ───────────────────────────────────────────


def test_main_has_no_duplicate_module_imports():
    """main.py 顶部把各 router/service 一路 `as router` / `as xxx_service` 别名导入，
    所以按 (module, alias) 而不是按裸名字去重。
    """
    source = (APP_DIR / "main.py").read_text()
    tree = ast.parse(source)
    seen: set[tuple[str | None, str]] = set()
    duplicates: list[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        module = getattr(node, "module", None) or ""
        for alias in node.names:
            key = (module, alias.asname or alias.name)
            if key in seen:
                duplicates.append(f"{module}.{alias.asname or alias.name}")
            seen.add(key)
    assert duplicates == [], f"main.py 重复 import: {duplicates}"


def test_cache_manager_imported_exactly_once_in_main():
    source = (APP_DIR / "main.py").read_text()
    assert source.count("from .cache.service import CacheManager") == 1


# ── 6. 全仓 F821 归零（防止新增）────────────────────────────────────────


def _repo_files() -> list[pathlib.Path]:
    return sorted(p for p in APP_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def test_no_f821_anywhere_in_app():
    """跑 ruff --select F821，仓库必须干净。"""
    import subprocess

    root = APP_DIR.parent
    ruff = root / ".venv" / "bin" / "ruff"
    if not ruff.exists():
        pytest.skip("ruff not available in this venv")
    proc = subprocess.run(
        [str(ruff), "check", "app/", "--select", "F821"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"F821 未清零:\n{proc.stdout}"


def test_push_register_device_binds_user_id_from_the_device_token():
    """register_device 里的 `user_id` 必须来自 device_token，而不是凭空引用。"""
    import app.push.service as push_service

    source = pathlib.Path(push_service.__file__).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "register_device":
            loaded = {
                n.id
                for n in ast.walk(node)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
            }
            assigned = {
                n.id
                for n in ast.walk(node)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
            }
            assert "user_id" in assigned, "register_device 仍在裸引用未定义的 user_id"
            assert "user_id" not in (loaded - assigned)
            return
    pytest.fail("register_device not found")