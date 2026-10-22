"""微信小程序登录 — jscode2session 客户端。

小程序端 wx.login() 拿到临时 code 后，服务端用它向微信换取 openid /
unionid / session_key：
https://developers.weixin.qq.com/miniprogram/dev/api-backend/open-api/login/auth.code2Session.html

安全口径：
- session_key 是会话密钥，**不落库、不进日志、不回前端**；本模块只把它
  放在返回值里，调用方不得持久化或下发。
- app_secret 只出现在发往 api.weixin.qq.com 的请求参数里，不进日志；
  微信侧 errmsg 只记 errcode，不把原始响应全文写进日志/响应。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("hemall.wechat_mp")

JSCODE2SESSION_URL = "https://api.weixin.qq.com/sns/jscode2session"


class WeChatMpError(Exception):
    """微信 jscode2session 返回业务错误（errcode != 0 或响应缺 openid）。"""

    def __init__(self, errcode: int, errmsg: str = "") -> None:
        super().__init__(f"wechat jscode2session errcode={errcode}: {errmsg}")
        self.errcode = errcode
        self.errmsg = errmsg


async def jscode2session(
    app_id: str,
    app_secret: str,
    code: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """用小程序登录 code 换 openid/unionid/session_key。

    成功返回 {"openid", "unionid", "session_key"}（unionid / session_key
    可能为 None）。微信侧 errcode 非 0 或响应缺 openid 时抛 WeChatMpError；
    网络层异常以 httpx.HTTPError 原样抛出，由调用方映射状态码。
    """
    params = {
        "appid": app_id,
        "secret": app_secret,
        "js_code": code,
        "grant_type": "authorization_code",
    }
    async with httpx.AsyncClient(timeout=10.0, transport=transport) as client:
        resp = await client.get(JSCODE2SESSION_URL, params=params)
    data = resp.json()
    errcode = int(data.get("errcode") or 0)
    if errcode != 0:
        logger.warning("jscode2session failed errcode=%s", errcode)
        raise WeChatMpError(errcode, str(data.get("errmsg") or ""))
    openid = data.get("openid")
    if not openid:
        logger.warning("jscode2session response missing openid")
        raise WeChatMpError(-1, "missing openid in wechat response")
    return {
        "openid": str(openid),
        "unionid": data.get("unionid"),
        "session_key": data.get("session_key"),
    }
