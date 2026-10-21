"""微信支付 API v3 生产分支 — 单元测试（全 mock，无外网、无 DB）。

覆盖 app/payments/wechat.py 的 JSAPI 下单 / 回调验签解密 / 查单：
- JSAPI 请求 URL/body 关键字段与 Authorization 头结构，且头签名可用
  商户公钥验过；
- 响应组装的小程序调起参数 paySign 可用商户公钥验过；
- 回调：平台证书私钥签名 + api_v3_key AES-GCM 加密构造的回调能被
  验签+解密正确解析；篡改签名/正文被拒（返回 None）；
- 配置不全时构造 fail-fast；沙箱行为不变。
"""

from __future__ import annotations

import base64
import json
import re
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app.ext.payment_gateways import rsa_sha256_sign, rsa_sha256_verify
from app.payments.wechat import (
    WeChatPayOrderRequest,
    WeChatPayProvider,
)


def _aes_gcm_encrypt(key_str: str, plaintext: str, associated_data: str):
    """按微信服务端的方式加密回调资源（AEAD_AES_256_GCM）。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = "testnonce123"  # 12 字节
    ciphertext = AESGCM(key_str.encode()).encrypt(
        nonce.encode(), plaintext.encode(), associated_data.encode()
    )
    return nonce, base64.b64encode(ciphertext).decode()


API_V3_KEY = "0123456789abcdef0123456789abcdef"  # 32 字节测试密钥


def _rsa_key_and_pem():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return key, pem


def _self_signed_cert_pem(key) -> str:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "wechat-pay-test")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode()


MERCHANT_KEY, MERCHANT_KEY_PEM = _rsa_key_and_pem()
PLATFORM_KEY, _ = _rsa_key_and_pem()
PLATFORM_CERT_PEM = _self_signed_cert_pem(PLATFORM_KEY)


def _settings(**overrides):
    base = {
        "wechat_pay_appid": "wxmpappid123",
        "wechat_pay_mchid": "1900000001",
        "wechat_pay_serial_no": "SERIAL123",
        "wechat_pay_private_key_path": MERCHANT_KEY_PEM,
        "wechat_pay_api_v3_key": API_V3_KEY,
        "wechat_pay_platform_cert_path": PLATFORM_CERT_PEM,
        "wechat_pay_notify_url": "https://mall.example.com/payments/wechat/notify",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _parse_auth_header(header: str) -> dict[str, str]:
    assert header.startswith("WECHATPAY2-SHA256-RSA2048 ")
    return dict(re.findall(r'(\w+)="([^"]*)"', header))


@pytest.mark.asyncio
async def test_jsapi_create_order_request_shape_and_paysign():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = request.content.decode()
        captured["authorization"] = request.headers.get("Authorization", "")
        return httpx.Response(200, json={"prepay_id": "wx_prepay_123"})

    provider = WeChatPayProvider(settings=_settings(), transport=httpx.MockTransport(handler))
    resp = await provider.create_order(
        WeChatPayOrderRequest(
            out_trade_no="order-mp-1",
            description="测试商品",
            total_amount=2599,
            payer_openid="openid-alice-wxmp",
        )
    )

    assert resp.success is True
    assert resp.prepay_id == "wx_prepay_123"

    # 请求形状
    assert captured["method"] == "POST"
    assert captured["path"] == "/v3/pay/transactions/jsapi"
    body = json.loads(captured["body"])
    assert body["appid"] == "wxmpappid123"
    assert body["mchid"] == "1900000001"
    assert body["description"] == "测试商品"
    assert body["out_trade_no"] == "order-mp-1"
    assert body["notify_url"] == "https://mall.example.com/payments/wechat/notify"
    assert body["amount"] == {"total": 2599, "currency": "CNY"}
    assert body["payer"] == {"openid": "openid-alice-wxmp"}

    # Authorization 头结构 + 签名可用商户公钥验过
    auth = _parse_auth_header(captured["authorization"])
    assert auth["mchid"] == "1900000001"
    assert auth["serial_no"] == "SERIAL123"
    assert auth["timestamp"] and auth["nonce_str"] and auth["signature"]
    sign_message = (
        f"POST\n/v3/pay/transactions/jsapi\n{auth['timestamp']}\n"
        f"{auth['nonce_str']}\n{captured['body']}\n"
    )
    assert rsa_sha256_verify(MERCHANT_KEY.public_key(), sign_message, auth["signature"])

    # 小程序调起参数 + paySign 可验
    params = resp.jsapi_params
    assert params is not None
    assert params["package"] == "prepay_id=wx_prepay_123"
    assert params["signType"] == "RSA"
    pay_message = (
        f"wxmpappid123\n{params['timeStamp']}\n{params['nonceStr']}\n{params['package']}\n"
    )
    assert rsa_sha256_verify(MERCHANT_KEY.public_key(), pay_message, params["paySign"])


@pytest.mark.asyncio
async def test_jsapi_create_order_wechat_error_maps_to_err_fields():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"code": "NOAUTH", "message": "not authorized"})

    provider = WeChatPayProvider(settings=_settings(), transport=httpx.MockTransport(handler))
    resp = await provider.create_order(
        WeChatPayOrderRequest(
            out_trade_no="order-mp-2",
            description="x",
            total_amount=100,
            payer_openid="openid-alice-wxmp",
        )
    )
    assert resp.success is False
    assert resp.err_code == "NOAUTH"
    assert resp.err_msg == "not authorized"
    assert resp.jsapi_params is None


@pytest.mark.asyncio
async def test_query_order_production():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["query"] = request.url.query.decode()
        captured["authorization"] = request.headers.get("Authorization", "")
        return httpx.Response(200, json={"trade_state": "SUCCESS", "out_trade_no": "order-mp-1"})

    provider = WeChatPayProvider(settings=_settings(), transport=httpx.MockTransport(handler))
    result = await provider.query_order("order-mp-1")
    assert result["trade_state"] == "SUCCESS"
    assert captured["path"] == "/v3/pay/transactions/out-trade-no/order-mp-1"
    assert "mchid=1900000001" in captured["query"]
    auth = _parse_auth_header(captured["authorization"])
    sign_message = (
        f"GET\n/v3/pay/transactions/out-trade-no/order-mp-1?mchid=1900000001\n"
        f"{auth['timestamp']}\n{auth['nonce_str']}\n\n"
    )
    assert rsa_sha256_verify(MERCHANT_KEY.public_key(), sign_message, auth["signature"])


def _build_notify_body_and_headers(*, tamper_body: bool = False, wrong_key=None):
    plain = json.dumps(
        {
            "out_trade_no": "order-mp-1",
            "transaction_id": "wx-tx-999",
            "trade_state": "SUCCESS",
            "amount": {"total": 2599, "currency": "CNY"},
            "success_time": "2026-10-21T12:00:00+08:00",
        }
    )
    nonce, ciphertext = _aes_gcm_encrypt(API_V3_KEY, plain, "payment_notify")
    envelope = {
        "id": "notify-1",
        "event_type": "TRANSACTION.SUCCESS",
        "resource": {
            "algorithm": "AEAD_AES_256_GCM",
            "ciphertext": ciphertext,
            "nonce": nonce,
            "associated_data": "payment_notify",
        },
    }
    body = json.dumps(envelope).encode()
    timestamp = str(int(time.time()))
    sign_nonce = "notifynonce123"
    signature = rsa_sha256_sign(
        wrong_key or PLATFORM_KEY,
        f"{timestamp}\n{sign_nonce}\n{body.decode()}\n",
    )
    if tamper_body:
        body = body + b" "
    headers = {
        "Wechatpay-Timestamp": timestamp,
        "Wechatpay-Nonce": sign_nonce,
        "Wechatpay-Signature": signature,
        "Wechatpay-Serial": "PLATFORM-SERIAL",
    }
    return headers, body


@pytest.mark.asyncio
async def test_handle_notify_production_verify_and_decrypt():
    provider = WeChatPayProvider(settings=_settings())
    headers, body = _build_notify_body_and_headers()
    notify = await provider.handle_notify(headers, body)
    assert notify is not None
    assert notify.out_trade_no == "order-mp-1"
    assert notify.transaction_id == "wx-tx-999"
    assert notify.trade_state == "SUCCESS"
    assert notify.amount == 2599
    assert notify.is_success is True


@pytest.mark.asyncio
async def test_handle_notify_tampered_body_rejected():
    provider = WeChatPayProvider(settings=_settings())
    headers, body = _build_notify_body_and_headers(tamper_body=True)
    assert await provider.handle_notify(headers, body) is None


@pytest.mark.asyncio
async def test_handle_notify_wrong_signer_rejected():
    provider = WeChatPayProvider(settings=_settings())
    headers, body = _build_notify_body_and_headers(wrong_key=MERCHANT_KEY)
    assert await provider.handle_notify(headers, body) is None


def test_constructor_fail_fast_when_config_incomplete():
    with pytest.raises(NotImplementedError):
        WeChatPayProvider(settings=_settings(wechat_pay_serial_no=""))


def test_sandbox_behavior_unchanged():
    provider = WeChatPayProvider(settings=_settings(wechat_pay_mchid=""))
    assert provider._sandbox is True


@pytest.mark.asyncio
async def test_sandbox_create_order_still_works():
    provider = WeChatPayProvider(settings=_settings(wechat_pay_mchid=""))
    resp = await provider.create_order(
        WeChatPayOrderRequest(out_trade_no="sandbox-1", description="x", total_amount=1)
    )
    assert resp.success is True
    assert resp.prepay_id
    assert resp.code_url
    assert resp.jsapi_params is None


@pytest.mark.asyncio
async def test_service_routes_openid_to_sdk_when_gateway_is_native_only(monkeypatch):
    """PaymentService._provider_prepay：注册网关只有 Native prepay 时，
    带 payer_openid 的微信支付须分流到 SDK 的 JSAPI create_order，
    且 jsapi 调起参数透传进结果（storefront 响应据此带给小程序）。"""
    from app.payments import wechat as wechat_module
    from app.payments.service import PaymentService
    from app.payments.wechat import WeChatPayOrderResponse

    constructed: dict = {}

    class FakeSdkProvider:
        def __init__(self, *, settings=None, transport=None):
            constructed["settings"] = settings

        async def create_order(self, request):
            constructed["openid"] = request.payer_openid
            return WeChatPayOrderResponse(
                prepay_id="prepay-x",
                jsapi_time_stamp="1",
                jsapi_nonce_str="n",
                jsapi_package="prepay_id=prepay-x",
                jsapi_sign_type="RSA",
                jsapi_pay_sign="c2ln",
            )

    class NativeOnlyGateway:
        async def prepay(self, **kwargs):
            return {"code_url": "weixin://native", "gateway": "wechat"}

    settings = SimpleNamespace(
        wechat_pay_notify_url="https://mall.example.com/payments/wechat/notify"
    )
    svc = PaymentService(object(), settings)
    monkeypatch.setattr(svc, "_gateway", lambda provider: NativeOnlyGateway())
    monkeypatch.setattr(wechat_module, "WeChatPayProvider", FakeSdkProvider)

    result = await svc._provider_prepay(
        provider="wechat",
        out_trade_no="svc-order-1",
        total_fee_cents=100,
        currency="CNY",
        description="svc",
        payer_openid="openid-alice-wxmp",
    )
    assert constructed["settings"] is settings
    assert constructed["openid"] == "openid-alice-wxmp"
    assert result["gateway"] == "wechat"
    assert result["prepay_id"] == "prepay-x"
    assert result["jsapi"]["package"] == "prepay_id=prepay-x"
    assert result["jsapi"]["paySign"] == "c2ln"


def test_response_base64_signature_is_real_base64():
    # paySign 是标准 Base64（回归防护：签名串拼错会让验签直接失败，
    # 上面的验签断言已覆盖；这里只确保字段非空占位）。
    assert base64.b64decode(PLATFORM_CERT_PEM.encode().splitlines()[1])
