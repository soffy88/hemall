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


class ManualPaymentGateway:
    """补天计划 Task 1.3 — 收款网关骨架：统一下单 (Prepay) 与退款 (Refund)。

    架构师指令：把收款通道的入参/出参结构先实装出来，确保随时可接入真实密钥。
    跟 ManualPayoutProvider (付钱方向) 不同，这是收钱方向 (顾客→平台) 的网关
    占位实现：内存态，统一下单直接生成一个模拟 prepay 单，退款直接成功。

    真实接入点 (实现替换本类同名方法即可，签名不变)：
        - 微信支付: 统一下单 v3 API (POST /v3/pay/transactions/native)，
          prepay() 返回的 code_url 就是 native 支付的二维码链接；回调验签走
          app/security/webhook.py 的 HMAC 装甲 (真实环境替换为微信平台证书验签)。
        - Stripe: PaymentIntent (mode=payment)，prepay() 返回 payment_intent_id
          + client_secret，前端用 Stripe.js 完成 3DS 确认。
        - 密钥来源约定 (bootstrap 注册时注入，避免散落在代码里)：
          HEMALL_WECHAT_MCH_ID / HEMALL_WECHAT_API_V3_KEY / HEMALL_STRIPE_SECRET_KEY

    prepay() 输入 (跟微信支付 v3 的字段一一对应，用分作金额单位)：
        out_trade_no: 商户订单号 (幂等键，微信侧重复提交返回同一单)。
        total_fee_cents: 订单金额 (分)。
        description: 商品描述。
        notify_url: 回调地址 (平台侧 /payments/wechat/notify)。

    refund() 输入：
        out_trade_no: 原支付商户订单号。
        out_refund_no: 商户退款单号 (幂等键)。
        refund_fee_cents: 退款金额 (分)。
        reason: 退款原因。
    """

    def __init__(self, *, merchant_id: str = "", api_key: str = "") -> None:
        # 真实接入时从环境注入；内存态实现不需要密钥也能跑通全链路。
        self._merchant_id = merchant_id
        self._api_key = api_key
        self._prepays: dict[str, dict[str, Any]] = {}
        self._refunds: dict[str, dict[str, Any]] = {}

    async def prepay(
        self,
        *,
        out_trade_no: str,
        total_fee_cents: int,
        description: str,
        notify_url: str,
    ) -> dict[str, Any]:
        """统一下单。返回结构同时覆盖微信 native 支付与 Stripe PaymentIntent 两种形态。"""
        if total_fee_cents <= 0:
            raise ValueError("prepay: total_fee_cents must be positive")
        if out_trade_no in self._prepays:
            return self._prepays[out_trade_no]

        record = {
            "out_trade_no": out_trade_no,
            "total_fee_cents": total_fee_cents,
            "description": description,
            "notify_url": notify_url,
            "status": "pending",
            # 微信 native: 二维码链接 (真实实现来自统一下单 v3 响应 code_url)。
            "code_url": f"weixin://wxpay/bizpayurl?pr=MOCK_{out_trade_no[-12:]}",
            "prepay_id": f"wx_mock_prepay_{out_trade_no[-12:]}",
            # Stripe: PaymentIntent 形态 (真实实现来自 PaymentIntent.create)。
            "payment_intent_id": None,
            "client_secret": None,
        }
        self._prepays[out_trade_no] = record
        return record

    async def stripe_prepay(
        self,
        *,
        out_trade_no: str,
        total_fee_cents: int,
        description: str,
    ) -> dict[str, Any]:
        """Stripe 专属统一下单形态 (PaymentIntent)。"""
        record = {
            "out_trade_no": out_trade_no,
            "total_fee_cents": total_fee_cents,
            "description": description,
            "status": "requires_payment_method",
            "payment_intent_id": f"pi_mock_{out_trade_no[-12:]}",
            "client_secret": f"pi_mock_secret_{out_trade_no[-12:]}_secret",
            "code_url": None,
            "prepay_id": None,
        }
        self._prepays[out_trade_no] = record
        return record

    async def refund(
        self,
        *,
        out_trade_no: str,
        out_refund_no: str,
        refund_fee_cents: int,
        reason: str = "",
    ) -> dict[str, Any]:
        """退款。真实实现调微信 v3 退款 API / Stripe Refund.create。"""
        if refund_fee_cents <= 0:
            raise ValueError("refund: refund_fee_cents must be positive")
        record = {
            "out_refund_no": out_refund_no,
            "out_trade_no": out_trade_no,
            "refund_fee_cents": refund_fee_cents,
            "reason": reason,
            "status": "success",  # 真实实现为 PROCESSING/SUCCESS/CLOSED
            "refund_id": f"wx_mock_refund_{out_refund_no[-12:]}",
        }
        self._refunds[out_refund_no] = record
        return record
