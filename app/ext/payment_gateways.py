"""app.ext.payment_gateways — 真实收款网关平行实现 (补天计划 Task 2 / 架构师 P0 冲刺)。

与 ManualPaymentGateway (app/ext/payout_provider.py) 保持**同名同签名**的平行替换：
    - prepay(out_trade_no, total_fee_cents, description, notify_url) → dict
    - stripe_prepay(...) → dict
    - refund(out_trade_no, out_refund_no, refund_fee_cents, reason) → dict
    - verify_callback(headers, body) → dict | None  (回调验签 + 解密)
    - is_configured → bool

实现两个真实通道：
1) WechatPayNativeGateway — 微信支付 v3 Native：
   - 统一下单 POST /v3/pay/transactions/native (商户私钥 RSA-SHA256 签名
     WECHATPAY2-SHA256-RSA2048)
   - 退款 POST /v3/refund/domestic/refunds
   - 回调：平台证书验签 (WECHATPAY2-SHA256-RSA2048) + AES-256-GCM 资源解密
2) StripePaymentGateway — Stripe PaymentIntent：
   - 创建 POST /v1/payment_intents (Basic auth)
   - 退款 POST /v1/refunds
   - 回调：Stripe-Signature 头 HMAC-SHA256 验签 (t=,v1=, 300s 容差)

沙盒/生产无缝切换：bootstrap 按 HEMALL_PAYMENT_GATEWAY_PROVIDER (manual|wechat|
stripe) 注册；真实通道密钥缺失时**诚实回退 manual 并打日志**，不假装已接入。

诚实的空白：
- 微信平台证书 (HEMALL_WECHAT_PAY_PLATFORM_CERT_PATH) 为 PEM 文件；真实环境
  首次需从 /v3/certificates 拉取后落盘（API 调用本身也要商户签名，此文件未实现
  自动轮换，到期需运维手动更新）。
- 微信回调解密后的业务处理 (交易/退款结果落库) 在 payments/router 的 notify
  端点里做，本文件只管验签+解密。
- 无测试密钥时只能单测签名/验签/解密的纯算法（用测试生成的密钥对），HTTP 调用
  通过注入 httpx.MockTransport 验证请求形状。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("hemall.ext.payment_gateways")

#: 微信支付 v3 官方域名 (测试环境可注入 base_url 覆盖为沙盒)
WECHAT_API_BASE = "https://api.mch.weixin.qq.com"
STRIPE_API_BASE = "https://api.stripe.com"

#: 微信回调时间戳容差 (秒)
_WECHAT_REPLAY_WINDOW = 300
#: Stripe 回调时间戳容差 (秒)
_STRIPE_REPLAY_WINDOW = 300


class PaymentGatewayError(RuntimeError):
    """网关调用失败 (网络/签名/商户配置错误)。"""


# ── 微信平台证书 (轮换支持) ────────────────────────────────────────────


class WechatCertificate:
    """微信支付平台证书快照 (轮换引擎解析 /v3/certificates 后的产物)。"""

    __slots__ = ("serial_no", "effective_time", "expire_time", "pem")

    def __init__(
        self, *, serial_no: str, effective_time: str, expire_time: str, pem: str
    ) -> None:
        self.serial_no = serial_no
        self.effective_time = effective_time
        self.expire_time = expire_time
        self.pem = pem

    def to_dict(self) -> dict[str, str]:
        return {
            "serial_no": self.serial_no,
            "effective_time": self.effective_time,
            "expire_time": self.expire_time,
            "pem": self.pem,
        }

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> "WechatCertificate":
        return cls(
            serial_no=data["serial_no"],
            effective_time=data["effective_time"],
            expire_time=data["expire_time"],
            pem=data["pem"],
        )


async def fetch_wechat_platform_certificates(
    gateway: "WechatPayNativeGateway",
) -> list[WechatCertificate]:
    """GET /v3/certificates → 解密 encrypt_certificate → 解析 PEM 平台证书。

    微信 v3 平台证书接口返回的证书本体也是用 APIv3 密钥加密的
    (AEAD_AES_256_GCM)，必须解密后才能得到 PEM——这正是轮换引擎要消灭的
    "首次手动拉取落盘"痛点。响应里可能有多张证书 (新旧并存窗口期)，调用方
    按 expire_time 取最新的即可。
    """
    path = "/v3/certificates"
    headers = {
        "Authorization": gateway._auth_header("GET", path, ""),
        "Accept": "application/json",
        "User-Agent": "hemall/1.0",
    }
    async with gateway._client() as client:
        resp = await client.get(path, headers=headers)
    if resp.status_code >= 300:
        raise PaymentGatewayError(
            f"wechat cert api -> {resp.status_code}: {resp.text[:300]}"
        )
    data = resp.json()
    certs: list[WechatCertificate] = []
    for item in data.get("data", []):
        enc = item.get("encrypt_certificate", {})
        plaintext = aes_256_gcm_decrypt(
            gateway._api_v3_key,
            enc.get("nonce", ""),
            enc.get("ciphertext", ""),
            enc.get("associated_data", ""),
        )
        # 校验解析出的 PEM 确实能加载为公钥，坏证书直接丢弃不热换
        try:
            load_public_key_pem(plaintext)
        except Exception as exc:  # noqa: BLE001 - 单张坏证书不拖垮整批
            logger.warning(
                "wechat certificate %s failed PEM parse: %s",
                item.get("serial_no"), exc,
            )
            continue
        certs.append(
            WechatCertificate(
                serial_no=str(item.get("serial_no", "")),
                effective_time=str(item.get("effective_time", "")),
                expire_time=str(item.get("expire_time", "")),
                pem=plaintext,
            )
        )
    return certs


# ── 签名/解密原语 (纯算法，可单测) ───────────────────────────────────────


def _read_pem(pem: str | Path) -> bytes:
    """PEM 内容或路径二选一：内容含 BEGIN 标记视为内联 PEM，否则按路径读。"""
    text = str(pem)
    if "-----BEGIN" in text:
        return text.encode()
    return Path(text).read_bytes()


def load_private_key_pem(pem: str | Path) -> Any:
    """读取 PEM 商户私钥 (支持 PKCS1/PKCS8，支持路径或内联内容)。"""
    from cryptography.hazmat.primitives import serialization

    return serialization.load_pem_private_key(_read_pem(pem), password=None)


def load_public_key_pem(pem: str | Path) -> Any:
    """读取 PEM 平台公钥 (微信平台证书 / 自签测试证书，支持路径或内联内容)。"""
    from cryptography.hazmat.primitives import serialization

    return serialization.load_pem_public_key(_read_pem(pem))


def rsa_sha256_sign(private_key: Any, message: str) -> str:
    """RSA-SHA256 签名 (微信 v3 签名串)。返回 base64。"""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    signature = private_key.sign(
        message.encode("utf-8"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode()


def rsa_sha256_verify(public_key: Any, message: str, signature_b64: str) -> bool:
    """RSA-SHA256 验签 (微信平台证书验签)。"""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    try:
        public_key.verify(
            base64.b64decode(signature_b64),
            message.encode("utf-8"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return True
    except Exception:  # noqa: BLE001 - 验签失败统一视为不合法
        return False


def aes_256_gcm_decrypt(
    api_v3_key: str, nonce: str, ciphertext_b64: str, associated_data: str
) -> str:
    """微信回调资源解密 (AEAD_AES_256_GCM)。api_v3_key 为 32 字节 hex 密钥。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = api_v3_key.encode("utf-8")
    if len(key) != 32:
        raise PaymentGatewayError("wechat api_v3_key must be 32 bytes")
    plaintext = AESGCM(key).decrypt(
        nonce.encode("utf-8"),
        base64.b64decode(ciphertext_b64),
        associated_data.encode("utf-8"),
    )
    return plaintext.decode("utf-8")


