"""app.ext.omodul.execute_channel_broadcast_workflow — 微信视频号自动播报。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import asyncpg
from pydantic import BaseModel

from omodul._base import BaseConfig, Trail, build_result, compute_fingerprint

from ..oprim import ext_llm_generate_text, ext_wechat_channel_publish
from ..oskill import build_fomo_system_prompt, construct_fomo_user_prompt


class ExecuteChannelBroadcastWorkflowConfig(BaseConfig):
    _omodul_name: ClassVar[str] = "execute_channel_broadcast_workflow"
    _omodul_version: ClassVar[str] = "1.0.0"
    _fingerprint_fields: ClassVar[set[str]] = {"batch_id", "broadcast_type"}
    _enabled_pillars: ClassVar[set[str]] = {"decision_trail"}

    llm_provider: str = "manual"
    wechat_provider: str = "manual"


class ExecuteChannelBroadcastWorkflowInput(BaseModel):
    batch_id: str
    broadcast_type: str  # "fresh_arrival" | "clearance"
    market_price: int
    access_token: str


async def execute_channel_broadcast_workflow(
    config: ExecuteChannelBroadcastWorkflowConfig,
    input_data: ExecuteChannelBroadcastWorkflowInput,
    output_dir: Path,
    *,
    pool: Any = None,
    on_step: Any = None,
) -> dict:
    """完整的静默发版物理事务：取批次真实数据 → LLM 生成恐慌感文案 → 落库审计
    → 推流微信视频号 → 回写发布结果。

    防重发用 channel_broadcast_log 的 (batch_id, broadcast_type) 唯一索引
    做原子拦截，不是"先 SELECT 查有没有、没有再 INSERT"那种检查后再执行的
    两步走 (那样在高并发下会有竞态窗口——两个协程同时通过 SELECT 检查，
    都以为自己是第一个，一起插入)。这里反过来：先把 LLM 文案生成好 (schema
    要求 llm_copywriting NOT NULL，没有文案没法插入)，直接尝试 INSERT，
    唯一索引冲突就是"已经播报过"的权威判断，让 PostgreSQL 自己的约束保证
    正确性——跟 db_lock_batch_inventory 用单条原子 UPDATE 防超卖是同一个
    工程哲学。

    Args:
        config: ExecuteChannelBroadcastWorkflowConfig(llm_provider="manual",
            wechat_provider="manual")。
        input_data: batch_id / broadcast_type ("fresh_arrival"/"clearance") /
            market_price (传统商超参考价，分) / access_token (微信视频号
            access_token；真实获取需要走微信 OAuth 中控刷新流程，这里假定
            调用方已经拿到，不在这个 omodul 里实现)。
        output_dir: decision_trail 落盘目录。
        pool: obase.persistence.PgPool。必须落库，pool 为 None 直接判 failed。
        on_step: 进度回调，可选。

    Returns:
        标准 omodul 返回 dict；completed 时含 log_id / feed_id / copywriting。
        failed 时如果已经写过 pending 行 (LLM 生成成功、微信推流失败)，
        channel_broadcast_log 里对应行会被标记 status='failed' 留痕，不是
        静默丢弃。
    """
    trail = Trail()
    fp = compute_fingerprint(
        {"batch_id": input_data.batch_id, "broadcast_type": input_data.broadcast_type}
    )

    try:
        if pool is None:
            raise ValueError(
                "pool is required — execute_channel_broadcast_workflow always touches persisted stock"
            )
        if input_data.broadcast_type not in ("fresh_arrival", "clearance"):
            raise ValueError(
                f"broadcast_type must be 'fresh_arrival' or 'clearance', "
                f"got {input_data.broadcast_type!r}"
            )
        if input_data.market_price <= 0:
            raise ValueError("market_price must be positive")

        async with pool.acquire() as conn:
            batch = await conn.fetchrow(
                "SELECT b.id, b.video_url, b.retail_price_cents AS retail_price, "
                "b.stock_qty, v.sku_code AS variant_desc, "
                "s.polygon_name AS supplier_polygon_name "
                'FROM "inventory_batch" b '
                'JOIN "product_variant" v ON b.variant_id = v.id '
                'LEFT JOIN "supplier" s ON b.supplier_id = s.id '
                "WHERE b.id = $1 AND b.status = 'active'",
                input_data.batch_id,
            )
        if batch is None or batch["stock_qty"] <= 0:
            raise ValueError(
                f"batch {input_data.batch_id!r} not found, inactive, or stock depleted"
            )
        trail.record(
            event="batch_loaded",
            batch_id=str(batch["id"]),
            stock_qty=batch["stock_qty"],
        )

        batch_info = dict(batch)
        batch_info["supplier_polygon_name"] = (
            batch_info["supplier_polygon_name"] or "溯源信息保密"
        )
        batch_info["broadcast_type"] = input_data.broadcast_type

        sys_prompt = build_fomo_system_prompt()
        usr_prompt = construct_fomo_user_prompt(
            batch_info, market_price=input_data.market_price
        )

        try:
            copywriting = await ext_llm_generate_text(
                config.llm_provider, system_prompt=sys_prompt, user_prompt=usr_prompt
            )
        except Exception as exc:
            raise ValueError(f"llm generation failed: {exc}") from exc
        trail.record(event="copywriting_generated", length=len(copywriting))

        mp_path = f"pages/checkout/direct?batch_id={input_data.batch_id}"

        from obase.uuid7 import uuid7

        log_id = uuid7()
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    'INSERT INTO "channel_broadcast_log" '
                    "(id, batch_id, broadcast_type, llm_copywriting, mini_program_path) "
                    "VALUES ($1, $2, $3, $4, $5)",
                    log_id,
                    input_data.batch_id,
                    input_data.broadcast_type,
                    copywriting,
                    mp_path,
                )
        except asyncpg.UniqueViolationError as exc:
            raise ValueError(
                f"batch {input_data.batch_id!r} already broadcasted for "
                f"type {input_data.broadcast_type!r}"
            ) from exc
        trail.record(event="log_recorded_pending", log_id=log_id)

        try:
            wx_resp = await ext_wechat_channel_publish(
                config.wechat_provider,
                video_url=batch["video_url"],
                copy_text=copywriting,
                mp_path=mp_path,
                access_token=input_data.access_token,
            )
        except Exception as exc:
            async with pool.acquire() as conn:
                await conn.execute(
                    'UPDATE "channel_broadcast_log" '
                    "SET status = 'failed', error_message = $1 WHERE id = $2",
                    str(exc),
                    log_id,
                )
            raise ValueError(f"wechat publish network error: {exc}") from exc

        if wx_resp.get("errcode") != 0:
            async with pool.acquire() as conn:
                await conn.execute(
                    'UPDATE "channel_broadcast_log" '
                    "SET status = 'failed', error_message = $1 WHERE id = $2",
                    json.dumps(wx_resp),
                    log_id,
                )
            raise ValueError(f"wechat api rejected: {wx_resp}")

        feed_id = wx_resp.get("feed_id", "unknown_feed")
        async with pool.acquire() as conn:
            await conn.execute(
                'UPDATE "channel_broadcast_log" '
                "SET status = 'published', wechat_feed_id = $1, published_at = NOW() "
                "WHERE id = $2",
                feed_id,
                log_id,
            )
        trail.record(event="wechat_publish_success", feed_id=feed_id)

        trail_path = trail.write(output_dir)
        return build_result(
            status="completed",
            error=None,
            fingerprint=fp,
            trail=trail,
            trail_path=trail_path,
            log_id=log_id,
            feed_id=feed_id,
            copywriting=copywriting,
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
