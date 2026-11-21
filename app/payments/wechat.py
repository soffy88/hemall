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

import json
import logging
import os
import secrets
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

from app.ext.payment_gateways import (
    WECHAT_API_BASE,
    aes_256_gcm_decrypt,
    load_private_key_pem,
    load_public_key_pem,
    rsa_sha256_sign,
    rsa_sha256_verify,
    wechat_sign_str,
)

logger = logging.getLogger("hemall.payments.wechat")

#: 微信支付 V3 实现（商户 API 签名 / 平台证书验签 / AES-256-GCM 解密）
#: 已落地 JSAPI 下单、回调验签解密与按商户订单号查单（签名/加解密
#: 机器复用 app/ext/payment_gateways.py）。Native 扫码收款仍走
#: app/ext/payment_gateways.py（WechatPayNativeGateway），由
#: HEMALL_PAYMENT_GATEWAY_PROVIDER + 密钥装配；退款生产分支仍未在
#: 本 provider 实现。
#:
#: 配置口径：Settings（HEMALL_WECHAT_PAY_*）优先、遗留 env 兜底。
#: 选到生产模式（配了 mchid）但配置不齐/私钥不可用时**构造即失败**，
#: 而不是运行到某个回调才 500（audit tests.md P2-2 口径延续）。
PRODUCTION_IMPLEMENTED: bool = True


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


def _load_verify_public_key(source: str) -> Any:
    """加载验签公钥：优先按公钥 PEM 读，失败则按 X.509 证书解析取公钥。

    微信平台证书下发的是证书 PEM，而 ext 的 load_public_key_pem 只认
    公钥 PEM；两种形态都要能验，路径/内联内容与 _read_pem 同口径。
    """
    try:
        return load_public_key_pem(source)
    except Exception:
        from pathlib import Path

        from cryptography import x509

        text = str(source)
        data = text.encode() if "-----BEGIN" in text else Path(text).read_bytes()
        return x509.load_pem_x509_certificate(data).public_key()


def _wechat_error_code(resp: httpx.Response) -> str:
    """微信 API v3 错误响应里的 code（取不到时用 HTTP 状态码）。"""
    try:
        return str(resp.json().get("code") or resp.status_code)
    except Exception:
        return str(resp.status_code)


def _wechat_error_message(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("message") or "wechat pay request failed")
    except Exception:
        return "wechat pay request failed"


