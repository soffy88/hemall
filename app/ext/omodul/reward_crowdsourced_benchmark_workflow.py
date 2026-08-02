"""app.ext.omodul.reward_crowdsourced_benchmark_workflow — 众包小票核销与算力金奖励。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from omodul._base import BaseConfig, Trail, build_result, compute_fingerprint

from ..oprim import cv_parse_retail_receipt
from ..oskill import normalize_sku_price

#: SPEC 明确写死的无门槛系统余额奖励 (5 元)。
_DEFAULT_REWARD_CENTS = 500


class RewardCrowdsourcedBenchmarkWorkflowConfig(BaseConfig):
    _omodul_name: ClassVar[str] = "reward_crowdsourced_benchmark_workflow"
    _omodul_version: ClassVar[str] = "1.0.0"
    _fingerprint_fields: ClassVar[set[str]] = {"customer_id"}
    _enabled_pillars: ClassVar[set[str]] = {"decision_trail", "cost"}

    cv_provider: str = "manual"
    reward_amount_cents: int = _DEFAULT_REWARD_CENTS


class RewardCrowdsourcedBenchmarkWorkflowInput(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    # 统一之前是自由字符串 user_id；统一之后是真 customer.id FK，奖励余额
    # (customer.system_balance) 才能真的落到一个真实账户上。
    customer_id: str
    receipt_image: bytes


async def reward_crowdsourced_benchmark_workflow(
    config: RewardCrowdsourcedBenchmarkWorkflowConfig,
    input_data: RewardCrowdsourcedBenchmarkWorkflowInput,
    output_dir: Path,
    *,
    pool: Any = None,
    on_step: Any = None,
) -> dict:
    """众包小票核销事务：OCR 解析 -> 归一化写入价格基线 -> 无门槛打赏算力金。

    SPEC 的函数签名是 ``(config, user_id, receipt_image, db_pool)``，没有走
    这个项目里每个 omodul 都严格遵守的 (config, input_data, output_dir, *,
    pool, on_step) 契约——这里改成标准契约，user_id/receipt_image 塞进
    Input 模型。

    variant_id 匹配：小票品名跟 product.title 子串匹配，命中就用该商品任一
    variant 的 id 写入 price_benchmark.variant_id；没命中就把 variant_id 留
    NULL、原始品名存进 raw_item_name (统一之前用一个字符串 slug 顶替，现在
    variant_id 是真 UUID FK 装不下 slug，见该表 schema 注释)。

    normalize_sku_price 解析不出单位时返回 None——原样存 NULL 到
    normalized_price_per_unit，不写一个不可比的"整件价"进去污染后续 AVG()。

    系统余额奖励用 INSERT ... ON CONFLICT 而不是普通 UPDATE：customer 行本身
    在下单/注册时就该已经存在，但这里不假设调用顺序，ON CONFLICT DO UPDATE
    比普通 UPDATE 更保险 (即便一开始没有也不会静默丢失奖励)。

    Args:
        config: RewardCrowdsourcedBenchmarkWorkflowConfig(cv_provider="manual",
            reward_amount_cents=500)。
        input_data: customer_id / receipt_image (小票原始图片字节流)。
        output_dir: decision_trail 落盘目录。
        pool: obase.persistence.PgPool。必须落库，pool 为 None 直接判 failed。
        on_step: 进度回调，可选。

    Returns:
        标准 omodul 返回 dict；completed 时含 rewarded_cents /
        benchmarks_added / benchmark_ids。
    """
    trail = Trail()
    fp = compute_fingerprint({"customer_id": input_data.customer_id})

    try:
        if pool is None:
            raise ValueError(
                "pool is required — reward_crowdsourced_benchmark_workflow always touches persisted stock"
            )

        items = await cv_parse_retail_receipt(
            config.cv_provider, image_bytes=input_data.receipt_image
        )
        if not items:
            raise ValueError("unrecognized_receipt: OCR returned no items")
        trail.record(event="receipt_parsed", item_count=len(items))

        async with pool.acquire() as conn:
            candidates = await conn.fetch(
                "SELECT p.title AS product_name, v.id AS variant_id "
                'FROM "product" p JOIN "product_variant" v ON v.product_id = p.id'
            )

        from obase.uuid7 import uuid7

        benchmark_ids: list[str] = []
        skipped_unnormalized: list[str] = []
        async with pool.acquire() as conn:
            for item in items:
                name = str(item.get("item", ""))
                price = int(item.get("price", 0))
                unit = str(item.get("unit", "1件"))
                store = item.get("store") or "unknown"

                matched_variant_id: Any = None
                name_lower = name.lower()
                for row in candidates:
                    product_name_lower = row["product_name"].lower()
                    if name_lower and (
                        name_lower in product_name_lower
                        or product_name_lower in name_lower
                    ):
                        matched_variant_id = row["variant_id"]
                        break

                normalized = normalize_sku_price(price, raw_unit=unit)
                if normalized is None:
                    skipped_unnormalized.append(name)

                bm_id = uuid7()
                await conn.execute(
                    'INSERT INTO "price_benchmark" '
                    "(id, variant_id, raw_item_name, source_type, competitor_name, "
                    "raw_price_cents, raw_unit, normalized_price_per_unit) "
                    "VALUES ($1, $2, $3, 'receipt', $4, $5, $6, $7)",
                    bm_id,
                    matched_variant_id,
                    None if matched_variant_id else name,
                    store,
                    price,
                    unit,
                    normalized,
                )
                benchmark_ids.append(bm_id)
        trail.record(
            event="benchmarks_recorded",
            count=len(benchmark_ids),
            skipped_unnormalized=skipped_unnormalized,
        )

        async with pool.acquire() as conn:
            updated = await conn.fetchval(
                'UPDATE "customer" SET system_balance = system_balance + $1 '
                "WHERE id = $2 RETURNING id",
                config.reward_amount_cents,
                input_data.customer_id,
            )
        if updated is None:
            raise ValueError(f"customer {input_data.customer_id!r} not found")
        trail.record(event="reward_credited", amount=config.reward_amount_cents)

        trail_path = trail.write(output_dir)
        return build_result(
            status="completed",
            error=None,
            fingerprint=fp,
            trail=trail,
            trail_path=trail_path,
            cost_usd=0.0,
            rewarded_cents=config.reward_amount_cents,
            benchmarks_added=len(benchmark_ids),
            benchmark_ids=benchmark_ids,
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
