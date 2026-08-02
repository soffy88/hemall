"""app.ext.douyin_provider — 内存态抖音开放平台网关 provider。

obase 没有"抖音精选联盟/本地生活开放平台"这个 provider 类别 (跟 weather/vlm/
cv/llm/wechat_channel/spider 一样，是 hemall 扩展域特有的外部依赖)。真实实现
应该接字节跳动开放平台的 ``/local_life/sku/sync`` 接口；这个开发环境里没有
真实的 access_token/本地生活资质，风格对齐 ManualWeChatChannelProvider：
进程内内存态，默认返回成功 (err_no=0)，可用 set_response 覆写指定 SKU 这次
调用的返回结果 (模拟风控拒绝/额度超限之类的失败场景)，供测试/演练用。真实
网关接入时只要注册一个新的 "douyin" provider 实现替换掉它，
oprim.ext_douyin_sync_inventory 及以上都不用改。

给达人打款复用已有的 "payout" provider category (ManualPayoutProvider /
oprim.ext_pay_transfer)——SPEC 自己的 ext_pay_transfer_to_creator 描述是
"复用底层的支付网关能力"，字面上就是同一个东西，不重复定义一个新 provider
类别。
"""

from __future__ import annotations

from typing import Any


class ManualDouyinProvider:
    """内存态抖音开放平台 provider：默认同步成功，可用 set_response 覆写指定结果。"""

    def __init__(self) -> None:
        self._overrides: dict[str, dict[str, Any]] = {}

    def set_response(self, *, sku_id: str, response: dict[str, Any]) -> None:
        """覆写指定 sku_id 这次调用的返回结果 (测试/演练用)。"""
        self._overrides[sku_id] = response

    async def sync_inventory(
        self,
        *,
        sku_id: str,
        price: int,
        stock: int,
        commission_rate: float,
        access_token: str,
    ) -> dict[str, Any]:
        """向抖音精选联盟/本地生活强行推入或更新一个带高额佣金的临期 SKU。"""
        override = self._overrides.get(sku_id)
        if override is not None:
            return override
        return {
            "err_no": 0,
            "err_msg": "success",
            "out_sku_id": sku_id,
            "commission_rate": round(commission_rate * 10000),
        }