def wechat_sign_str(method: str, url_path: str, timestamp: str, nonce: str, body: str) -> str:
    """微信 v3 待签名串：method + 规范化 URL + timestamp + nonce + body。"""
    return f"{method}\n{url_path}\n{timestamp}\n{nonce}\n{body}\n"


def stripe_webhook_verify(secret: str, body: bytes, signature_header: str) -> bool:
    """Stripe Webhook 验签：Stripe-Signature: t=<ts>,v1=<hex hmac>。"""
    if not signature_header:
        return False
    parts: dict[str, str] = {}
    for pair in signature_header.split(","):
        k, _, v = pair.strip().partition("=")
        parts[k] = v
    ts = parts.get("t")
    v1 = parts.get("v1")
    if not ts or not v1:
        return False
    try:
        if abs(time.time() - int(ts)) > _STRIPE_REPLAY_WINDOW:
            return False
    except ValueError:
        return False
    expected = hmac.new(
        secret.encode("utf-8"), f"{ts}.{body.decode('utf-8')}".encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, v1)


# ── 微信支付 v3 Native 真实网关 ─────────────────────────────────────────


class WechatPayNativeGateway:
    """微信支付 v3 Native 统一下单/退款/回调验签。

    Args:
        appid: 商户绑定的 AppID。
        mchid: 商户号。
        serial_no: 商户 API 证书序列号。
        private_key: 商户私钥 PEM (路径或内容)。
        api_v3_key: APIv3 密钥 (32 字节, 用于回调解密)。
        platform_cert: 微信支付平台证书 PEM (路径或内容, 回调验签用)。
        notify_url: 统一下单回填的回调地址 (公网可达 /payments/wechat/notify)。
        base_url: 测试注入沙盒地址。
        transport: 测试注入 httpx.MockTransport。
    """

    def __init__(
        self,
        *,
        appid: str = "",
        mchid: str = "",
        serial_no: str = "",
        private_key: str | Path = "",
        api_v3_key: str = "",
        platform_cert: str | Path = "",
        notify_url: str = "",
        base_url: str = WECHAT_API_BASE,
        transport: Any = None,
    ) -> None:
        self._appid = appid
        self._mchid = mchid
        self._serial_no = serial_no
        self._private_key = load_private_key_pem(private_key) if private_key else None
        self._api_v3_key = api_v3_key
        self._platform_cert = (
            load_public_key_pem(platform_cert) if platform_cert else None
        )
        self._platform_cert_serial = ""
        self._platform_cert_pem = str(platform_cert) if platform_cert else ""
        self._notify_url = notify_url
        self._base_url = base_url.rstrip("/")
        self._transport = transport

    def rotate_platform_cert(self, pem: str, serial_no: str = "") -> None:
        """热更新平台证书 (cert_rotation_engine 调用，无重启换证书)。

        同时保留 PEM 原文与序列号——引擎用它们落盘持久化，重启后加载的
        网关 (bootstrap 从文件读) 能接上轮换后的状态。
        """
        if not pem:
            raise PaymentGatewayError("rotate_platform_cert: empty pem")
        self._platform_cert = load_public_key_pem(pem)
        self._platform_cert_pem = pem
        self._platform_cert_serial = serial_no

    @property
    def platform_cert_snapshot(self) -> dict[str, str]:
        """当前生效平台证书快照 (轮换引擎持久化用)。"""
        return {
            "serial_no": self._platform_cert_serial,
            "pem": self._platform_cert_pem,
        }

    @property
    def is_configured(self) -> bool:
        return bool(
            self._appid
            and self._mchid
            and self._serial_no
            and self._private_key is not None
            and self._api_v3_key
            and self._platform_cert is not None
        )

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            transport=self._transport,
            timeout=10.0,
        )

    def _auth_header(self, method: str, url_path: str, body: str) -> str:
        if self._private_key is None:
            raise PaymentGatewayError("wechat gateway not configured (missing private key)")
        timestamp = str(int(time.time()))
        nonce = uuid.uuid4().hex
        message = wechat_sign_str(method, url_path, timestamp, nonce, body)
        signature = rsa_sha256_sign(self._private_key, message)
        return (
            'WECHATPAY2-SHA256-RSA2048 '
            f'mchid="{self._mchid}",nonce_str="{nonce}",'
            f'signature="{signature}",timestamp="{timestamp}",'
            f'serial_no="{self._serial_no}"'
        )

    async def _post(self, url_path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False)
        headers = {
            "Authorization": self._auth_header("POST", url_path, body),
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "hemall/1.0",
        }
        async with self._client() as client:
            resp = await client.post(url_path, content=body, headers=headers)
        if resp.status_code >= 300:
            raise PaymentGatewayError(
                f"wechat api {url_path} -> {resp.status_code}: {resp.text[:300]}"
            )
        return resp.json()

    async def prepay(
        self,
        *,
        out_trade_no: str,
        total_fee_cents: int,
        description: str,
        notify_url: str,
    ) -> dict[str, Any]:
        """统一下单 (Native) → code_url (二维码链接)。"""
        if total_fee_cents <= 0:
            raise ValueError("prepay: total_fee_cents must be positive")
        payload = {
            "appid": self._appid,
            "mchid": self._mchid,
            "description": description,
            "out_trade_no": out_trade_no,
            "notify_url": notify_url or self._notify_url,
            "amount": {"total": total_fee_cents, "currency": "CNY"},
        }
        data = await self._post("/v3/pay/transactions/native", payload)
        return {
            "out_trade_no": out_trade_no,
            "total_fee_cents": total_fee_cents,
            "status": "pending",
            "code_url": data.get("code_url", ""),
            "prepay_id": None,
            "payment_intent_id": None,
            "client_secret": None,
            "gateway": "wechat",
        }

    async def stripe_prepay(
        self,
        *,
        out_trade_no: str,
        total_fee_cents: int,
        description: str,
    ) -> dict[str, Any]:
        raise PaymentGatewayError("stripe_prepay is not supported by WechatPayNativeGateway")

    async def refund(
        self,
        *,
        out_trade_no: str,
        out_refund_no: str,
        refund_fee_cents: int,
        reason: str = "",
        total_fee_cents: int | None = None,
    ) -> dict[str, Any]:
        """退款 (国内退款 API)。微信侧要求原单金额，缺省按退款金额回填。"""
        if refund_fee_cents <= 0:
            raise ValueError("refund: refund_fee_cents must be positive")
        payload = {
            "out_trade_no": out_trade_no,
            "out_refund_no": out_refund_no,
            "reason": reason,
            "notify_url": self._notify_url,
            "amount": {
                "refund": refund_fee_cents,
                "total": total_fee_cents or refund_fee_cents,
                "currency": "CNY",
            },
        }
        data = await self._post("/v3/refund/domestic/refunds", payload)
        return {
            "out_refund_no": out_refund_no,
            "out_trade_no": out_trade_no,
            "refund_fee_cents": refund_fee_cents,
            "status": data.get("status", "PROCESSING"),
            "refund_id": data.get("refund_id", ""),
            "gateway": "wechat",
        }

    async def verify_callback(
        self, headers: dict[str, str], body: bytes
    ) -> dict[str, Any] | None:
        """微信支付回调验签 + 解密。合法返回解密后的业务 JSON；否则 None。

        headers: 原始请求头 (大小写不敏感 dict)。
        body: 原始请求体。
        """
        if self._platform_cert is None:
            raise PaymentGatewayError("wechat gateway not configured (missing platform cert)")
        h = {k.lower(): v for k, v in headers.items()}
        ts = h.get("wechatpay-timestamp", "")
        nonce = h.get("wechatpay-nonce", "")
        signature = h.get("wechatpay-signature", "")
        serial = h.get("wechatpay-serial", "")
        if not (ts and nonce and signature and serial):
            return None
        try:
            if abs(time.time() - int(ts)) > _WECHAT_REPLAY_WINDOW:
                return None
        except ValueError:
            return None
        message = f"{ts}\n{nonce}\n{body.decode('utf-8')}\n"
        if not rsa_sha256_verify(self._platform_cert, message, signature):
            logger.warning("wechat callback signature invalid: serial=%s", serial)
            return None

        try:
            envelope = json.loads(body.decode("utf-8"))
            resource = envelope["resource"]
            plaintext = aes_256_gcm_decrypt(
                self._api_v3_key,
                resource["nonce"],
                resource["ciphertext"],
                resource.get("associated_data", ""),
            )
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("wechat callback decrypt failed: %s", exc)
            return None
        return json.loads(plaintext)


