"""微信小程序登录 — jscode2session 客户端。

小程序端 wx.login() 拿到临时 code 后，服务端用它向微信换取 openid /
unionid / session_key：
https://developers.weixin.qq.com/miniprogram/dev/api-backend/open-api/login/auth.code2Session.html

安全口径：
- session_key 是会话密钥，**不落库、不进日志、不回前端**；本模块只把它
  放在返回值里，调用方不得持久化或下发。
- app_secret 只出现在发往 api.weixin.qq.com 的请求参数里，不进日志；
  微信侧 errcode 只记 errcode，不把原始响应全文写进日志/响应。

扩展功能：
- 手机号登录 (getPhoneNumber)：使用 session_key 解密手机号
- 订阅消息 (subscribe message)：下发模板消息
- 小程序码 (wxacode)：生成带参数的小程序码
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

logger = logging.getLogger("hemall.wechat_mp")

JSCODE2SESSION_URL = "https://api.weixin.qq.com/sns/jscode2session"
GET_PHONE_NUMBER_URL = "https://api.weixin.qq.com/wxa/business/getuserphonenumber"
SUBSCRIBE_MESSAGE_URL = "https://api.weixin.qq.com/cgi-bin/message/subscribe/send"
WXACODE_URL = "https://api.weixin.qq.com/wxa/getwxacodeunlimit"
ACCESS_TOKEN_URL = "https://api.weixin.qq.com/cgi-bin/token"


class WeChatMpError(Exception):
    """微信 API 返回业务错误（errcode != 0）。"""

    def __init__(self, errcode: int, errmsg: str = "") -> None:
        super().__init__(f"wechat mp errcode={errcode}: {errmsg}")
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


async def get_access_token(
    app_id: str,
    app_secret: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """获取微信 access_token (用于订阅消息、小程序码等接口)。"""
    params = {
        "grant_type": "client_credential",
        "appid": app_id,
        "secret": app_secret,
    }
    async with httpx.AsyncClient(timeout=10.0, transport=transport) as client:
        resp = await client.get(ACCESS_TOKEN_URL, params=params)
    data = resp.json()
    errcode = int(data.get("errcode") or 0)
    if errcode != 0:
        logger.warning("get_access_token failed errcode=%s", errcode)
        raise WeChatMpError(errcode, str(data.get("errmsg") or ""))
    access_token = data.get("access_token")
    if not access_token:
        raise WeChatMpError(-1, "missing access_token in wechat response")
    return str(access_token)


def _decrypt_session_data(session_key: str, encrypted_data: str, iv: str) -> dict[str, Any]:
    """解密微信加密数据 (AES-128-CBC, PKCS7 padding)。"""
    # base64 解码
    session_key_bytes = base64.b64decode(session_key)
    encrypted_data_bytes = base64.b64decode(encrypted_data)
    iv_bytes = base64.b64decode(iv)

    # AES-128-CBC 解密
    cipher = AES.new(session_key_bytes, AES.MODE_CBC, iv_bytes)
    decrypted = unpad(cipher.decrypt(encrypted_data_bytes), AES.block_size)
    import json

    return json.loads(decrypted.decode("utf-8"))


async def get_phone_number(
    app_id: str,
    app_secret: str,
    code: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """用 code 换取用户手机号 (微信后台接口 getPhoneNumber)。

    成功返回 {"phone_number", "pure_phone_number", "country_code", "watermark"}。
    需要先用 jscode2session 获取 session_key，再用 code 换取手机号。
    """
    # 先获取 session_key
    session = await jscode2session(app_id, app_secret, code, transport)
    session_key = session.get("session_key")
    if not session_key:
        raise WeChatMpError(-1, "missing session_key")

    # 获取 access_token
    access_token = await get_access_token(app_id, app_secret, transport)

    # 调用 getPhoneNumber 接口
    async with httpx.AsyncClient(timeout=10.0, transport=transport) as client:
        resp = await client.post(
            GET_PHONE_NUMBER_URL,
            params={"access_token": access_token},
            json={"code": code},
        )
    data = resp.json()
    errcode = int(data.get("errcode") or 0)
    if errcode != 0:
        logger.warning("getPhoneNumber failed errcode=%s", errcode)
        raise WeChatMpError(errcode, str(data.get("errmsg") or ""))

    phone_info = data.get("phone_info") or {}
    return {
        "phone_number": phone_info.get("phoneNumber"),
        "pure_phone_number": phone_info.get("purePhoneNumber"),
        "country_code": phone_info.get("countryCode"),
        "watermark": phone_info.get("watermark"),
    }


async def send_subscribe_message(
    app_id: str,
    app_secret: str,
    openid: str,
    template_id: str,
    page: str,
    data: dict[str, Any],
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """下发订阅消息 (模板消息)。

    参数:
    - openid: 用户 openid
    - template_id: 订阅消息模板 ID (在微信公众平台配置)
    - page: 点击消息跳转的小程序页面路径
    - data: 消息内容数据，格式为 { "key": {"value": "xxx"} }
    """
    access_token = await get_access_token(app_id, app_secret, transport)

    payload = {
        "touser": openid,
        "template_id": template_id,
        "page": page,
        "data": data,
    }

    async with httpx.AsyncClient(timeout=10.0, transport=transport) as client:
        resp = await client.post(
            SUBSCRIBE_MESSAGE_URL,
            params={"access_token": access_token},
            json=payload,
        )
    data = resp.json()
    errcode = int(data.get("errcode") or 0)
    if errcode != 0:
        logger.warning(
            "send_subscribe_message failed errcode=%s openid=%s", errcode, openid[:10] + "..."
        )
        # 部分错误码不抛异常，仅记录 (如用户未订阅 43101)
        if errcode in (43101, 41028, 41029, 41030):
            return {"errcode": errcode, "errmsg": str(data.get("errmsg") or "")}
        raise WeChatMpError(errcode, str(data.get("errmsg") or ""))

    return {"errcode": 0, "errmsg": "ok"}


async def get_wxacode_unlimit(
    app_id: str,
    app_secret: str,
    scene: str,
    page: str | None = None,
    width: int = 430,
    auto_color: bool = False,
    line_color: dict[str, int] | None = None,
    is_hyaline: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
) -> bytes:
    """生成小程序码 (无限制版本，适合需要大量码的业务场景)。

    参数:
    - scene: 场景值 (最大32字符)，扫码进入小程序时可获取
    - page: 指定页面路径 (需在已发布版本中存在)
    - width: 二维码宽度 (单位px，最小280，最大1280)
    - auto_color: 自动配色
    - line_color: 线条颜色 {"r": 0, "g": 0, "b": 0}
    - is_hyaline: 是否透明背景

    返回: 图片二进制数据 (PNG)
    """
    access_token = await get_access_token(app_id, app_secret, transport)

    payload: dict[str, Any] = {
        "scene": scene,
        "width": width,
        "auto_color": auto_color,
        "is_hyaline": is_hyaline,
    }
    if page:
        payload["page"] = page
    if line_color:
        payload["line_color"] = line_color

    async with httpx.AsyncClient(timeout=30.0, transport=transport) as client:
        resp = await client.post(
            WXACODE_URL,
            params={"access_token": access_token},
            json=payload,
        )

    # 检查是否为 JSON 错误响应
    content_type = resp.headers.get("content-type", "")
    if "application/json" in content_type:
        data = resp.json()
        errcode = int(data.get("errcode") or 0)
        if errcode != 0:
            logger.warning("get_wxacode_unlimit failed errcode=%s", errcode)
            raise WeChatMpError(errcode, str(data.get("errmsg") or ""))

    return resp.content
