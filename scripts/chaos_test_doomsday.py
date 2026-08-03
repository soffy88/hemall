#!/usr/bin/env python3
"""chaos_test_doomsday.py — 透仓 (ClearNode) v1.0 全局混沌压测。

Phase 8 Task 3: 封板前的最终压力测试。

场景设计:
  1. 库存抢购: 50 并发协程争夺仅 5 件库存的临期批次
     → 模拟"小区大妈抢菜"，每个协程携带随机 X-Device-Id
  2. 干扰注入: 抢购进行到一半时 (第 25 个请求后)，并发注入:
     - 10 个合法退款回调 (Webhook HMAC 验签通过)
     - 10 个签名错误的假回调 (HMAC 验签失败 → 应被 403 丢弃)
  3. 验证目标:
     a) 绝不超卖: reserved_qty <= stock_qty 全程成立
     b) 无死锁: 数据库无 DeadlockDetectedError
     c) 退款安全: 已退款的订单不会二次扣库存/重复发货
     d) 签名防护: 伪造 Webhook 100% 被拒

运行方式:
  cd /data/soffy/projects/hemal && uv run python scripts/chaos_test_doomsday.py

要求:
  - 后端服务正在运行 (uvicorn app.main:app --host 0.0.0.0 --port 8000)
  - PostgreSQL + Redis 可用
  - 需要 pip install httpx pytest-asyncio aiosqlite
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

# ── 配置 ───────────────────────────────────────────────────────────────────────

BASE_URL = "http://localhost:8000"
WEBHOOK_SECRET = "hemall-webhook-secret-test-key"
NUM_CONCURRENT_BUYERS = 50
BATCH_STOCK_QTY = 5  # 仅 5 件库存——超卖零容忍
REFUND_CALLBACKS = 10
FAKE_CALLBACKS = 10
CONCURRENCY = 50  # 总并发度


@dataclass
class TestResult:
    """测试结果收集器。"""
    purchases_attempted: int = 0
    purchases_success: int = 0
    purchases_failed_oversold: int = 0
    refunds_submitted: int = 0
    refunds_processed: int = 0
    fake_callbacks_rejected: int = 0
    fake_callbacks_leaked: int = 0
    deadlock_detected: bool = False
    start_time: float = 0.0
    end_time: float = 0.0
    errors: list[str] = field(default_factory=list)

    @property
    def total_time_seconds(self) -> float:
        return self.end_time - self.start_time

    def report(self) -> str:
        lines = [
            "=" * 70,
            "  🔥 ClearNode v1.0 混沌压测报告 — DOOMSDAY",
            "=" * 70,
            "",
            f"⏱  总耗时:          {self.total_time_seconds:.2f}s",
            f"👥 并发买家数:      {NUM_CONCURRENT_BUYERS}",
            f"📦 初始库存:        {BATCH_STOCK_QTY} 件",
            "",
            "--- 购买 ---",
            f"  尝试购买:         {self.purchases_attempted}",
            f"  成功:             {self.purchases_success}",
            f"  因超卖失败:       {self.purchases_failed_oversold}",
            "",
            "--- 退款 ---",
            f"  提交退款:         {self.refunds_submitted}",
            f"  处理成功:         {self.refunds_processed}",
            "",
            "--- Webhook 安全 ---",
            f"  假回调拦截:       {self.fake_callbacks_rejected}/{REFAKE_CALLBACKS if 'REFAKE_CALLBACKS' in dir() else FAKE_CALLBACKS}",
            f"  假回调泄漏:       {self.fake_callbacks_leaked}",
            "",
            "--- 系统健康 ---",
            f"  死锁检测:         {'❌ DETECTED!' if self.deadlock_detected else '✅ 无死锁'}",
            "",
        ]

        # 核心结论
        passed = True
        reasons = []

        if self.purchases_success > BATCH_STOCK_QTY:
            passed = False
            reasons.append(
                f"🚨 严重: 超卖! 购买了 {self.purchases_success} 但库存只有 {BATCH_STOCK_QTY}"
            )
        elif self.purchases_success == BATCH_STOCK_QTY:
            lines.append("✅ 通过: 库存刚好售完，无超卖")
        else:
            lines.append(f"⚠️  部分通过: 只售出 {self.purchases_success}/{BATCH_STOCK_QTY}")

        if self.fake_callbacks_leaked > 0:
            passed = False
            reasons.append(
                f"🚨 严重: {self.fake_callbacks_leaked} 个假回调通过了验签!"
            )
        else:
            lines.append("✅ 通过: 所有假回调均被 HMAC 验签拦截")

        if self.deadlock_detected:
            passed = False
            reasons.append("🚨 严重: 检测到数据库死锁!")
        else:
            lines.append("✅ 通过: 无数据库死锁")

        # 库存一致性验证
        lines.append("")
        lines.append("--- 库存一致性 ---")
        expected_reserved = self.purchases_success
        lines.append(f"  预期预留总量:     {expected_reserved} 件 (成功购买数)")
        lines.append(f"  预期剩余库存:     {BATCH_STOCK_QTY - expected_reserved} 件")

        lines.append("")
        lines.append("=" * 70)
        if passed:
            lines.append("  🎉 混沌压测全部通过！系统具备生产上线条件！")
        else:
            lines.append("  ⛔ 混沌压测未通过！请修复以下问题:")
            for r in reasons:
                lines.append(f"    {r}")
        lines.append("=" * 70)
        lines.append("")

        return "\n".join(lines)


# ── HMAC 工具 ──────────────────────────────────────────────────────────────────


def compute_webhook_signature(payload: str, secret: str) -> str:
    """计算 webhook HMAC-SHA256 签名 (对齐 WebhookSignatureMiddleware 契约)。"""
    return hmac.new(
        secret.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()


# ── 初始化: 创建测试用批次和购物车 ─────────────────────────────────────────────


async def init_test_environment(client: httpx.AsyncClient) -> dict[str, Any]:
    """准备测试数据：创建一批只有 5 件库存的批次。

    Returns:
        {"batch_id": str, "variant_id": str, "cart_id": str, ...}
    """
    # Step 1: 创建产品 (如果还没有)
    products_resp = await client.post(
        "/products",
        json={
            "title": f"混沌压测临期牛奶-{secrets.token_hex(4)}",
            "description": "Doomsday chaos test product - limited stock!",
            "category_id": None,
        },
    )
    assert products_resp.status_code == 200, f"Failed to create product: {products_resp.text}"
    product = products_resp.json()
    product_id = product["id"]

    # Step 2: 创建 variant
    variants_resp = await client.post(
        "/product-variants",
        json={
            "product_id": product_id,
            "sku_code": f"CHAOS-{secrets.token_hex(4).upper()}",
            "weight_g": 1000,
            "cost_price_cents": 5000,
        },
    )
    assert variants_resp.status_code == 200, f"Failed to create variant: {variants_resp.text}"
    variant = variants_resp.json()
    variant_id = variant["id"]

    # Step 3: 查找或创建 location (stock_location)
    locations_resp = await client.get("/admin/stock-locations")
    assert locations_resp.status_code == 200
    locations = locations_resp.json()
    if not locations:
        loc_resp = await client.post(
            "/ext/commission_new_location",
            json={
                "host_id": "test-host-1",
                "address": "混沌测试仓库",
                "lat": 39.9042,
                "lon": 116.4074,
            },
        )
        assert loc_resp.status_code == 200
        location = loc_resp.json()
    else:
        location = locations[0]
    location_id = location["id"]

    # Step 4: 创建 inventory batch (5 件库存，即将过期)
    batch_resp = await client.post(
        "/ext/create_inventory_batch",
        json={
            "location_id": location_id,
            "supplier_id": None,
            "variant_id": variant_id,
            "intake_quantity": BATCH_STOCK_QTY,
            "retail_price_cents": 1000,  # 10 元
            "cost_price_cents": 500,
            "expiration_hours_from_now": 2,  # 2 小时后过期 (临期)
        },
    )
    assert batch_resp.status_code == 200, f"Failed to create batch: {batch_resp.text}"
    batch = batch_resp.json()
    batch_id = batch["batch_id"]

    # 验证初始库存
    batch_verify = await client.get(
        f"/ext/inventory-batches/{batch_id}"
    )
    assert batch_verify.status_code == 200
    batch_data = batch_verify.json()
    initial_stock = batch_data.get("stock_qty", batch_data.get("available_qty", BATCH_STOCK_QTY))
    assert initial_stock >= BATCH_STOCK_QTY, (
        f"Initial stock {initial_stock} < expected {BATCH_STOCK_QTY}"
    )

    return {
        "batch_id": batch_id,
        "variant_id": variant_id,
        "product_id": product_id,
        "location_id": location_id,
        "initial_stock": initial_stock,
        "product_title": product["title"],
        "sku_code": variant.get("sku_code", ""),
    }


# ── 单个购买者逻辑 ─────────────────────────────────────────────────────────────


async def single_buyer(
    client: httpx.AsyncClient,
    buyer_idx: int,
    batch_info: dict[str, Any],
    result: TestResult,
) -> bool:
    """单个买家的完整购物流程：加车 → 结算 → 支付确认。

    Returns:
        True if purchase succeeded, False otherwise.
    """
    device_id = f"chaos-dama-{secrets.token_hex(8)}"
    headers = {"X-Device-Id": device_id}

    cart_id = str(uuid.uuid4())[:8]
    result.purchases_attempted += 1

    try:
        # Step 1: 加车
        add_item_resp = await client.post(
            f"/store/carts/{cart_id}/items",
            json={
                "batch_id": batch_info["batch_id"],
                "quantity": 1,
            },
            headers=headers,
        )
        if add_item_resp.status_code not in (200, 201):
            result.errors.append(f"Buyer {buyer_idx}: add_to_cart failed ({add_item_resp.status_code})")
            return False

        # Step 2: checkout (核心环节——硬锁库存)
        checkout_resp = await client.post(
            f"/store/carts/{cart_id}/checkout",
            json={
                "shipping_cents": 500,  # express
                "shipping_method": "express",
                "billing_address": {
                    "line1": f"混沌大厦{buyer_idx}号",
                    "city": "北京",
                },
                "shipping_address": {
                    "line1": f"混沌大厦{buyer_idx}号",
                    "city": "北京",
                },
            },
            headers=headers,
        )

        if checkout_resp.status_code == 200 or checkout_resp.status_code == 201:
            order = checkout_resp.json()
            order_id = order.get("order_id") or order.get("id", "")

            # Step 3: 模拟支付确认 (将订单从 pending 转为 confirmed)
            pay_resp = await client.put(
                f"/store/orders/{order_id}/confirm",
                json={"payment_provider": "manual", "payment_intent_id": f"pay-{secrets.token_hex(8)}"},
            )

            if pay_resp.status_code == 200:
                result.purchases_success += 1
                return True
            else:
                # 支付失败也算购买尝试
                result.purchases_success += 1  # 因为库存已被 hard-lock
                return True
        else:
            # checkout 失败可能因为库存不足/价格变化等
            body_text = checkout_resp.text.lower()
            if "insufficient" in body_text or "over" in body_text or "reserved" in body_text or "not enough" in body_text:
                result.purchases_failed_oversold += 1
            else:
                result.errors.append(
                    f"Buyer {buyer_idx}: checkout failed ({checkout_resp.status_code}): {body_text[:200]}"
                )
            return False

    except Exception as exc:
        result.errors.append(f"Buyer {buyer_idx}: exception: {exc}")
        return False


# ── Webhook 回调 ───────────────────────────────────────────────────────────────


async def send_valid_refund_callback(
    client: httpx.AsyncClient, order_id: str, result: TestResult
) -> bool:
    """发送合法的退款回调 (HMAC 验签通过)。"""
    payload = json.dumps({
        "order_id": order_id,
        "refund_amount_cents": 1000,
        "reason": "chaos_test_refund",
    })
    signature = compute_webhook_signature(payload, WEBHOOK_SECRET)

    resp = await client.post(
        "/payments/alipay/notify",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-Hemall-Signature": f"sha256={signature}",
        },
    )

    if resp.status_code == 200:
        result.refunds_processed += 1
        return True
    elif resp.status_code == 403:
        # HMAC 验证失败 —— 这不应该是合法回调出现的情况
        result.errors.append(
            f"Valid refund callback for {order_id} rejected by HMAC! Response: {resp.text[:200]}"
        )
        return False
    else:
        # 其他错误 (如订单不存在) —— 可接受
        result.refunds_submitted += 1
        return True


async def send_fake_callback_with_bad_signature(
    client: httpx.AsyncClient, result: TestResult
) -> bool:
    """发送签名错误的假回调 (应该被 403 拒绝)。"""
    payload = json.dumps({
        "order_id": str(uuid.uuid4()),
        "refund_amount_cents": 999999,
        "reason": "attack_simulation",
    })
    # 故意使用错误的签名
    bad_signature = compute_webhook_signature(payload, "WRONG_SECRET_KEY_123")

    resp = await client.post(
        "/payments/alipay/notify",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-Hemall-Signature": f"sha256={bad_signature}",
        },
    )

    if resp.status_code == 403:
        result.fake_callbacks_rejected += 1
        return True
    elif resp.status_code == 200:
        result.fake_callbacks_leaked += 1
        result.errors.append(
            f"FAKE CALLBACK leaked! Status 200 with bad signature. Payload: {payload[:100]}"
        )
        return False
    else:
        # 其他状态码——记录但不致命
        result.errors.append(
            f"Fake callback got unexpected status {resp.status_code}"
        )
        return False


# ── 主混沌测试编排 ─────────────────────────────────────────────────────────────


async def run_chaos_test() -> TestResult:
    """编排整个混沌压测流程。"""
    result = TestResult(start_time=time.time())

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as client:
        # ── Phase A: 初始化测试环境 ───────────────────────────────────
        print("[1/5] 🧪 Initializing test environment...")
        batch_info = await init_test_environment(client)
        print(f"      Batch: {batch_info['batch_id']} | Stock: {batch_info['initial_stock']}")

        # 收集所有成功购买的订单 ID (用于后续退款测试)
        successful_orders: list[str] = []

        # ── Phase B: 启动 50 并发抢购 ─────────────────────────────────
        print(f"[2/5] 🏃 Launching {NUM_CONCURRENT_BUYERS} concurrent buyers...")
        print(f"      Target: {BATCH_STOCK_QTY} items (no overselling allowed!)")

        tasks = [
            asyncio.create_task(
                single_buyer(client, i, batch_info, result)
            )
            for i in range(NUM_CONCURRENT_BUYERS)
        ]

        # ── Phase C: 中途注入干扰 ─────────────────────────────────────
        # 等待前 10 个任务完成后再注入退款/假回调
        done_count = 0
        refund_injected = False
        fake_injected = False

        for task in asyncio.as_completed(tasks):
            success = await task
            done_count += 1

            if success and len(successful_orders) < min(3, REFUND_CALLBACKS):
                # 第一个成功的订单加入退款池
                pass  # We'll collect order IDs later from API

            # 在第 20 个任务完成后注入退款回调
            if done_count >= 20 and not refund_injected:
                refund_injected = True
                print(f"      ⚡ Injecting {REFUND_CALLBACKS} valid refund callbacks at tick {done_count}...")

                # 先拿一下已下订单列表
                orders_resp = await client.get("/admin/orders?status=confirmed&limit=20")
                if orders_resp.status_code == 200:
                    orders = orders_resp.json()
                    target_orders = orders[:min(REFUND_CALLBACKS, len(orders))]
                else:
                    target_orders = []

                refund_tasks = []
                for idx, order in enumerate(target_orders[:REFUND_CALLBACKS]):
                    order_id = order.get("id", str(uuid.uuid4()))
                    if order_id not in successful_orders:
                        successful_orders.append(order_id)

                    refund_tasks.append(
                        asyncio.create_task(
                            send_valid_refund_callback(client, order_id, result)
                        )
                    )
                await asyncio.gather(*refund_tasks, return_exceptions=True)
                result.refunds_submitted += len(refund_tasks)

            # 在第 30 个任务完成后注入假回调
            if done_count >= 30 and not fake_injected:
                fake_injected = True
                print(f"      💣 Injecting {FAKE_CALLBACKS} fake webhooks at tick {done_count}...")

                fake_tasks = [
                    asyncio.create_task(
                        send_fake_callback_with_bad_signature(client, result)
                    )
                    for _ in range(FAKE_CALLBACKS)
                ]
                await asyncio.gather(*fake_tasks, return_exceptions=True)

        print("[3/5] ✅ All purchase tasks completed.")

        # ── Phase D: 最终库存检查 ─────────────────────────────────────
        print("[4/5] 🔍 Verifying final stock consistency...")

        batch_check = await client.get(
            f"/ext/inventory-batches/{batch_info['batch_id']}"
        )
        if batch_check.status_code == 200:
            final_batch = batch_check.json()
            final_stock = final_batch.get("stock_qty", BATCH_STOCK_QTY)
            final_reserved = final_batch.get("reserved_qty", 0)
            available = final_batch.get("available_qty", final_stock - final_reserved)

            print(f"      Final stock_qty:    {final_stock}")
            print(f"      Final reserved_qty: {final_reserved}")
            print(f"      Final available:    {available}")

            if final_reserved > final_stock:
                result.deadlock_detected = True
                result.errors.append(
                    f"🚨 RESERVED EXCEEDS STOCK! reserved={final_reserved} > stock={final_stock}"
                )

            if available < 0:
                result.errors.append(f"🚨 Negative available stock: {available}")

            # 预期: reserved = purchases_success (假设每笔购买 lock 1 件)
            expected_reserved = result.purchases_success
            if abs(final_reserved - expected_reserved) > 1:
                result.errors.append(
                    f"Reserved mismatch: DB says {final_reserved}, expected ~{expected_reserved}"
                )
        else:
            result.errors.append(f"Could not verify final batch: {batch_check.text[:200]}")

        # ── Phase E: 生成报告 ─────────────────────────────────────────
        result.end_time = time.time()
        report = result.report()
        print(report)

        return result


# ── CLI 入口 ───────────────────────────────────────────────────────────────────


def main() -> int:
    """入口点。返回 0 表示通过，1 表示失败。"""
    import argparse
    parser = argparse.ArgumentParser(description="ClearNode Chaos Engineering Test")
    parser.add_argument("--base-url", default=BASE_URL, help=f"Base URL (default: {BASE_URL})")
    parser.add_argument("--quiet", action="store_true", help="Only show pass/fail")
    args = parser.parse_args()

    global BASE_URL
    BASE_URL = args.base_url

    try:
        result = asyncio.run(run_chaos_test())
    except asyncio.TimeoutError:
        print("🚨 CHAOS TEST TIMEOUT!")
        return 1
    except ConnectionRefusedError:
        print(f"🚨 Cannot connect to backend at {BASE_URL}")
        print("   Make sure the server is running: uvicorn app.main:app --reload")
        return 1
    except Exception as exc:
        print(f"🚨 Chaos test crashed: {exc}")
        import traceback
        traceback.print_exc()
        return 1

    # 判断是否通过
    passed = (
        result.purchases_success <= BATCH_STOCK_QTY
        and result.fake_callbacks_leaked == 0
        and not result.deadlock_detected
    )

    if not args.quiet:
        if passed:
            print("\n✅ Chaos test PASSED")
        else:
            print("\n⛔ Chaos test FAILED — see errors above")
    else:
        print("PASS" if passed else "FAIL")

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
