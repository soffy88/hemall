"""app.ext.wechat_provider — 内存态微信视频号发版 provider。

obase 没有"微信视频号 API"这个 provider 类别 (跟 weather/vlm/cv/llm 一样，是
hemall 扩展域特有的外部依赖)。真实实现应该接微信视频号带货组件的
``/channels/ec/video/add`` 接口；这个开发环境里没有真实的 access_token/商家号
资质，风格对齐 ManualVLMProvider：进程内内存态，默认返回成功 (errcode=0 +
生成一个 feed_id)，可用 set_response 覆写指定调用的返回结果 (模拟风控拒绝/
超时之类的失败场景)，供测试/演练用。真实网关接入时只要注册一个新的
"wechat_channel" provider 实现替换掉它，oprim.ext_wechat_channel_publish
及以上都不用改。
"""

from __future__ import annotations

import uuid
from typing import Any


class ManualWeChatChannelProvider:
    """内存态微信视频号 provider：默认发布成功，可用 set_response 覆写指定结果。"""

    def __init__(self) -> None:
        self._overrides: dict[str, dict[str, Any]] = {}

    def set_response(self, *, mp_path: str, response: dict[str, Any]) -> None:
        """覆写指定 mini_program_path 这次调用的返回结果 (测试/演练用)。"""
        self._overrides[mp_path] = response

    async def channel_publish(
        self, *, video_url: str, copy_text: str, mp_path: str, access_token: str
    ) -> dict[str, Any]:
        """把视频源、文案与带参小程序路径打包推流。"""
        override = self._overrides.get(mp_path)
        if override is not None:
            return override
        return {
            "errcode": 0,
            "errmsg": "ok",
            "feed_id": f"feed_{uuid.uuid4().hex[:16]}",
        }
