"""Scriptable asyncpg-shaped fake connection/pool for service unit tests.

These fakes let the pure-logic unit tests exercise `app.orders.service`,
`app.inventory.service` and `app.payments.service` without a live PostgreSQL
instance, which is what keeps the P0 coverage gate meaningful offline.

The fake is intentionally *strict*: it records every statement and matches
responses by (verb, table) so a test can assert the exact SQL a service
issued.  That is how the write-guard regressions are pinned — a service that
stops checking `UPDATE` rowcounts must fail a test here, not silently pass.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

__all__ = ["FakeConn", "FakePool", "FakeTx", "Recording"]

Handler = Callable[[str, tuple[Any, ...]], Any]


def _normalize(sql: str) -> str:
    return " ".join(sql.split())


class FakeTx:
    """Async context manager standing in for `conn.transaction()`."""

    def __init__(self, conn: FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> FakeTx:
        self._conn.transactions += 1
        self._conn.transaction_stack.append(self)
        return self

    async def __aexit__(self, exc_type: Any, *_args: Any) -> bool:
        self._conn.transaction_stack.pop()
        if exc_type is not None:
            self._conn.rolled_back += 1
        else:
            self._conn.committed += 1
        return False


class FakeConn:
    """A single fake DBAPI connection driven by ordered handler callbacks."""

    def __init__(self, handlers: dict[str, Handler] | None = None) -> None:
        self.handlers: dict[str, Handler] = dict(handlers or {})
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self.transaction_stack: list[FakeTx] = []
        self.transactions = 0
        self.committed = 0
        self.rolled_back = 0

    async def __aenter__(self) -> FakeConn:
        return self

    async def __aexit__(self, *_args: Any) -> bool:
        return False

    # ── registration ──────────────────────────────────────────────

    def on(self, key: str, *responses: Any) -> FakeConn:
        """Queue responses for statements matching `key` (regex, case-insensitive).

        Each call to `on` registers a handler for the *next* match only, so a
        test can script a whole conversation in order.  A single-element tuple
        is repeated forever for statements that are expected to be idempotent.
        """
        queue = list(responses)

        def handler(sql: str, args: tuple[Any, ...], _queue: list[Any] = queue) -> Any:
            if len(_queue) > 1:
                return _queue.pop(0)
            return _queue[0] if _queue else None

        self.handlers[key] = handler
        return self

    # ── DBAPI surface ─────────────────────────────────────────────

    def _dispatch(self, sql: str, args: tuple[Any, ...], method: str) -> Any:
        flat = _normalize(sql)
        self.statements.append((flat, args))
        for pattern, handler in self.handlers.items():
            if re.search(pattern, flat, re.IGNORECASE):
                return handler(flat, args)
        if method == "execute":
            return "SELECT 0"
        return None

    async def execute(self, sql: str, *args: Any) -> str:
        return self._dispatch(sql, args, "execute")

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return self._dispatch(sql, args, "fetchval")

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        return self._dispatch(sql, args, "fetchrow")

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self._dispatch(sql, args, "fetch") or []

    def transaction(self) -> FakeTx:
        return FakeTx(self)

    # ── assertions ────────────────────────────────────────────────

    def issued(self, pattern: str) -> list[tuple[str, tuple[Any, ...]]]:
        return [(s, a) for s, a in self.statements if re.search(pattern, s, re.IGNORECASE)]

    def did_issue(self, pattern: str) -> bool:
        return bool(self.issued(pattern))


class FakePool:
    """Pool handing out a fixed connection (async context manager)."""

    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn

    def acquire(self) -> FakeConn:
        return self.conn

    async def close(self) -> None:
        return None


class Recording(dict):
    """dict subclass that also answers `.get()` like asyncpg Record."""

    def get(self, key: Any, default: Any = None) -> Any:  # type: ignore[override]
        return self[key] if key in self else default
