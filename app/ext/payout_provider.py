"""app.ext.payout_provider — 打款 provider (供应商货款/工资/宿主分润)。

obase.payment_providers 只有面向"向顾客收款"的 authorize/capture/refund/cancel
状态机 (ManualPaymentProvider)，没有"主动向供应商/工人/宿主账户打钱"这个
方向的能力——语义上是两种不同的 provider category ("payment" 收钱 vs
"payout" 付钱)，obase 目前没有现成实现。这里在 hemall 项目层补一个最小的
内存态实现，注册进同一个 obase.provider_registry.ProviderRegistry (category
="payout")，跟 ManualPaymentProvider 走一样的"内存状态机、生产环境替换成真
实网关"套路，不是另起炉灶。
"""

from __future__ import annotations

import uuid
from typing import Any


class ManualPayoutProvider:
    """内存态打款 provider：模拟微信企业付款/银行对公转账，进程生命周期内维持流水。

    v2.0 追加 escrow()：跟 transfer() (立即到账) 是两套独立的状态机——escrow
    模拟"高级分账网关的资金托管"能力，把钱锁到 release_date 才能真正到账，
    随时可能被 execute_slashing_workflow 那类仲裁流程拦截扣走，不是简单的
    延迟转账。SPEC 目前没有指名哪个 omodul 调 ext_trigger_smart_escrow (供应商
    escrow_balance 的扣款走的是 execute_liability_routing_workflow /
    execute_slashing_workflow 直接改 DB 列，不经过这个 provider)，这里只是把
    "高级分账托管"这个外部能力先做成可用的原语，接哪个业务流程留给以后。
    """

    def __init__(self) -> None:
        self._transfers: dict[str, dict[str, Any]] = {}
        self._escrows: dict[str, dict[str, Any]] = {}

    async def transfer(
        self, *, account: str, amount: int, meta: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        transfer_id = f"payout_{uuid.uuid4().hex[:16]}"
        record = {
            "transfer_id": transfer_id,
            "account": account,
            "amount": amount,
            "status": "completed",
            "meta": meta or {},
        }
        self._transfers[transfer_id] = record
        return record

    async def escrow(
        self, *, account: str, amount: int, release_date: Any
    ) -> dict[str, Any]:
        """把资金锁定至 release_date 才能到账 (T+7 分账托管)。"""
        escrow_id = f"escrow_{uuid.uuid4().hex[:16]}"
        record = {
            "escrow_id": escrow_id,
            "account": account,
            "amount": amount,
            "release_date": release_date,
            "status": "locked",
        }
        self._escrows[escrow_id] = record
        return record
