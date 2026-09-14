"""支付宝 SDK — 当面付 (扫码) + 回调验签 + 退款。

基于支付宝开放平台 API (https://opendocs.alipay.com/open/)。
当前实现为"可插拔 SDK 骨架 + 沙箱/手动降级"模式:

- 生产环境: 使用真实的 app_id / private_key / alipay_public_key 调用支付宝 API
- 开发/测试: 自动降级为内存态 ManualAlipayProvider

配置环境变量:
    ALIPAY_APP_ID: 应用 APPID
    ALIPAY_PRIVATE_KEY: 应用私钥 (RSA2)
    ALIPAY_PUBLIC_KEY: 支付宝公钥
    ALIPAY_NOTIFY_URL: 支付结果回调 URL
    ALIPAY_SANDBOX: 是否使用沙箱环境 (true/false)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("hemall.payments.alipay")


# ── 请求/响应模型 ─────────────────────────────────────────────────────


class AlipayOrderRequest:
    """支付宝当面付 (扫码) 下单请求。"""

    def __init__(
        self,
        *,
        out_trade_no: str,
        subject: str,
        total_amount: str,  # 元 (字符串, 如 "0.01")
        body: str | None = None,
        timeout_express: str = "30m",
    ) -> None:
        self.out_trade_no = out_trade_no
        self.subject = subject
        self.total_amount = total_amount
        self.body = body
        self.timeout_express = timeout_express


class AlipayOrderResponse:
    """支付宝下单响应。"""

    def __init__(
        self,
        *,
        qr_code: str | None = None,  # 扫码支付链接
        err_code: str | None = None,
        err_msg: str | None = None,
    ) -> None:
        self.qr_code = qr_code
        self.err_code = err_code
        self.err_msg = err_msg
        self.success = err_code is None


class AlipayNotify:
    """支付宝异步通知。"""

    def __init__(
        self,
        *,
        out_trade_no: str,
        trade_no: str,
        trade_status: str,  # TRADE_SUCCESS / TRADE_FINISHED / WAIT_BUYER_PAY / TRADE_CLOSED
        total_amount: str,
        gmt_payment: str | None = None,
    ) -> None:
        self.out_trade_no = out_trade_no
        self.trade_no = trade_no
        self.trade_status = trade_status
        self.total_amount = total_amount
        self.gmt_payment = gmt_payment

    @property
    def is_success(self) -> bool:
        return self.trade_status in ("TRADE_SUCCESS", "TRADE_FINISHED")


# ── Provider 实现 ────────────────────────────────────────────────────


class AlipayProvider:
    """支付宝 Provider (生产实现骨架 + 沙箱降级)。"""

    def __init__(self) -> None:
        self._app_id = os.getenv("ALIPAY_APP_ID", "")
        self._notify_url = os.getenv("ALIPAY_NOTIFY_URL", "")
        self._sandbox = not bool(self._app_id)

        # 内存态订单存储 (沙箱模式)
        self._orders: dict[str, dict[str, Any]] = {}
        self._refunds: dict[str, dict[str, Any]] = {}

        if self._sandbox:
            logger.warning(
                "AlipayProvider running in SANDBOX mode "
                "(set ALIPAY_APP_ID for production)"
            )

    async def create_order(
        self, request: AlipayOrderRequest
    ) -> AlipayOrderResponse:
        """创建支付宝当面付订单 (预下单)。"""
        if self._sandbox:
            return self._sandbox_create_order(request)

        # TODO: 生产实现 — 调用 alipay.trade.precreate
        raise NotImplementedError(
            "production Alipay not yet implemented; "
            "set ALIPAY_APP_ID='' for sandbox mode"
        )

    async def handle_notify(self, params: dict[str, str]) -> AlipayNotify | None:
        """处理支付宝异步通知。"""
        if self._sandbox:
            return self._sandbox_handle_notify(params)

        # TODO: 生产实现 — 验签
        raise NotImplementedError("production notify handling not yet implemented")

    async def query_order(self, out_trade_no: str) -> dict[str, Any]:
        """查询订单状态。"""
        if self._sandbox:
            order = self._orders.get(out_trade_no)
            if order is None:
                return {"trade_status": "WAIT_BUYER_PAY"}
            return order

        raise NotImplementedError("production query not yet implemented")

    async def refund(
        self, *, out_trade_no: str, refund_amount: str, out_request_no: str | None = None
    ) -> dict[str, Any]:
        """申请退款 (alipay.trade.refund)。"""
        if self._sandbox:
            if out_request_no and out_request_no in self._refunds:
                return self._refunds[out_request_no]
            order = self._orders.get(out_trade_no)
            if order is None:
                return {"code": "ACQ.TRADE_NOT_EXIST", "msg": "order not found"}
            order["refund_amount"] = refund_amount
            result = {
                "code": "10000",
                "msg": "Success",
                "refund_fee": refund_amount,
                "fund_change": "Y",
            }
            if out_request_no:
                self._refunds[out_request_no] = result
            return result

        raise NotImplementedError("production refund not yet implemented")

    # ── 沙箱实现 ──────────────────────────────────────────────────

    def _sandbox_create_order(
        self, request: AlipayOrderRequest
    ) -> AlipayOrderResponse:
        trade_no = f"alipay_{uuid.uuid4().hex[:16]}"
        self._orders[request.out_trade_no] = {
            "out_trade_no": request.out_trade_no,
            "trade_no": trade_no,
            "subject": request.subject,
            "total_amount": request.total_amount,
            "trade_status": "WAIT_BUYER_PAY",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        qr_code = f"https://qr.alipay.com/{trade_no}"
        return AlipayOrderResponse(qr_code=qr_code)

    def _sandbox_handle_notify(self, params: dict[str, str]) -> AlipayNotify | None:
        try:
            out_trade_no = params.get("out_trade_no", "")
            order = self._orders.get(out_trade_no)
            if order is None:
                return None
            order["trade_status"] = "TRADE_SUCCESS"
            order["gmt_payment"] = datetime.now(timezone.utc).isoformat()
            return AlipayNotify(
                out_trade_no=out_trade_no,
                trade_no=order["trade_no"],
                trade_status="TRADE_SUCCESS",
                total_amount=order["total_amount"],
            )
        except Exception as exc:
            logger.error("sandbox notify parse failed: %s", exc)
            return None

    # ── 签名/验签工具 (生产用) ────────────────────────────────────

    @staticmethod
    def _build_sign_params(params: dict[str, str]) -> str:
        """构造 RSA2 签名串 (生产环境实现)。"""
        sorted_params = sorted(params.items())
        sign_str = "&".join(f"{k}={v}" for k, v in sorted_params if v)
        return sign_str

    @staticmethod
    def _verify_notify_signature(params: dict[str, str], alipay_public_key: str) -> bool:
        """验证异步通知签名 (生产环境实现)。

        fail-closed 存根：未接支付宝公钥验签，永不返回 True。
        """
        logger.warning(
            "legacy alipay _verify_notify_signature called — fail-closed"
        )
        return False
