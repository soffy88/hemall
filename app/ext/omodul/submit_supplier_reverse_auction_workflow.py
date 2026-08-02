"""app.ext.omodul.submit_supplier_reverse_auction_workflow — 果农反向竞标准入。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from omodul._base import BaseConfig, Trail, build_result, compute_fingerprint

from ..oskill import validate_supplier_margin


class SubmitSupplierReverseAuctionWorkflowConfig(BaseConfig):
    _omodul_name: ClassVar[str] = "submit_supplier_reverse_auction_workflow"
    _omodul_version: ClassVar[str] = "1.0.0"
    _fingerprint_fields: ClassVar[set[str]] = {"batch_id"}
    _enabled_pillars: ClassVar[set[str]] = {"decision_trail"}


class SubmitSupplierReverseAuctionWorkflowInput(BaseModel):
    batch_id: str
    # price_benchmark.normalized_price_per_unit 是"分/克"口径——supplier_bid
    # 必须以同一口径申报，这个 omodul 不做重量换算 (没有批次总重量数据可以
    # 换算)。
    supplier_bid_price_per_gram: float
    # 实际要写回 inventory_batch.cost_price_cents 的绝对成本 (分/整批)——跟
    # 上面的每克报价是两个独立数字，由供应商同时申报，不是从每克价反推出来
    # 的 (反推需要知道这批货总共多少克，SPEC 没给这个数据)。
    batch_cost_price: int


async def submit_supplier_reverse_auction_workflow(
    config: SubmitSupplierReverseAuctionWorkflowConfig,
    input_data: SubmitSupplierReverseAuctionWorkflowInput,
    output_dir: Path,
    *,
    pool: Any = None,
    on_step: Any = None,
) -> dict:
    """果农反向竞标事务：对比系统爬虫/众包建立的商超基线均价，判断该批次
    是否允许入库发车。

    SPEC 原文里 ``supplier_bid`` 和 ``AVG(normalized_price_per_unit)`` 直接
    拿去比较，但前者 (SPEC 语境里更像是"整批成本价") 和后者 (明确是"分/克"
    单价) 不是同一个计价口径——直接比较是在拿苹果对橙子。这里改成让调用方
    显式申报两个独立数字：supplier_bid_price_per_gram (跟基准线同一口径，
    只用来核验毛利红线) 和 batch_cost_price (真正写回 inventory_batches.
    cost_price 的整批成本)，不在 omodul 内部做一个需要"这批货多少克"才能算
    对、而 SPEC 又没给这个数据的隐式换算。

    基准线用 price_benchmark.variant_id = 批次的 variant_id 关联查询 (统一
    之后是真 FK，不再是靠字符串对齐的 sku_id)。这个 variant 完全没有历史
    基线数据时直接判 failed，不假装算出一条基准线放行 (没有参照就没法核验
    毛利红线，不能算通过)。

    Args:
        config: SubmitSupplierReverseAuctionWorkflowConfig。
        input_data: batch_id / supplier_bid_price_per_gram / batch_cost_price。
        output_dir: decision_trail 落盘目录。
        pool: obase.persistence.PgPool。必须落库，pool 为 None 直接判 failed。
        on_step: 进度回调，可选。

    Returns:
        标准 omodul 返回 dict；completed 时含 action ("batch_approved") /
        benchmark_price (核验用的基准均价)。margin_too_low / 无基线数据都判
        failed，error.message 里说明具体原因。
    """
    trail = Trail()
    fp = compute_fingerprint({"batch_id": input_data.batch_id})

    try:
        if pool is None:
            raise ValueError(
                "pool is required — submit_supplier_reverse_auction_workflow always touches persisted stock"
            )
        if input_data.supplier_bid_price_per_gram < 0:
            raise ValueError("supplier_bid_price_per_gram must be non-negative")
        if input_data.batch_cost_price <= 0:
            raise ValueError("batch_cost_price must be positive")

        async with pool.acquire() as conn:
            batch = await conn.fetchrow(
                'SELECT variant_id FROM "inventory_batch" WHERE id = $1',
                input_data.batch_id,
            )
            if batch is None:
                raise ValueError(f"batch {input_data.batch_id!r} not found")

            avg_benchmark = await conn.fetchval(
                'SELECT AVG(normalized_price_per_unit) FROM "price_benchmark" '
                "WHERE variant_id = $1",
                batch["variant_id"],
            )
        if avg_benchmark is None:
            raise ValueError(
                f"no price benchmark data for variant {batch['variant_id']!r} yet; "
                "cannot validate margin without a reference"
            )
        benchmark_price = float(avg_benchmark)
        trail.record(event="benchmark_loaded", benchmark_price=benchmark_price)

        approved = validate_supplier_margin(
            input_data.supplier_bid_price_per_gram, benchmark_price=benchmark_price
        )
        trail.record(
            event="margin_validated",
            approved=approved,
            supplier_bid_price_per_gram=input_data.supplier_bid_price_per_gram,
        )
        if not approved:
            raise ValueError(
                "margin_too_low: supplier bid does not clear the 50% margin red line"
            )

        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE \"inventory_batch\" SET cost_price_cents = $1, status = 'in_transit' "
                "WHERE id = $2",
                input_data.batch_cost_price,
                input_data.batch_id,
            )
        trail.record(
            event="batch_approved",
            batch_id=input_data.batch_id,
            cost_price=input_data.batch_cost_price,
        )

        trail_path = trail.write(output_dir)
        return build_result(
            status="completed",
            error=None,
            fingerprint=fp,
            trail=trail,
            trail_path=trail_path,
            action="batch_approved",
            benchmark_price=benchmark_price,
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
