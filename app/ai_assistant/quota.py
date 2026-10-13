"""AI 助手成本闸 —— 按主体的调用配额 + 入参长度上限。

为什么需要
----------
``/ai/*`` 每个端点都在调 LLM（真金白银）。鉴权层只回答"是不是员工"，不回答
"这个员工今天已经烧了多少"。任何一个拿到 staff 令牌的脚本都能把账单打爆，
而进程内没有任何计数。

这里提供两道便宜的进程内闸（无需 Redis）：
  * :class:`AIQuotaGuard` —— 滑动窗口配额，按**已认证主体**计数。双窗口
    （每分钟 / 每小时）：分钟窗口挡住突发刷屏，小时窗口挡住慢速长跑。
  * :func:`enforce_message_budget` —— 入参长度上限。一次 10MB 的 prompt
    既是成本炸弹，也是把检索上下文顶掉的手段。

多实例部署下配额是**每进程**的（与 slowapi 的内存降级同一量级），要全局生效
需换 Redis 后端——这是已知的边界，写在这里而不是留给下一个人踩。
"""

from __future__ import annotations

import time
from collections import deque
from threading import Lock

from fastapi import HTTPException


class AIQuotaGuard:
    """按主体计数的双窗口调用配额（分钟 + 小时）。线程安全。"""

    def __init__(
        self,
        calls_per_minute: int = 10,
        calls_per_hour: int = 120,
        *,
        clock=time.monotonic,
    ) -> None:
        if calls_per_minute <= 0 or calls_per_hour <= 0:
            raise ValueError("quota limits must be positive")
        self._per_minute = calls_per_minute
        self._per_hour = calls_per_hour
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = Lock()

    @staticmethod
    def _key(principal_id: str) -> str:
        key = (principal_id or "").strip()
        if not key:
            # 没主体就没法计费——宁可拒绝也不能放一个"免费"主体进来。
            raise HTTPException(
                status_code=401, detail="AI quota requires an authenticated principal"
            )
        return key

    def consume(self, principal_id: str) -> bool:
        """记一次调用；超配额抛 429。返回 True 表示放行。"""
        key = self._key(principal_id)
        now = self._clock()
        hour_window = 3600.0
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            cutoff = now - hour_window
            while hits and hits[0] < cutoff:
                hits.popleft()
            per_minute = sum(1 for t in hits if t >= now - 60.0)
            if per_minute >= self._per_minute:
                raise HTTPException(
                    status_code=429,
                    detail=(
                        f"AI quota exceeded: {self._per_minute} calls/minute "
                        f"for this principal"
                    ),
                )
            if len(hits) >= self._per_hour:
                raise HTTPException(
                    status_code=429,
                    detail=(
                        f"AI quota exceeded: {self._per_hour} calls/hour "
                        f"for this principal"
                    ),
                )
            hits.append(now)
        return True

    def remaining(self, principal_id: str) -> int:
        """当前主体在本小时窗口内还剩多少次（用于 X-RateLimit 头 / 排障）。"""
        key = self._key(principal_id)
        now = self._clock()
        with self._lock:
            hits = self._hits.get(key)
            if not hits:
                return self._per_hour
            cutoff = now - 3600.0
            live = [t for t in hits if t >= cutoff]
            return max(0, self._per_hour - len(live))

    def reset(self) -> None:
        """清空计数（测试与运维用）。"""
        with self._lock:
            self._hits.clear()


#: 进程级单例。限额来自 Settings，构造时读取。
_GUARD: AIQuotaGuard | None = None


def get_quota_guard() -> AIQuotaGuard:
    global _GUARD
    if _GUARD is None:
        from app.config import Settings

        settings = Settings()
        _GUARD = AIQuotaGuard(
            calls_per_minute=settings.ai_quota_calls_per_minute,
            calls_per_hour=settings.ai_quota_calls_per_hour,
        )
    return _GUARD


def reset_quota_guard() -> None:
    global _GUARD
    _GUARD = None


def enforce_message_budget(text: str, *, max_chars: int | None = None) -> str:
    """入参长度闸：超长直接 413，不进 LLM。

    超长 prompt 既是成本炸弹（token 按字符走），也能把 RAG 检索到的上下文
    顶出窗口——后者会直接降低答案质量，是个正确性问题而非只是成本问题。
    """
    if max_chars is None:
        from app.config import Settings

        max_chars = Settings().ai_max_message_chars
    if len(text) > max_chars:
        raise HTTPException(
            status_code=413,
            detail=f"text too long: {len(text)} chars (limit {max_chars})",
        )
    return text