"""补天 P0: 真实支付网关 (微信 v3 Native / Stripe) 单测 — 无需真实密钥/网络。

覆盖纯算法与请求形状：
  - RSA-SHA256 签名/验签往返 (测试生成密钥对)
  - AES-256-GCM 回调解密 (测试构造密文)
  - WechatPayNativeGateway.prepay 请求形状 (httpx.MockTransport)
  - WechatPayNativeGateway.verify_callback 平台证书验签 + 解密
  - StripePaymentGateway.prepay/refund 请求形状
  - Stripe webhook HMAC 验签
  - build_payment_gateway 密钥缺失诚实回退 manual
"""

from __future__ import annotations

import base64
import json
import time
from datetime import UTC, datetime

import httpx
import pytest

from app.config import Settings
from app.ext.payment_gateways import (
    StripePaymentGateway,
    WechatPayNativeGateway,
    aes_256_gcm_decrypt,
    build_payment_gateway,
    load_private_key_pem,
    load_public_key_pem,
    rsa_sha256_sign,
    rsa_sha256_verify,
    stripe_webhook_verify,
)
from app.ext.payout_provider import ManualPaymentGateway


def _make_keypair() -> tuple[str, str]:
    """测试用 RSA-2048 密钥对 (PEM)。"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return priv_pem, pub_pem


def _aes_256_gcm_encrypt(api_v3_key: str, nonce: str, plaintext: str) -> tuple[str, str]:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = api_v3_key.encode()
    assert len(key) == 32
    ct = AESGCM(key).encrypt(nonce.encode(), plaintext.encode(), b"")
    return base64.b64encode(ct).decode(), ""


# ── 签名/解密原语 ──────────────────────────────────────────────────────


def test_rsa_sign_verify_roundtrip():
    priv_pem, pub_pem = _make_keypair()
    priv = load_private_key_pem(priv_pem)
    pub = load_public_key_pem(pub_pem)
    sig = rsa_sha256_sign(priv, "GET\n/v3/pay/transactions/native\n1\nabc\n{}\n")
    assert rsa_sha256_verify(pub, "GET\n/v3/pay/transactions/native\n1\nabc\n{}\n", sig)
    assert not rsa_sha256_verify(pub, "tampered", sig)


def test_aes_gcm_decrypt_roundtrip():
    api_v3_key = "0" * 32
    nonce = "123456789012"
    ct, ad = _aes_256_gcm_encrypt(api_v3_key, nonce, '{"trade_state":"SUCCESS"}')
    assert aes_256_gcm_decrypt(api_v3_key, nonce, ct, ad) == '{"trade_state":"SUCCESS"}'


def test_aes_gcm_decrypt_wrong_key_fails():
    nonce = "123456789012"
    ct, _ = _aes_256_gcm_encrypt("0" * 32, nonce, "secret")
    with pytest.raises(Exception):
        aes_256_gcm_decrypt("1" * 32, nonce, ct, "")


# ── 微信网关 ───────────────────────────────────────────────────────────


def _wechat_gateway(transport=None) -> WechatPayNativeGateway:
    priv_pem, _ = _make_keypair()
    return WechatPayNativeGateway(
        appid="wx_test_app",
        mchid="1900000001",
        serial_no="SERIAL_1",
        private_key=priv_pem,
        api_v3_key="0" * 32,
        platform_cert="",
        notify_url="https://mall.sxueji.com/payments/wechat/notify",
        transport=transport,
    )


@pytest.mark.asyncio
async def test_wechat_prepay_request_shape():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization", "")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"code_url": "weixin://wxpay/bizpayurl?pr=test123"}
        )

    gw = _wechat_gateway(transport=httpx.MockTransport(handler))
    result = await gw.prepay(
        out_trade_no="order_001",
        total_fee_cents=2500,
        description="土鸡蛋批次",
        notify_url="",
    )
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.mch.weixin.qq.com/v3/pay/transactions/native"
    assert captured["auth"].startswith("WECHATPAY2-SHA256-RSA2048")
    assert 'mchid="1900000001"' in captured["auth"]
    assert 'serial_no="SERIAL_1"' in captured["auth"]
    body = captured["body"]
    assert body["amount"] == {"total": 2500, "currency": "CNY"}
    assert body["out_trade_no"] == "order_001"
    assert body["notify_url"] == "https://mall.sxueji.com/payments/wechat/notify"
    assert result["code_url"].startswith("weixin://")
    assert result["gateway"] == "wechat"


@pytest.mark.asyncio
async def test_wechat_prepay_api_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text='{"code":"SIGN_ERROR"}')

    gw = _wechat_gateway(transport=httpx.MockTransport(handler))
    with pytest.raises(Exception) as exc:
        await gw.prepay(
            out_trade_no="o", total_fee_cents=100, description="x", notify_url=""
        )
    assert "401" in str(exc.value)


def _wechat_platform_gateway() -> tuple[WechatPayNativeGateway, str]:
    """构造带平台证书的网关 + 平台私钥 PEM (测试自签)。"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()

    priv, _ = _make_keypair()  # 商户私钥
    gw = WechatPayNativeGateway(
        appid="wx_test_app",
        mchid="1900000001",
        serial_no="SERIAL_1",
        private_key=priv,
        api_v3_key="0" * 32,
        platform_cert=pub_pem,
        notify_url="",
    )
    return gw, priv_pem


