"""app.ext.omodul.bind_digital_lord_contract_workflow — 数字领主永久契约册封。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from omodul._base import BaseConfig, Trail, build_result, compute_fingerprint


class BindDigitalLordContractWorkflowConfig(BaseConfig):
    _omodul_name: ClassVar[str] = "bind_digital_lord_contract_workflow"
    _omodul_version: ClassVar[str] = "1.0.0"
    _fingerprint_fields: ClassVar[set[str]] = {"douyin_uid", "location_id"}
    _enabled_pillars: ClassVar[set[str]] = {"decision_trail"}


class BindDigitalLordContractWorkflowInput(BaseModel):
    douyin_uid: str
    location_id: str


async def bind_digital_lord_contract_workflow(
    config: BindDigitalLordContractWorkflowConfig,
    input_data: BindDigitalLordContractWorkflowInput,
    output_dir: Path,
    *,
    pool: Any = None,
    on_step: Any = None,
) -> dict:
    """某个陌生小区意向金达成 500 单 ("点火成功") 时触发：把该节点的"永久
    领主税"契约写入底层，往后这个节点产生的每一笔交易都给这位达人抽成。

    "500 单达标" 的判定不在这个 omodul 内部——crowd_intents 表按 variant_id
    (商品) 记意向单，没有 location_id 维度，跟"某个小区节点是否点火"完全是
    两个不同的计数维度，SPEC 没给"小区级意向单计数"这张表/这条链路，属于
    "调用方提供无法推导的数据"这个项目一贯的处理方式 (同 tote_count /
    batch_cost_price 等)——调用方 (运营后台) 自行判断"这个节点是否已经达标"
    后再调用这个 omodul，本函数只负责契约本身的原子写入 + 幂等/冲突处理。

    幂等 + 排他处理 (SPEC 原文只有 ON CONFLICT DO NOTHING，但没说 DO NOTHING
    的冲突键是什么——PK 是随机生成的 contract_id，天然不会冲突，SPEC 原文的
    ON CONFLICT 实际上永远不会触发，是一句没有意义的占位):
      - 该节点已经是这位达人的领地 (重复调用) → 幂等 no-op，返回已有 contract_id。
      - 该节点已经被另一位达人占了 → 拒绝 (判 failed；一个节点只能有一个
        创始领主，不能被后来者顶替)。
      - 该节点还没有领主 → 正常册封，同时把节点从 pending_hardware 促活成
        active ("点火" 字面意思就是从"认领待激活"转为"正式运营"，这是
        process_cloud_franchise_claim_workflow 和这个 omodul 唯一的衔接点，
        SPEC 两段代码分开写但显然是同一条业务链路的前后两步)。

    Args:
        config: BindDigitalLordContractWorkflowConfig。
        input_data: douyin_uid / location_id。
        output_dir: decision_trail 落盘目录。
        pool: obase.persistence.PgPool。必须落库，pool 为 None 直接判 failed。
        on_step: 进度回调，可选。

    Returns:
        标准 omodul 返回 dict；completed 时含 action
        ("bound"/"already_lord") / contract_id。
    """
    trail = Trail()
    fp = compute_fingerprint(
        {"douyin_uid": input_data.douyin_uid, "location_id": input_data.location_id}
    )

    try:
        if pool is None:
            raise ValueError(
                "pool is required — bind_digital_lord_contract_workflow always touches persisted stock"
            )

        async with pool.acquire() as conn:
            location = await conn.fetchrow(
                'SELECT id, status FROM "stock_location" WHERE id = $1',
                input_data.location_id,
            )
            if location is None:
                raise ValueError(f"location {input_data.location_id!r} not found")

            existing = await conn.fetchrow(
                'SELECT id, douyin_uid FROM "affiliate_contract" '
                "WHERE contract_type = 'digital_lord' AND bound_entity_id = $1 "
                "AND status = 'active'",
                input_data.location_id,
            )

        if existing is not None:
            if existing["douyin_uid"] == input_data.douyin_uid:
                trail.record(event="already_lord", contract_id=str(existing["id"]))
                trail_path = trail.write(output_dir)
                return build_result(
                    status="completed",
                    error=None,
                    fingerprint=fp,
                    trail=trail,
                    trail_path=trail_path,
                    action="already_lord",
                    contract_id=str(existing["id"]),
                )
            raise ValueError(
                f"location {input_data.location_id!r} already has an active "
                f"digital lord ({existing['douyin_uid']!r}); cannot be reassigned"
            )

        from obase.uuid7 import uuid7

        contract_id = uuid7()
        commission_logic = {"tax_rate": 0.002, "type": "perpetual_volume_tax"}
        async with pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO "affiliate_contract" '
                "(id, douyin_uid, contract_type, bound_entity_id, commission_logic, status) "
                "VALUES ($1, $2, 'digital_lord', $3, $4, 'active')",
                contract_id,
                input_data.douyin_uid,
                input_data.location_id,
                json.dumps(commission_logic),
            )
            if location["status"] == "pending_hardware":
                await conn.execute(
                    "UPDATE \"stock_location\" SET status = 'active' WHERE id = $1",
                    input_data.location_id,
                )
                trail.record(event="node_ignited", location_id=input_data.location_id)

        trail.record(
            event="lord_contract_bound",
            contract_id=contract_id,
            douyin_uid=input_data.douyin_uid,
            location_id=input_data.location_id,
        )

        trail_path = trail.write(output_dir)
        return build_result(
            status="completed",
            error=None,
            fingerprint=fp,
            trail=trail,
            trail_path=trail_path,
            action="bound",
            contract_id=contract_id,
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
