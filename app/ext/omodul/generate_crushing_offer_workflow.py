"""app.ext.omodul.generate_crushing_offer_workflow — 竞对小票比价狙击。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from omodul._base import BaseConfig, Trail, build_result, compute_fingerprint

from ..oskill import compute_user_savings_yield

#: 暴击比价订单的有效期 (分钟)——SPEC 明确写死 30 分钟。这个时限只体现在
#: 返回值 expires_at 里给前端/顾客看，不落库、也没有后台 reaper 去强制失效
#: 购物车 (v1.0 也没有"购物车过期回收"机制)，是诚实的已知空白，不是假装
#: 实现了强制失效。
_OFFER_TTL_MINUTES = 30


class GenerateCrushingOfferWorkflowConfig(BaseConfig):
    _omodul_name: ClassVar[str] = "generate_crushing_offer_workflow"
    _omodul_version: ClassVar[str] = "1.0.0"
    _fingerprint_fields: ClassVar[set[str]] = {"customer_id"}
    _enabled_pillars: ClassVar[set[str]] = {"fingerprint"}


class GenerateCrushingOfferWorkflowInput(BaseModel):
    customer_id: str
    # 预期调用方已经拿 oprim.cv_parse_retail_receipt 把小票 OCR 成结构化明细
    # (跟 execute_liability_routing_workflow 接收已算好的 VLM 结果是同一个
    # "omodul 只吃已处理好的上游输出，不在内部重新调 oprim" 的设计语言)。
    receipt_items: list[dict[str, Any]]  # 每条至少含 "item" (str) / "price" (int)


async def generate_crushing_offer_workflow(
    config: GenerateCrushingOfferWorkflowConfig,
    input_data: GenerateCrushingOfferWorkflowInput,
    output_dir: Path,
    *,
    pool: Any = None,
    on_step: Any = None,
) -> dict:
    """接收小票 OCR 结果，从库存中锁定平替商品组合，生成限时 30 分钟的暴击
    比价订单 (购物车)。

    商品匹配用最朴素的大小写不敏感子串匹配 (小票品名 vs 商品名互相包含即算
    命中)——不是真正的语义/embedding 匹配，SPEC 没给匹配算法细节，这是能
    跑起来的最小可用实现，命中率有限是已知局限，不是伪装成智能匹配。每个
    命中项挑当前可售、零售价最低的一个批次 (不是最新批次)，真正体现"平替
    比价"的定价意图。这里只加购物车，不硬锁库存——跟普通加车流程一致，
    真正锁库存发生在 complete_checkout。

    Args:
        config: GenerateCrushingOfferWorkflowConfig。
        input_data: customer_id / receipt_items (OCR 结果，每条至少含 "item"
            商品名和 "price" 竞对价格分)。
        output_dir: decision_trail 落盘目录 (本函数只声明 fingerprint 支柱，
            仍然写一份 trail 方便排障，跟仓库里其余函数的习惯一致)。
        pool: obase.persistence.PgPool。必须落库，pool 为 None 直接判 failed。
        on_step: 进度回调，可选。

    Returns:
        标准 omodul 返回 dict；completed 时含 cart_id / matched_items
        (list[{"receipt_item","batch_id","our_price","savings"}]) /
        unmatched_items (list[str]) / total_savings / expires_at (ISO str)。
    """
    trail = Trail()
    fp = compute_fingerprint({"customer_id": input_data.customer_id})

    try:
        if pool is None:
            raise ValueError(
                "pool is required — generate_crushing_offer_workflow always touches persisted stock"
            )
        if not input_data.receipt_items:
            raise ValueError("receipt_items must not be empty")

        async with pool.acquire() as conn:
            candidates = await conn.fetch(
                "SELECT p.title AS product_name, b.id AS batch_id, "
                "b.retail_price_cents AS retail_price "
                'FROM "product" p '
                'JOIN "product_variant" v ON v.product_id = p.id '
                'JOIN "inventory_batch" b ON b.variant_id = v.id '
                "WHERE b.status = 'active' AND b.stock_qty - b.reserved_qty > 0 "
                "ORDER BY b.retail_price_cents ASC"
            )

        # 每个商品名只保留零售价最低的那一个候选批次 (candidates 已按价升序，
        # dict 更新时只在第一次出现时写入，天然保留最低价的那条)。
        cheapest_by_product: dict[str, dict[str, Any]] = {}
        for row in candidates:
            cheapest_by_product.setdefault(
                row["product_name"],
                {"batch_id": row["batch_id"], "retail_price": row["retail_price"]},
            )

        from omodul.create_cart import CreateCartConfig, CreateCartInput, create_cart

        cart_result = await create_cart(
            CreateCartConfig(),
            CreateCartInput(customer_id=input_data.customer_id),
            output_dir,
            pool=pool,
        )
        if cart_result["status"] == "failed":
            raise ValueError(f"create_cart failed: {cart_result['error']['message']}")
        cart_id = cart_result["cart_id"]

        from obase.uuid7 import uuid7

        matched_items: list[dict[str, Any]] = []
        unmatched_items: list[str] = []
        total_savings = 0
        subtotal_cents = 0

        async with pool.acquire() as conn:
            for receipt_item in input_data.receipt_items:
                name = str(receipt_item.get("item", ""))
                competitor_price = int(receipt_item.get("price", 0))
                match = None
                name_lower = name.lower()
                for product_name, candidate in cheapest_by_product.items():
                    if name_lower and (
                        name_lower in product_name.lower()
                        or product_name.lower() in name_lower
                    ):
                        match = candidate
                        break

                if match is None:
                    unmatched_items.append(name)
                    continue

                our_price = match["retail_price"]
                savings = compute_user_savings_yield(
                    competitor_price, node_retail_price=our_price
                )
                total_savings += savings
                subtotal_cents += our_price

                await conn.execute(
                    'INSERT INTO "cart_line_item" '
                    "(id, cart_id, batch_id, quantity, unit_price_cents, line_total_cents) "
                    "VALUES ($1, $2, $3, 1, $4, $4)",
                    uuid7(),
                    cart_id,
                    match["batch_id"],
                    our_price,
                )
                matched_items.append(
                    {
                        "receipt_item": name,
                        "batch_id": str(match["batch_id"]),
                        "our_price": our_price,
                        "savings": savings,
                    }
                )

            if subtotal_cents:
                await conn.execute(
                    'UPDATE "cart" SET subtotal_cents = $1, grand_total_cents = $1 '
                    "WHERE id = $2",
                    subtotal_cents,
                    cart_id,
                )

        trail.record(
            event="crushing_offer_generated",
            cart_id=cart_id,
            matched_count=len(matched_items),
            unmatched_count=len(unmatched_items),
            total_savings=total_savings,
        )

        expires_at = datetime.now(UTC) + timedelta(minutes=_OFFER_TTL_MINUTES)
        trail_path = trail.write(output_dir)
        return build_result(
            status="completed",
            error=None,
            fingerprint=fp,
            trail=trail,
            trail_path=trail_path,
            cart_id=cart_id,
            matched_items=matched_items,
            unmatched_items=unmatched_items,
            total_savings=total_savings,
            expires_at=expires_at.isoformat(),
        )

    except Exception as exc:
        trail.record(event="error", detail=str(exc))
        trail_path = trail.write(output_dir) if output_dir else None
        return build_result(
            status="failed",
            error={"type": type(exc).__name__, "message": str(exc)},
            fingerprint=fp,
            trail=trail,
            trail_path=trail_path,
        )