@pytest.mark.asyncio
async def test_wechat_callback_verify_and_decrypt():
    gw, platform_priv_pem = _wechat_platform_gateway()
    # 微信回调：签名覆盖原始请求 body (envelope)，resource 内是 AES-GCM 密文。
    nonce_iv = "123456789012"
    plaintext = json.dumps(
        {"out_trade_no": "order_001", "trade_state": "SUCCESS", "transaction_id": "wx123"}
    )
    ct, ad = _aes_256_gcm_encrypt("0" * 32, nonce_iv, plaintext)
    envelope = json.dumps(
        {"resource": {"algorithm": "AEAD_AES_256_GCM", "ciphertext": ct, "nonce": nonce_iv, "associated_data": ad}}
    ).encode()
    ts = str(int(time.time()))
    nonce = "platform_nonce_1"
    message = f"{ts}\n{nonce}\n{envelope.decode()}\n"
    platform_priv = load_private_key_pem(platform_priv_pem)
    signature = rsa_sha256_sign(platform_priv, message)

    headers = {
        "Wechatpay-Timestamp": ts,
        "Wechatpay-Nonce": nonce,
        "Wechatpay-Signature": signature,
        "Wechatpay-Serial": "PLATFORM_SERIAL",
    }
    result = await gw.verify_callback(headers, envelope)
    assert result == {
        "out_trade_no": "order_001",
        "trade_state": "SUCCESS",
        "transaction_id": "wx123",
    }


@pytest.mark.asyncio
async def test_wechat_callback_tampered_signature_rejected():
    gw, _ = _wechat_platform_gateway()
    body = json.dumps({"a": 1}).encode()
    headers = {
        "Wechatpay-Timestamp": str(int(time.time())),
        "Wechatpay-Nonce": "n",
        "Wechatpay-Signature": base64.b64encode(b"forged").decode(),
        "Wechatpay-Serial": "PLATFORM_SERIAL",
    }
    assert await gw.verify_callback(headers, body) is None


@pytest.mark.asyncio
async def test_wechat_callback_stale_timestamp_rejected():
    gw, _ = _wechat_platform_gateway()
    body = json.dumps({"a": 1}).encode()
    headers = {
        "Wechatpay-Timestamp": str(int(time.time()) - 3600),
        "Wechatpay-Nonce": "n",
        "Wechatpay-Signature": "x",
        "Wechatpay-Serial": "PLATFORM_SERIAL",
    }
    assert await gw.verify_callback(headers, body) is None