class WeChatPayOrderResponse:
    """微信支付统一下单响应（jsapi_* 为小程序调起参数，仅 JSAPI 成功时有值）。"""

    def __init__(
        self,
        *,
        prepay_id: str | None = None,
        code_url: str | None = None,  # Native 扫码支付 URL
        err_code: str | None = None,
        err_msg: str | None = None,
        jsapi_time_stamp: str | None = None,
        jsapi_nonce_str: str | None = None,
        jsapi_package: str | None = None,
        jsapi_sign_type: str | None = None,
        jsapi_pay_sign: str | None = None,
    ) -> None:
        self.prepay_id = prepay_id
        self.code_url = code_url
        self.err_code = err_code
        self.err_msg = err_msg
        self.success = err_code is None
        self.jsapi_time_stamp = jsapi_time_stamp
        self.jsapi_nonce_str = jsapi_nonce_str
        self.jsapi_package = jsapi_package
        self.jsapi_sign_type = jsapi_sign_type
        self.jsapi_pay_sign = jsapi_pay_sign

    @property
    def jsapi_params(self) -> dict[str, str] | None:
        """wx.requestPayment 调起参数；非 JSAPI 成功下单时为 None。"""
        if not self.jsapi_pay_sign:
            return None
        return {
            "timeStamp": self.jsapi_time_stamp or "",
            "nonceStr": self.jsapi_nonce_str or "",
            "package": self.jsapi_package or "",
            "signType": self.jsapi_sign_type or "RSA",
            "paySign": self.jsapi_pay_sign,
        }


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

    def __init__(
        self,
        *,
        settings: Any = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """装配配置：优先 Settings（HEMALL_WECHAT_PAY_*），缺省回退遗留 env。

        transport 供测试注入 httpx MockTransport；生产为 None（真实网络）。
        沙箱判定不变：没配 mchid 即沙箱。选到生产模式但配置不齐 →
        构造期 fail-fast（见下方）。
        """
        if settings is not None:
            vals = {
                "appid": str(getattr(settings, "wechat_pay_appid", "") or ""),
                "mch": str(getattr(settings, "wechat_pay_mchid", "") or ""),
                "serial": str(getattr(settings, "wechat_pay_serial_no", "") or ""),
                "v3": str(getattr(settings, "wechat_pay_api_v3_key", "") or ""),
                "priv": str(getattr(settings, "wechat_pay_private_key_path", "") or ""),
                "cert": str(getattr(settings, "wechat_pay_platform_cert_path", "") or ""),
                "notify": str(getattr(settings, "wechat_pay_notify_url", "") or ""),
            }
        else:
            vals = {
                "appid": os.getenv("WECHAT_PAY_APPID", ""),
                "mch": os.getenv("WECHAT_PAY_MCH_ID", ""),
                "serial": os.getenv("WECHAT_PAY_SERIAL_NO", ""),
                "v3": os.getenv("WECHAT_PAY_API_KEY", ""),
                "priv": os.getenv("WECHAT_PAY_PRIVATE_KEY_PATH", ""),
                "cert": os.getenv("WECHAT_PAY_PLATFORM_CERT_PATH", ""),
                "notify": os.getenv("WECHAT_PAY_NOTIFY_URL", ""),
            }
        self._appid = vals["appid"]
        self._mch_id = vals["mch"]
        self._serial_no = vals["serial"]
        self._api_key = vals["v3"]
        self._private_key_source = vals["priv"]
        self._platform_cert_source = vals["cert"]
        self._notify_url = vals["notify"]
        self._transport = transport
        self._sandbox = not bool(self._mch_id)

        # 内存态订单存储 (沙箱模式)
        self._orders: dict[str, dict[str, Any]] = {}
        self._refunds: dict[str, dict[str, Any]] = {}
        self._private_key: Any = None

        # fail-fast: 选了生产模式但配置不齐/私钥不可用 —— 拒绝构造。
        # 不这么做的话，部署时一切正常，直到某个用户付款回调进来才 500，
        # 而那时订单已经创建、钱可能已经扣了。
        if not self._sandbox:
            missing = [
                name
                for name, value in (
                    ("wechat_pay_appid", self._appid),
                    ("wechat_pay_serial_no", self._serial_no),
                    ("wechat_pay_private_key_path", self._private_key_source),
                    ("wechat_pay_api_v3_key", self._api_key),
                    ("wechat_pay_notify_url", self._notify_url),
                )
                if not value
            ]
            if missing:
                # 只记缺失的配置项名，绝不回显任何值。
                logger.error(
                    "wechat pay production config incomplete, missing: %s",
                    ", ".join(missing),
                )
                raise NotImplementedError(
                    "production WeChat Pay selected but configuration is "
                    "incomplete (missing settings; see log)"
                )
            try:
                self._private_key = load_private_key_pem(self._private_key_source)
            except Exception as exc:
                raise NotImplementedError(
                    "production WeChat Pay selected but the merchant private "
                    "key (wechat_pay_private_key_path) cannot be loaded"
                ) from exc
            logger.warning("WeChatPayProvider running in REAL (non-sandbox) mode")

        if self._sandbox:
            logger.warning(
                "WeChatPayProvider running in SANDBOX mode (set WECHAT_PAY_MCH_ID for production)"
            )

    async def create_order(self, request: WeChatPayOrderRequest) -> WeChatPayOrderResponse:
        """创建微信支付订单。"""
        if self._sandbox:
            return self._sandbox_create_order(request)

        if not request.payer_openid:
            # Native 扫码下单走 app.ext 的 WechatPayNativeGateway；本
            # provider 的生产路径只实现 JSAPI（小程序/公众号内支付，
            # 必须带 payer_openid），无 openid 时不假装成功。
            raise NotImplementedError(
                "production WeChat Pay without payer_openid uses the Native "
                "gateway; WeChatPayProvider implements the JSAPI branch only"
            )
        return await self._jsapi_create_order(request)

    def _auth_header(self, method: str, url_path: str, body: str) -> str:
        """API v3 请求签名头（WECHATPAY2-SHA256-RSA2048，与网关同口径）。"""
        timestamp = str(int(time.time()))
        nonce = secrets.token_hex(16)
        signature = rsa_sha256_sign(
            self._private_key,
            wechat_sign_str(method, url_path, timestamp, nonce, body),
        )
        return (
            "WECHATPAY2-SHA256-RSA2048 "
            f'mchid="{self._mch_id}",'
            f'nonce_str="{nonce}",'
            f'signature="{signature}",'
            f'timestamp="{timestamp}",'
            f'serial_no="{getattr(self, "_serial_no", "")}"'
        )

    async def _jsapi_create_order(self, request: WeChatPayOrderRequest) -> WeChatPayOrderResponse:
        """小程序 JSAPI 下单：POST /v3/pay/transactions/jsapi。

        appid 用 wechat_pay_appid——小程序支付时它必须是**小程序的
        appid**（与 payer.openid 所属应用一致）。拿到 prepay_id 后组装
        wx.requestPayment 调起参数（paySign 用商户私钥对
        appid/timeStamp/nonceStr/package 四行签名）。微信侧错误以
        err_code/err_msg 回填，不抛裸异常给上层。
        """
        if getattr(self, "_private_key", None) is None:
            return WeChatPayOrderResponse(
                err_code="NOT_CONFIGURED",
                err_msg="wechat pay merchant private key not loaded",
            )
        url_path = "/v3/pay/transactions/jsapi"
        payload = {
            "appid": self._appid,
            "mchid": self._mch_id,
            "description": request.description,
            "out_trade_no": request.out_trade_no,
            "notify_url": self._notify_url,
            "amount": {"total": request.total_amount, "currency": "CNY"},
            "payer": {"openid": request.payer_openid},
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        headers = {
            "Authorization": self._auth_header("POST", url_path, body),
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "hemall/1.0",
        }
        try:
            async with httpx.AsyncClient(
                base_url=WECHAT_API_BASE,
                transport=getattr(self, "_transport", None),
                timeout=10.0,
            ) as client:
                resp = await client.post(url_path, content=body, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("wechat jsapi create_order request failed: %s", exc)
            return WeChatPayOrderResponse(
                err_code="REQUEST_ERROR", err_msg="wechat pay request failed"
            )
        if resp.status_code >= 300:
            return WeChatPayOrderResponse(
                err_code=_wechat_error_code(resp), err_msg=_wechat_error_message(resp)
            )
        try:
            prepay_id = str(resp.json().get("prepay_id") or "")
        except Exception:
            prepay_id = ""
        if not prepay_id:
            return WeChatPayOrderResponse(
                err_code="INVALID_RESPONSE",
                err_msg="wechat pay response missing prepay_id",
            )
        time_stamp = str(int(time.time()))
        nonce_str = secrets.token_hex(16)
        package = f"prepay_id={prepay_id}"
        pay_sign = rsa_sha256_sign(
            self._private_key,
            f"{self._appid}\n{time_stamp}\n{nonce_str}\n{package}\n",
        )
        return WeChatPayOrderResponse(
            prepay_id=prepay_id,
            jsapi_time_stamp=time_stamp,
            jsapi_nonce_str=nonce_str,
            jsapi_package=package,
            jsapi_sign_type="RSA",
            jsapi_pay_sign=pay_sign,
        )

    async def handle_notify(self, headers: dict[str, str], body: bytes) -> WeChatPayNotify | None:
        """处理微信支付回调通知。"""
        if self._sandbox:
            return self._sandbox_handle_notify(headers, body)

        return self._production_handle_notify(headers, body)

    def _production_handle_notify(
        self, headers: dict[str, str], body: bytes
    ) -> WeChatPayNotify | None:
        """生产回调：平台证书验签 + APIv3 密钥 AES-256-GCM 解密。

        任一环节失败（证书未配/时间戳超 300s 窗/验签不过/解密失败）→
        返回 None 并记日志，绝不返回"验签通过"的对象——假成功等于
        订单被白嫖。平台证书每次现读（wechat_pay_platform_cert_path，
        可为路径或 PEM 内容），跟随证书轮换落盘更新。
        """
        cert_source = getattr(self, "_platform_cert_source", "")
        if not cert_source:
            logger.warning("wechat notify dropped: platform cert not configured")
            return None
        normalized = {str(k).lower(): str(v) for k, v in headers.items()}
        timestamp = normalized.get("wechatpay-timestamp", "")
        try:
            if abs(int(time.time()) - int(timestamp)) > 300:
                logger.warning("wechat notify dropped: timestamp outside 300s window")
                return None
        except ValueError:
            logger.warning("wechat notify dropped: bad timestamp header")
            return None
        if not self._verify_notify_signature(headers, body, cert_source):
            logger.warning("wechat notify dropped: signature verification failed")
            return None
        try:
            envelope = json.loads(body.decode("utf-8"))
            resource = envelope["resource"]
            plaintext = aes_256_gcm_decrypt(
                getattr(self, "_api_key", ""),
                str(resource["nonce"]),
                str(resource["ciphertext"]),
                str(resource.get("associated_data") or ""),
            )
            data = json.loads(plaintext)
        except Exception as exc:
            logger.warning("wechat notify dropped: decrypt/parse failed: %s", exc)
            return None
        amount = data.get("amount") or {}
        return WeChatPayNotify(
            out_trade_no=str(data.get("out_trade_no") or ""),
            transaction_id=str(data.get("transaction_id") or ""),
            trade_state=str(data.get("trade_state") or ""),
            amount=int(amount.get("total") or 0),
            success_time=data.get("success_time"),
        )

    async def query_order(self, out_trade_no: str) -> dict[str, Any]:
        """查询订单状态。"""
        if self._sandbox:
            order = self._orders.get(out_trade_no)
            if order is None:
                return {"trade_state": "NOTPAY"}
            return order

        if getattr(self, "_private_key", None) is None:
            return {
                "err_code": "NOT_CONFIGURED",
                "err_msg": "wechat pay merchant private key not loaded",
            }
        url_path = f"/v3/pay/transactions/out-trade-no/{out_trade_no}?mchid={self._mch_id}"
        headers = {
            "Authorization": self._auth_header("GET", url_path, ""),
            "Accept": "application/json",
            "User-Agent": "hemall/1.0",
        }
        try:
            async with httpx.AsyncClient(
                base_url=WECHAT_API_BASE,
                transport=getattr(self, "_transport", None),
                timeout=10.0,
            ) as client:
                resp = await client.get(url_path, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("wechat query_order request failed: %s", exc)
            return {"err_code": "REQUEST_ERROR", "err_msg": "wechat pay query failed"}
        if resp.status_code >= 300:
            return {
                "err_code": _wechat_error_code(resp),
                "err_msg": _wechat_error_message(resp),
            }
        return dict(resp.json())

    async def refund(
        self, *, out_trade_no: str, out_refund_no: str, refund_amount: int
    ) -> dict[str, Any]:
        """申请退款。"""
        if self._sandbox:
            if out_refund_no in self._refunds:
                return self._refunds[out_refund_no]
            order = self._orders.get(out_trade_no)
            if order is None:
                return {"status": "error", "message": "order not found"}
            order["refund_amount"] = refund_amount
            order["refund_status"] = "SUCCESS"
            result = {
                "status": "SUCCESS",
                "refund_id": f"refund_{out_refund_no[-12:]}",
                "out_refund_no": out_refund_no,
            }
            self._refunds[out_refund_no] = result
            return result

        raise NotImplementedError("production refund not yet implemented")

    # ── 沙箱实现 ──────────────────────────────────────────────────

    def _sandbox_create_order(self, request: WeChatPayOrderRequest) -> WeChatPayOrderResponse:
        prepay_id = f"prepay_{uuid.uuid4().hex[:16]}"
        self._orders[request.out_trade_no] = {
            "out_trade_no": request.out_trade_no,
            "description": request.description,
            "total_amount": request.total_amount,
            "prepay_id": prepay_id,
            "trade_state": "NOTPAY",
            "created_at": datetime.now(UTC).isoformat(),
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
    def _verify_notify_signature(headers: dict[str, str], body: bytes, platform_cert: str) -> bool:
        """用平台证书公钥验 Wechatpay-Signature（签名串 timestamp/nonce/body 三行）。

        platform_cert 可为 PEM 内容或文件路径（与网关 _read_pem 同口径）；
        缺证书/缺签名头/解码失败一律 False（fail-closed）。
        """
        if not platform_cert:
            return False
        normalized = {str(k).lower(): str(v) for k, v in headers.items()}
        timestamp = normalized.get("wechatpay-timestamp", "")
        nonce = normalized.get("wechatpay-nonce", "")
        signature = normalized.get("wechatpay-signature", "")
        if not (timestamp and nonce and signature):
            return False
        try:
            public_key = _load_verify_public_key(platform_cert)
            message = f"{timestamp}\n{nonce}\n{body.decode('utf-8')}\n"
        except Exception:
            return False
        return rsa_sha256_verify(public_key, message, signature)
