"""补天计划 (Operation Sky-Patch) 无 DB 单元测试 — 无条件运行。

覆盖：
  - Task 1.1 Webhook HMAC-SHA256 验签 (app.security.webhook)
  - Task 1.2 令牌桶限流 (app.middleware.ratelimit)
  - Task 1.3 收款网关 Prepay/Refund 契约骨架 (ManualPaymentGateway)
  - Task 2.1 地理位置找货的纯算法 (oskill.find_nearest_location / is_shelf_life_safe)
  - Task 3.1 零号探针 omodul 注册与 Input 校验
"""

from __future__ import annotations

import hmac
import hashlib
import time
from datetime import UTC, datetime, timedelta

import asyncio

import pytest

from app.ext.oskill import find_nearest_location, is_shelf_life_safe
from app.ext.payout_provider import ManualPaymentGateway
from app.ext.registry import ADMIN_OPS, DOMAINS, all_endpoint_specs
from app.middleware.ratelimit import TokenBucket, TokenBucketRateLimiter
from app.routers import public_zero_login_paths
from app.security.webhook import verify_hmac_sha256


# ── Task 1.1: Webhook 验签 ──────────────────────────────────────────────


def _sign(payload: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def test_webhook_valid_signature_passes():
    payload = b'{"order_id": "o1", "douyin_uid": "u1"}'
    assert verify_hmac_sha256(payload, _sign(payload, "s3cret"), "s3cret") is True


def test_webhook_wrong_signature_rejected():
    payload = b'{"order_id": "o1"}'
    assert verify_hmac_sha256(payload, "deadbeef", "s3cret") is False


def test_webhook_empty_signature_rejected():
    assert verify_hmac_sha256(b"x", "", "s3cret") is False


def test_webhook_timestamp_replay_window_rejected():
    payload = b"payload"
    stale = str(int(time.time()) - 3600)
    sig = _sign(payload, "s3cret")
    assert (
        verify_hmac_sha256(payload, sig, "s3cret", timestamp=stale) is False
    )


def test_webhook_fresh_timestamp_accepted():
    payload = b"payload"
    now = str(int(time.time()))
    sig = _sign(payload, "s3cret")
    assert verify_hmac_sha256(payload, sig, "s3cret", timestamp=now) is True


def test_webhook_nonce_replay_rejected():
    payload = b"payload"
    sig = _sign(payload, "s3cret")
    seen: set[str] = set()
    assert (
        verify_hmac_sha256(payload, sig, "s3cret", nonce="abc", _seen_nonces=seen)
        is True
    )
    assert (
        verify_hmac_sha256(payload, sig, "s3cret", nonce="abc", _seen_nonces=seen)
        is False
    )


def test_webhook_garbage_timestamp_rejected():
    payload = b"payload"
    sig = _sign(payload, "s3cret")
    assert verify_hmac_sha256(payload, sig, "s3cret", timestamp="not-a-number") is False


# ── Task 1.2: 令牌桶限流 ────────────────────────────────────────────────


def test_token_bucket_allows_up_to_capacity_then_blocks():
    bucket = TokenBucket(capacity=3, refill_per_sec=0.0)
    assert bucket.allow() == (True, 0.0)
    assert bucket.allow() == (True, 0.0)
    assert bucket.allow() == (True, 0.0)
    allowed, wait = bucket.allow()
    assert allowed is False
    assert wait == 0.0  # 永不补充的桶：拒绝且无 Retry-After 语义


def test_token_bucket_refills_over_time():
    bucket = TokenBucket(capacity=1, refill_per_sec=1000.0)
    assert bucket.allow() == (True, 0.0)
    allowed, _ = bucket.allow()
    assert allowed is False  # 瞬时第二次请求被拒 (等满微秒级补桶)
    time.sleep(0.01)
    assert bucket.allow() == (True, 0.0)  # 补桶后恢复放行


def test_limiter_keys_by_ip_and_device():
    limiter = TokenBucketRateLimiter(capacity=1, refill_per_sec=0.0)
    assert limiter.key_for("1.2.3.4", "dev-1") == "1.2.3.4:dev-1"
    assert limiter.key_for("1.2.3.4", None) == "1.2.3.4:anon"
    # 同一 IP 不同设备额度独立：第一台耗尽后第二台仍可放行。
    assert limiter.allow("1.2.3.4:dev-1") == (True, 0.0)
    assert limiter.allow("1.2.3.4:dev-1") == (False, 0.0)
    assert limiter.allow("1.2.3.4:dev-2") == (True, 0.0)


def test_limiter_public_path_set_derivation():
    paths = public_zero_login_paths()
    # 补天计划新增的公开端点必须在集合里 (限流防刷覆盖)。
    assert "/store/nearby-feed" in paths
    assert "/growth/douyin_callback" in paths
    assert "/aftersales/submit_rma_claim" in paths
    # Admin Ops 端点不在公开集合里。
    assert "/supply-chain/create_inventory_batch" not in paths
    assert "/marketing/trigger_initial_probe_workflow" not in paths


# ── Task 1.3: 收款网关契约骨架 ──────────────────────────────────────────


def test_payment_gateway_prepay_shape():
    gateway = ManualPaymentGateway()
    result = asyncio.run(gateway.prepay(
        out_trade_no="order_123",
        total_fee_cents=2500,
        description="测试订单",
        notify_url="https://mall.sxueji.com/payments/wechat/notify",
    ))
    assert result["status"] == "pending"
    assert result["code_url"].startswith("weixin://")
    assert result["prepay_id"]
    # 幂等：同 out_trade_no 重复下单返回同一单。
    again = asyncio.run(gateway.prepay(
        out_trade_no="order_123",
        total_fee_cents=2500,
        description="测试订单",
        notify_url="x",
    ))
    assert again["code_url"] == result["code_url"]


def test_payment_gateway_refund_shape():
    gateway = ManualPaymentGateway()
    result = asyncio.run(gateway.refund(
        out_trade_no="order_123",
        out_refund_no="refund_1",
        refund_fee_cents=500,
        reason="customer return",
    ))
    assert result["status"] == "success"
    assert result["refund_id"]


def test_payment_gateway_rejects_negative_amounts():
    gateway = ManualPaymentGateway()
    with pytest.raises(ValueError):
        asyncio.run(gateway.prepay(out_trade_no="o", total_fee_cents=0, description="", notify_url=""))
    with pytest.raises(ValueError):
        asyncio.run(gateway.refund(out_trade_no="o", out_refund_no="r", refund_fee_cents=-1))


def test_payment_gateway_stripe_prepay_shape():
    gateway = ManualPaymentGateway()
    result = asyncio.run(gateway.stripe_prepay(
        out_trade_no="order_s", total_fee_cents=990, description="草莓批次"
    ))
    assert result["payment_intent_id"].startswith("pi_mock_")
    assert result["client_secret"]


# ── Task 2.1: 地理位置找货纯算法 ────────────────────────────────────────


def test_find_nearest_location():
    locations = [("a", 31.0, 121.0), ("b", 31.5, 121.5)]
    nearest_id, dist_km = find_nearest_location(locations, lat=31.0, lon=121.0)
    assert nearest_id == "a"
    assert 0 <= dist_km < 1  # 同点距离约 0


def test_find_nearest_location_other_node():
    locations = [("a", 31.0, 121.0), ("b", 31.5, 121.5)]
    nearest_id, _ = find_nearest_location(locations, lat=31.4, lon=121.4)
    assert nearest_id == "b"


def test_find_nearest_location_empty_raises():
    with pytest.raises(ValueError):
        find_nearest_location([], lat=1.0, lon=1.0)


def test_shelf_life_safe():
    now = datetime.now(UTC)
    assert is_shelf_life_safe(None, now=now, margin_hours=2.0) is True  # 无过期时间=长期品
    assert (
        is_shelf_life_safe(now + timedelta(hours=5), now=now, margin_hours=2.0)
        is True
    )
    assert (
        is_shelf_life_safe(now + timedelta(hours=1), now=now, margin_hours=2.0)
        is False
    )
    assert is_shelf_life_safe(now - timedelta(minutes=1), now=now, margin_hours=2.0) is False


# ── Task 2.2 / 3.1: 新 omodul 注册 (registry 接线) ──────────────────────


def test_new_omoduls_registered():
    assert "scrap_batch_inventory" in DOMAINS["supply-chain"]
    assert "trigger_initial_probe_workflow" in DOMAINS["marketing"]
    assert "scrap_batch_inventory" in ADMIN_OPS
    assert "trigger_initial_probe_workflow" in ADMIN_OPS


def test_new_omoduls_load_endpoint_specs():
    specs = {s.name: s for s in all_endpoint_specs()}
    assert specs["scrap_batch_inventory"].require_auth is True
    assert specs["scrap_batch_inventory"].path == "/supply-chain/scrap_batch_inventory"
    assert specs["trigger_initial_probe_workflow"].require_auth is True
    assert specs["trigger_initial_probe_workflow"].path == (
        "/marketing/trigger_initial_probe_workflow"
    )


def test_probe_input_requires_positive_price():
    from app.ext.omodul.trigger_initial_probe_workflow import (
        TriggerInitialProbeWorkflowInput,
    )
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TriggerInitialProbeWorkflowInput(batch_id="b1", initial_price=0)
    ok = TriggerInitialProbeWorkflowInput(batch_id="b1", initial_price=100)
    assert ok.initial_price == 100