# ── Stripe 网关 ────────────────────────────────────────────────────────


def _stripe_gateway(transport=None) -> StripePaymentGateway:
    return StripePaymentGateway(
        secret_key="sk_test_abc",
        webhook_secret="whsec_test",
        notify_url="",
        transport=transport,
    )


@pytest.mark.asyncio
async def test_stripe_prepay_request_shape():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization", "")
        captured["body"] = request.content.decode()
        return httpx.Response(
            200,
            json={"id": "pi_123", "client_secret": "pi_123_secret", "status": "requires_payment_method"},
        )

    gw = _stripe_gateway(transport=httpx.MockTransport(handler))
    result = await gw.prepay(
        out_trade_no="order_s1", total_fee_cents=990, description="草莓", notify_url=""
    )
    assert captured["auth"].startswith("Basic ")
    assert "amount=990" in captured["body"]
    assert "currency=cny" in captured["body"]
    assert result["payment_intent_id"] == "pi_123"
    assert result["client_secret"] == "pi_123_secret"
    assert result["gateway"] == "stripe"


@pytest.mark.asyncio
async def test_stripe_refund_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "re_1", "status": "succeeded"})

    gw = _stripe_gateway(transport=httpx.MockTransport(handler))
    result = await gw.refund(
        out_trade_no="order_s1",
        out_refund_no="refund_1",
        refund_fee_cents=500,
        reason="customer return",
    )
    assert result["refund_id"] == "re_1"
    assert result["status"] == "succeeded"


def test_stripe_webhook_verify():
    body = b'{"type":"payment_intent.succeeded"}'
    ts = int(time.time())
    expected = __import__("hmac").new(
        b"whsec_test", f"{ts}.{body.decode()}".encode(), __import__("hashlib").sha256
    ).hexdigest()
    header = f"t={ts},v1={expected}"
    assert stripe_webhook_verify("whsec_test", body, header)
    assert not stripe_webhook_verify("whsec_wrong", body, header)
    assert not stripe_webhook_verify("whsec_test", body, "t=1,v1=deadbeef")


def test_stripe_webhook_stale_rejected():
    body = b"x"
    header = f"t={int(time.time()) - 3600},v1=abc"
    assert not stripe_webhook_verify("whsec_test", body, header)


# ── 装配工厂: 密钥缺失诚实回退 ─────────────────────────────────────────


def test_build_gateway_manual_default():
    name, gw = build_payment_gateway(Settings(payment_gateway_provider="manual"))
    assert name == "manual"
    assert isinstance(gw, ManualPaymentGateway)


def test_build_gateway_wechat_without_keys_falls_back():
    settings = Settings(
        payment_gateway_provider="wechat",
        wechat_pay_mchid="",
        wechat_pay_api_v3_key="",
    )
    name, gw = build_payment_gateway(settings)
    assert name == "wechat"
    assert isinstance(gw, ManualPaymentGateway)  # 注册名保真，实现诚实回退


def test_build_gateway_wechat_with_keys_real():
    priv_pem, pub_pem = _make_keypair()
    settings = Settings(
        payment_gateway_provider="wechat",
        wechat_pay_appid="wx_app",
        wechat_pay_mchid="1900000001",
        wechat_pay_serial_no="SERIAL_1",
        wechat_pay_private_key_path=priv_pem,
        wechat_pay_api_v3_key="0" * 32,
        wechat_pay_platform_cert_path=pub_pem,
    )
    name, gw = build_payment_gateway(settings)
    assert name == "wechat"
    assert isinstance(gw, WechatPayNativeGateway)
    assert gw.is_configured


def test_build_gateway_stripe_with_keys_real():
    settings = Settings(
        payment_gateway_provider="stripe",
        stripe_secret_key="sk_live_x",
        stripe_webhook_secret="whsec_x",
    )
    name, gw = build_payment_gateway(settings)
    assert name == "stripe"
    assert isinstance(gw, StripePaymentGateway)
    assert gw.is_configured
