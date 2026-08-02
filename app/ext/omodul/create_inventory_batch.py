"""app.ext.omodul.create_inventory_batch — 批次入库 (统一改造后的本地薄封装)。

**统一改造说明**：这个函数以前自己往平行的 inventory_batches 表插入
一整行，跟 platform/3O 共享包里同名的 omodul.create_inventory_batch 是两套
完全独立、互不相关的实现，只是恰好同名。统一之后，这里改成"复用共享核心 +
打本地补丁列"：先调共享的 omodul.create_inventory_batch 完成核心插入 (video_url
校验/库存校验/uuid7 生成/质检状态判定，逻辑一个字都不重新实现)，再在同一次
调用里对刚插入的那一行做一次本地 UPDATE，把共享包不知道的本地列
(expiration_time/supplier_id) 补上。这是本轮统一里"薄封装"策略的样板函数。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from omodul._base import BaseConfig, Trail, build_result, compute_fingerprint


class CreateInventoryBatchConfig(BaseConfig):
    _omodul_name: ClassVar[str] = "create_inventory_batch"
    _omodul_version: ClassVar[str] = "2.0.0"
    _fingerprint_fields: ClassVar[set[str]] = {"variant_id", "location_id", "video_url"}
    _enabled_pillars: ClassVar[set[str]] = {"fingerprint", "decision_trail"}

    default_currency: str = "CNY"


class CreateInventoryBatchInput(BaseModel):
    variant_id: str
    location_id: str
    video_url: str
    stock_qty: int
    cost_price: int
    retail_price: int
    expiration_time: str | None = None
    supplier_id: str | None = None


async def create_inventory_batch(
    config: CreateInventoryBatchConfig,
    input_data: CreateInventoryBatchInput,
    output_dir: Path,
    *,
    pool: Any = None,
    on_step: Any = None,
) -> dict:
    """批次入库：复用共享 omodul.create_inventory_batch 做核心插入，本地补
    expiration_time/supplier_id 两列 (共享 inventory_batch 表在 hemall 本地
    打过补丁，见 app/ext/schema.py::_ensure_shared_table_local_columns)。

    Args:
        config: CreateInventoryBatchConfig。
        input_data: variant_id / location_id / video_url / stock_qty /
            cost_price / retail_price / expiration_time(可选) / supplier_id(可选)。
        output_dir: decision_trail 落盘目录。
        pool: obase.persistence.PgPool。必须落库，pool 为 None 直接判 failed。
        on_step: 进度回调，可选。

    Returns:
        标准 omodul 返回 dict；completed 时含 batch_id / variant_id / retail_price。
    """
    from omodul.create_inventory_batch import (
        CreateInventoryBatchConfig as SharedConfig,
    )
    from omodul.create_inventory_batch import (
        CreateInventoryBatchInput as SharedInput,
    )
    from omodul.create_inventory_batch import (
        create_inventory_batch as shared_create_inventory_batch,
    )

    trail = Trail()
    fp = compute_fingerprint(
        {
            "variant_id": input_data.variant_id,
            "location_id": input_data.location_id,
            "video_url": input_data.video_url,
        }
    )

    try:
        if pool is None:
            raise ValueError(
                "pool is required — create_inventory_batch always touches persisted stock"
            )

        # batch_no 是共享 schema 的必填唯一列，旧版 Input 从没有
        # 过这个字段——用 fingerprint 当 batch_no，保证同一批次重复提交能被
        # 共享层的幂等语义识别 (跟共享 omodul 自己的 compute_fingerprint_for
        # 用同一批字段是同一个道理)。
        shared_result = await shared_create_inventory_batch(
            SharedConfig(default_currency=config.default_currency),
            SharedInput(
                variant_id=input_data.variant_id,
                location_id=input_data.location_id,
                batch_no=fp,
                video_url=input_data.video_url,
                cost_price_cents=input_data.cost_price,
                retail_price_cents=input_data.retail_price,
                stock_qty=input_data.stock_qty,
                inspected_by="ext-auto",
            ),
            output_dir,
            pool=pool,
        )
        if shared_result["status"] == "failed":
            raise ValueError(shared_result["error"]["message"])

        batch_id = shared_result["batch_id"]
        trail.record(event="shared_batch_created", batch_id=batch_id)

        # asyncpg 对 TIMESTAMPTZ 参数要求真 datetime 对象，不接受 ISO 字符串——
        # Input.expiration_time 保持 str (HTTP JSON 契约里时间戳本来就是字符
        # 串)，这里解析成 datetime 再传给驱动。
        expiration_dt = (
            datetime.fromisoformat(input_data.expiration_time)
            if input_data.expiration_time
            else None
        )
        async with pool.acquire() as conn:
            await conn.execute(
                'UPDATE "inventory_batch" SET expiration_time = $1, supplier_id = $2 '
                "WHERE id = $3",
                expiration_dt,
                input_data.supplier_id,
                batch_id,
            )
        trail.record(
            event="local_columns_patched",
            batch_id=batch_id,
            expiration_time=input_data.expiration_time,
            supplier_id=input_data.supplier_id,
        )

        trail_path = trail.write(output_dir)
        return build_result(
            status="completed",
            error=None,
            fingerprint=fp,
            trail=trail,
            trail_path=trail_path,
            batch_id=batch_id,
            variant_id=input_data.variant_id,
            retail_price=input_data.retail_price,
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