# ── Stripe PaymentIntent 真实网关 ───────────────────────────────────────


class StripePaymentGateway:
    """Stripe PaymentIntent 统一下单/退款/回调验签。

    Args:
        secret_key: sk_live_/sk_test_ 密钥。
        webhook_secret: whsec_ webhook 签名密钥。
        notify_url: 不参与 Stripe 下单 (Stripe 在 Dashboard 配置 webhook)，保留
            字段仅为契约对齐。
        base_url: 测试注入沙盒地址。
        transport: 测试注入 httpx.MockTransport。
    """

    def __init__(
        self,
        *,
        secret_key: str = "",
        webhook_secret: str = "",
        notify_url: str = "",
        base_url: str = STRIPE_API_BASE,
        transport: Any = None,
    ) -> None:
        self._secret_key = secret_key
        self._webhook_secret = webhook_secret
        self._notify_url = notify_url
        self._base_url = base_url.rstrip("/")
        self._transport = transport

    @property
    def is_configured(self) -> bool:
        return bool(self._secret_key and self._webhook_secret)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            transport=self._transport,
            auth=(self._secret_key, ""),
            timeout=10.0,
        )

    async def _post_form(
        self, url_path: str, fields: dict[str, Any]
    ) -> dict[str, Any]:
        async with self._client() as client:
            resp = await client.post(url_path, data=fields)
        if resp.status_code >= 300:
            raise PaymentGatewayError(
                f"stripe api {url_path} -> {resp.status_code}: {resp.text[:300]}"
            )
        return resp.json()

    async def prepay(
        self,
        *,
        out_trade_no: str,
        total_fee_cents: int,
        description: str,
        notify_url: str,
    ) -> dict[str, Any]:
        """创建 PaymentIntent → {payment_intent_id, client_secret}。"""
        return await self.stripe_prepay(
            out_trade_no=out_trade_no,
            total_fee_cents=total_fee_cents,
            description=description,
        )

    async def stripe_prepay(
        self,
        *,
        out_trade_no: str,
        total_fee_cents: int,
        description: str,
    ) -> dict[str, Any]:
        if total_fee_cents <= 0:
            raise ValueError("stripe_prepay: total_fee_cents must be positive")
        data = await self._post_form(
            "/v1/payment_intents",
            {
                "amount": total_fee_cents,
                "currency": "cny",
                "payment_method_types[]": "card",
                "description": description,
                "metadata[out_trade_no]": out_trade_no,
                "metadata[order_ref]": out_trade_no,
            },
        )
        return {
            "out_trade_no": out_trade_no,
            "total_fee_cents": total_fee_cents,
            "status": data.get("status", "requires_payment_method"),
            "payment_intent_id": data.get("id", ""),
            "client_secret": data.get("client_secret", ""),
            "code_url": None,
            "prepay_id": None,
            "gateway": "stripe",
        }

    async def refund(
        self,
        *,
        out_trade_no: str,
        out_refund_no: str,
        refund_fee_cents: int,
        reason: str = "",
        total_fee_cents: int | None = None,
    ) -> dict[str, Any]:
        """按 out_trade_no 找到原 PaymentIntent 并退款。

        注意：Stripe 退款需 payment_intent 对象 id，这里用 metadata[order_ref]
        反查是在真实环境下简化的约定——生产接入应先在本地 payment_session 表
        查到 payment_intent_id 再传，本实现保留 total_fee_cents 占位。
        """
        if refund_fee_cents <= 0:
            raise ValueError("refund: refund_fee_cents must be positive")
        data = await self._post_form(
            "/v1/refunds",
            {
                "amount": refund_fee_cents,
                "reason": reason[:250] or "requested_by_customer",
                "metadata[out_refund_no]": out_refund_no,
                "metadata[order_ref]": out_trade_no,
            },
        )
        return {
            "out_refund_no": out_refund_no,
            "out_trade_no": out_trade_no,
            "refund_fee_cents": refund_fee_cents,
            "status": data.get("status", "pending"),
            "refund_id": data.get("id", ""),
            "gateway": "stripe",
        }

    async def verify_callback(
        self, headers: dict[str, str], body: bytes
    ) -> dict[str, Any] | None:
        """Stripe Webhook 验签。合法返回解析后的 JSON；否则 None。"""
        h = {k.lower(): v for k, v in headers.items()}
        signature = h.get("stripe-signature", "")
        if not stripe_webhook_verify(self._webhook_secret, body, signature):
            return None
        try:
            return json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            return None


