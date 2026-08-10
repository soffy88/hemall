"""补天计划 Task 1.1 — Webhook 验签装甲 (HMAC-SHA256)。

架构师指令：在抖音回调和支付回调的 API 入口处强制注入 HMAC-SHA256 签名校验，
不合法的请求直接 403 丢弃，绝不允许触碰 omodul 层。

实现形态是纯 ASGI 中间件 (不是 FastAPI dependency / BaseHTTPMiddleware)：
拦截器在 ASGI 层把原始 body 完整读出来验签，签名不合法直接以 403 短路返回，
根本不会进入路由/依赖注入阶段——这就是"装甲"的语义：非法请求在业务代码
之前就被丢弃。验签通过后把 body 原样重新注入 receive 通道，下游 handler
(`await request.body()` / `request.form()`) 拿到的还是原始 payload，不丢失。

签名协议 (本域自建契约，README 已注明)：
    X-Hemall-Signature: hex( HMAC_SHA256(secret, raw_body_bytes) )
    X-Hemall-Timestamp: Unix 秒 (可选，提供则校验 ±300s 防重放)
    X-Hemall-Nonce:     一次性随机串 (可选，提供则进防重放去重集)

诚实的空白：真实微信支付回调用的是"微信支付平台证书验签 + wechatpay-* 头"，
支付宝用 RSA2 验签，不是这个 HMAC 契约——这两个 provider 目前是 mock，没有
真实证书可验；等真实密钥接入时把对应路径的验签逻辑替换成厂商标准即可
(中间件的路径→校验器可以按路径注册不同的 verifier，框架已经预留)。

受保护路径由 main.py 装配时显式传入 (当前: /payments/wechat/notify、
/payments/alipay/notify、/growth/douyin_callback)。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Any

from fastapi.responses import JSONResponse

logger = logging.getLogger("hemall.security.webhook")

#: 时间戳防重放窗口 (秒)。超过 ±5 分钟视为重放/时钟漂移，直接拒绝。
_REPLAY_WINDOW_SECONDS = 300
#: 防重放 nonce 去重集容量 (LRU 上限，防内存无限增长)。
_NONCE_MAX = 10_000


def verify_hmac_sha256(
    payload: bytes,
    signature: str,
    secret: str,
    *,
    timestamp: str | None = None,
    nonce: str | None = None,
    _seen_nonces: set[str] | None = None,
) -> bool:
    """校验单个请求的 HMAC-SHA256 签名。

    Args:
        payload: 原始请求体字节。
        signature: X-Hemall-Signature 头的值 (hex 小写)。
        secret: 共享密钥 (HEMALL_WEBHOOK_SECRET)。
        timestamp: X-Hemall-Timestamp (可选；提供则校验重放窗口)。
        nonce: X-Hemall-Nonce (可选；提供则进防重放去重集)。
        _seen_nonces: 进程内 nonce 去重集 (测试可注入，生产默认 None 用模块级集)。

    Returns:
        True 合法；False 签名缺失/不匹配/超重放窗口/重复 nonce。
    """
    if not signature or not secret:
        return False

    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature.lower(), expected):
        return False

    if timestamp:
        try:
            ts = int(timestamp)
        except (TypeError, ValueError):
            return False
        if abs(time.time() - ts) > _REPLAY_WINDOW_SECONDS:
            return False

    if nonce:
        cache = _seen_nonces if _seen_nonces is not None else _NONCE_CACHE
        if nonce in cache:
            return False
        cache.add(nonce)

    return True


class _BoundedNonceCache:
    """有界 FIFO 去重集：容量满后逐个淘汰最旧 nonce (而非整体清空)。

    整体 clear() 会让所有刚见过、仍在 ±300s 重放窗口内的 nonce 一次性
    失忆、可被重放；逐个淘汰只丢最老的一个, 绝大多数仍在窗口内的 nonce
    继续受去重保护。dict 自 3.7 起保证插入序, 借它实现 O(1) FIFO。
    """

    def __init__(self, maxsize: int) -> None:
        self._maxsize = maxsize
        self._data: dict[str, None] = {}

    def __contains__(self, nonce: str) -> bool:
        return nonce in self._data

    def add(self, nonce: str) -> None:
        if len(self._data) >= self._maxsize:
            self._data.pop(next(iter(self._data)))
        self._data[nonce] = None

    def __len__(self) -> int:
        return len(self._data)


#: 进程内 nonce 去重集 (有界 FIFO，见 _BoundedNonceCache)。
_NONCE_CACHE = _BoundedNonceCache(_NONCE_MAX)


class WebhookSignatureMiddleware:
    """纯 ASGI 中间件：对受保护的回调路径强制 HMAC-SHA256 验签。

    用法 (main.py)：
        app.add_middleware(
            WebhookSignatureMiddleware,
            secret=settings.webhook_secret,
            protected_paths={"/payments/wechat/notify", ...},
        )
    """

    def __init__(
        self,
        app: Any,
        *,
        secret: str,
        protected_paths: set[str],
    ) -> None:
        self.app = app
        self.secret = secret
        self.protected_paths = protected_paths

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["path"] not in self.protected_paths:
            await self.app(scope, receive, send)
            return

        # 1. 在 ASGI 层完整读 body (下游 handler 会经由 _replay_body 重新拿到)。
        body = b""
        while True:
            message = await receive()
            if message["type"] == "http.request":
                body += message.get("body", b"")
                if not message.get("more_body", False):
                    break
            else:  # http.disconnect
                return

        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }
        signature = headers.get("x-hemall-signature", "")
        timestamp = headers.get("x-hemall-timestamp", "")
        nonce = headers.get("x-hemall-nonce", "")

        if not verify_hmac_sha256(
            body,
            signature,
            self.secret,
            timestamp=timestamp,
            nonce=nonce,
        ):
            from ..middleware.metrics import WEBHOOK_SIGNATURE_REJECTED

            WEBHOOK_SIGNATURE_REJECTED.inc()
            logger.warning(
                "webhook signature rejected: path=%s client=%s",
                scope["path"],
                scope.get("client"),
            )
            response = JSONResponse(
                status_code=403,
                content={"detail": "invalid webhook signature"},
            )
            await response(scope, receive, send)
            return

        # 2. 验签通过：把 body 重新注入 receive，让下游照常读取。
        async def _replay_body() -> dict:
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, _replay_body, send)
