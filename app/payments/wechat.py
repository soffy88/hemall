"""微信支付 SDK — 统一下单 + 回调验签 + 退款。

基于微信支付 V3 API (https://pay.weixin.qq.com/wiki/doc/apiv3/)。
当前实现为"可插拔 SDK 骨架 + 沙箱/手动降级"模式:

- 生产环境: 使用真实的 mch_id / api_key / cert_serial_no 调用微信 API
- 开发/测试: 自动降级为内存态 ManualWeChatPayProvider (行为对齐手动 provider)

配置环境变量:
    WECHAT_PAY_MCH_ID: 商户号
    WECHAT_PAY_API_KEY: API v3 密钥 (32 字节)
    WECHAT_PAY_CERT_SERIAL: 商户 API 证书序列号
    WECHAT_PAY_PRIVATE_KEY_PATH: 商户私钥文件路径
    WECHAT_PAY_NOTIFY_URL: 支付结果回调 URL
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

logger = logging.getLogger("hemall.payments.wechat")


# ── 统一下单请求/响应 ─────────────────────────────────────────────────


class WeChatPayOrderRequest:
    """微信支付统一下单请求。"""

    def __init__(
        self,
        *,
        out_trade_no: str,
        description: str,
        total_amount: int,  # 分
        payer_openid: str | None = None,
        attach: str | None = None,
    ) -> None:
        self.out_trade_no = out_trade_no
        self.description = description
        self.total_amount = total_amount
        self.payer_openid = payer_openid
        self.attach = attach


class WeChatPayOrderResponse:
    """微信支付统一下单响应。"""

    def __init__(
        self,
        *,
        prepay_id: str | None = None,
        code_url: str | None = None,  # Native 扫码支付 URL
        err_code: str | None = None,
        err_msg: str | None = None,
    ) -> None:
        self.prepay_id = prepay_id
        self.code_url = code_url
        self.err_code = err_code
        self.err_msg = err_msg
        self.success = err_code is None


# ── 支付回调 ────────────────────────────────────────────────────────


class WeChatPayNotify:
    """微信支付回调数据。"""

    def __init__(
        self,
        *,
        out_trade_no: str,
        transaction_id: str,
        trade_state: str,  # SUCCESS / REFUND / NOTPAY / CLOSED / REVOKED / USERPAYING / PAYERROR
        amount: int,
        success_time: str | None = None,
    ) -> None:
        self.out_trade_no = out_trade_no
        self.transaction_id = transaction_id
        self.trade_state = trade_state
        self.amount = amount
        self.success_time = success_time

    @property
    def is_success(self) -> bool:
        return self.trade_state == "SUCCESS"


# ── Provider 接口 ────────────────────────────────────────────────────


class WeChatPayProvider:
    """微信支付 Provider (生产实现骨架 + 沙箱降级)。

    生产调用需要:
    1. 商户 API 私钥签名
    2. 微信平台证书验签
    3. AES-256-GCM 解密回调通知

    当前版本: 开发环境自动降级为内存态 (ManualWeChatPayProvider 风格)。
    """

    def __init__(self) -> None:
        self._mch_id = os.getenv("WECHAT_PAY_MCH_ID", "")
        self._api_key = os.getenv("WECHAT_PAY_API_KEY", "")
        self._notify_url = os.getenv("WECHAT_PAY_NOTIFY_URL", "")
        self._sandbox = not bool(self._mch_id)

        # 内存态订单存储 (沙箱模式)
        self._orders: dict[str, dict[str, Any]] = {}

        if self._sandbox:
            logger.warning(
                "WeChatPayProvider running in SANDBOX mode "
                "(set WECHAT_PAY_MCH_ID for production)"
            )

    async def create_order(
        self, request: WeChatPayOrderRequest
    ) -> WeChatPayOrderResponse:
        """创建微信支付订单。"""
        if self._sandbox:
            return self._sandbox_create_order(request)

        # TODO: 生产实现 — 调用微信 V3 API
        # POST https://api.mch.weixin.qq.com/v3/pay/transactions/native
        raise NotImplementedError(
            "production WeChat Pay not yet implemented; "
            "set WECHAT_PAY_MCH_ID='' for sandbox mode"
        )

    async def handle_notify(
        self, headers: dict[str, str], body: bytes
    ) -> WeChatPayNotify | None:
        """处理微信支付回调通知。"""
        if self._sandbox:
            return self._sandbox_handle_notify(headers, body)

        # TODO: 生产实现 — 验签 + 解密
        raise NotImplementedError("production notify handling not yet implemented")

    async def query_order(self, out_trade_no: str) -> dict[str, Any]:
        """查询订单状态。"""
        if self._sandbox:
            order = self._orders.get(out_trade_no)
            if order is None:
                return {"trade_state": "NOTPAY"}
            return order

        raise NotImplementedError("production query not yet implemented")

    async def refund(
        self, *, out_trade_no: str, out_refund_no: str, refund_amount: int
    ) -> dict[str, Any]:
        """申请退款。"""
        if self._sandbox:
            order = self._orders.get(out_trade_no)
            if order is None:
                return {"status": "error", "message": "order not found"}
            order["refund_amount"] = refund_amount
            order["refund_status"] = "SUCCESS"
            return {"status": "SUCCESS", "refund_id": f"refund_{uuid.uuid4().hex[:12]}"}

        raise NotImplementedError("production refund not yet implemented")

    # ── 沙箱实现 ──────────────────────────────────────────────────

    def _sandbox_create_order(
        self, request: WeChatPayOrderRequest
    ) -> WeChatPayOrderResponse:
        prepay_id = f"prepay_{uuid.uuid4().hex[:16]}"
        self._orders[request.out_trade_no] = {
            "out_trade_no": request.out_trade_no,
            "description": request.description,
            "total_amount": request.total_amount,
            "prepay_id": prepay_id,
            "trade_state": "NOTPAY",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        code_url = f"weixin://wxpay/bizpayurl?pr={prepay_id}"
        return WeChatPayOrderResponse(prepay_id=prepay_id, code_url=code_url)

    def _sandbox_handle_notify(
        self, headers: dict[str, str], body: bytes
    ) -> WeChatPayNotify | None:
        try:
            data = json.loads(body)
            out_trade_no = data.get("out_trade_no", "")
            order = self._orders.get(out_trade_no)
            if order is None:
                return None
            order["trade_state"] = "SUCCESS"
            order["transaction_id"] = f"tx_{uuid.uuid4().hex[:16]}"
            return WeChatPayNotify(
                out_trade_no=out_trade_no,
                transaction_id=order["transaction_id"],
                trade_state="SUCCESS",
                amount=order["total_amount"],
            )
        except Exception as exc:
            logger.error("sandbox notify parse failed: %s", exc)
            return None

    # ── 签名/验签工具 (生产用) ────────────────────────────────────

    @staticmethod
    def _sign_message(method: str, url: str, timestamp: str, nonce: str, body: str) -> str:
        """构造微信 V3 API 签名串。"""
        message = f"{method}\n{url}\n{timestamp}\n{nonce}\n{body}\n"
        return message

    @staticmethod
    def _verify_notify_signature(
        headers: dict[str, str], body: bytes, platform_cert: str
    ) -> bool:
        """验证回调通知签名 (生产环境实现)。

        fail-closed 存根：真实验签走 app.ext.payment_gateways
        WechatPayNativeGateway.verify_callback（RSA+AES-GCM）。
        此遗留方法永不返回 True，避免误接生产。
        """
        logger.warning(
            "legacy wechat _verify_notify_signature called — fail-closed (use WechatPayNativeGateway)"
        )
        return False