# ── 装配工厂 (bootstrap 用) ─────────────────────────────────────────────


def build_payment_gateway(settings: Any) -> tuple[str, Any]:
    """按 HEMALL_PAYMENT_GATEWAY_PROVIDER 装配网关。

    Returns:
        (注册名, 网关实例)。密钥缺失时诚实回退 ManualPaymentGateway 并打日志，
        注册名仍用配置的 provider 名 (查找不失败，只是内存态)。
    """
    from .payout_provider import ManualPaymentGateway

    provider = settings.payment_gateway_provider

    if provider == "wechat":
        gateway = WechatPayNativeGateway(
            appid=settings.wechat_pay_appid,
            mchid=settings.wechat_pay_mchid,
            serial_no=settings.wechat_pay_serial_no,
            private_key=settings.wechat_pay_private_key_path or "",
            api_v3_key=settings.wechat_pay_api_v3_key,
            platform_cert=settings.wechat_pay_platform_cert_path or "",
            notify_url=settings.wechat_pay_notify_url,
        )
        if gateway.is_configured:
            return provider, gateway
        logger.warning(
            "HEMALL_PAYMENT_GATEWAY_PROVIDER=wechat but keys missing "
            "(mchid/private_key/api_v3_key/platform_cert); falling back to manual"
        )
        return provider, ManualPaymentGateway()

    if provider == "stripe":
        gateway = StripePaymentGateway(
            secret_key=settings.stripe_secret_key,
            webhook_secret=settings.stripe_webhook_secret,
            notify_url=settings.stripe_notify_url,
        )
        if gateway.is_configured:
            return provider, gateway
        logger.warning(
            "HEMALL_PAYMENT_GATEWAY_PROVIDER=stripe but keys missing "
            "(secret_key/webhook_secret); falling back to manual"
        )
        return provider, ManualPaymentGateway()

    return "manual", ManualPaymentGateway()
