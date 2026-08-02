"""HTTP 适配层 — 把 omodul 标准返回 dict 映射为 HTTP 响应, 并提供通用端点工厂。

每个 commerce omodul 都遵守同一契约:
    async def <name>(config, input_data, output_dir, *, pool=None, on_step=None) -> dict
返回 dict 含 status ("completed"/"failed"/"cancelled") + error + 扁平 findings。
本模块把"调用 omodul + 翻译结果"的样板收敛到一处, 各领域路由只需声明式登记。

注意: 本模块刻意 **不** 启用 ``from __future__ import annotations``——端点工厂用
运行时传入的 ``input_cls`` 作为请求体注解 (``body: input_cls``), 需要注解在函数
定义期就求值为真实类对象, 供 FastAPI 生成请求 schema。
"""

import inspect
from typing import Any, Callable

from fastapi import Depends, Request
from fastapi.responses import JSONResponse

from obase.persistence.pool import PgPool

from .deps import (
    ANONYMOUS_PRINCIPAL,
    build_output_dir,
    get_current_user,
    get_pool,
    get_settings,
)
from .config import Settings

# omodul 状态 → HTTP 状态码
_STATUS_CODES = {"completed": None, "failed": 422, "cancelled": 409}  # completed 由 success_status 定


def omodul_to_response(result: dict[str, Any], *, success_status: int = 200) -> JSONResponse:
    """把 omodul 返回 dict 翻译成 JSONResponse。

    completed → success_status (200 或 201); failed → 422 (omodul 失败多为业务
    校验/状态不符, 从不 raise); cancelled → 409。body 原样回传整个 result
    (含 status/error/findings/fingerprint/decision_trail/cost_usd), 便于前端定位。
    """
    status = result.get("status")
    code = _STATUS_CODES.get(status, 500)
    if status == "completed":
        code = success_status
    return JSONResponse(status_code=code, content=_jsonable(result))


def _jsonable(obj: Any) -> Any:
    """result 里可能含 Path/UUID 等非 JSON 原生类型, 统一转字符串。"""
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


def omodul_endpoint(
    fn: Callable[..., Any],
    config_cls: type,
    input_cls: type,
    *,
    success_status: int = 200,
    require_auth: bool = True,
    post_hook: Callable[[dict, Request], Any] | None = None,
) -> Callable[..., Any]:
    """为单个 omodul 生成一个 FastAPI 路由处理函数。

    Args:
        fn: omodul 可调用 (sync/async 皆可, 内部自动判别)。
        config_cls: 该 omodul 的 Config 类 (无参实例化; 携带 _omodul_name 等元数据)。
        input_cls: 该 omodul 的 Input pydantic 模型 — FastAPI 据此自动生成请求体 schema。
        success_status: completed 时的 HTTP 码 (create_* 用 201)。
        require_auth: True 时强制 Bearer JWT; False 为公开端点 (用匿名主体)。
        post_hook: completed 后调用的旁路回调 (如派发事件); 收 (result, request),
            其异常不影响主响应。

    Returns:
        一个 async 路由处理函数, 签名含 FastAPI 依赖注入。
    """
    omodul_name = getattr(config_cls, "_omodul_name", "") or fn.__name__

    if require_auth:

        async def handler(
            body: input_cls,  # type: ignore[valid-type]
            request: Request,
            pool: PgPool = Depends(get_pool),
            principal: dict = Depends(get_current_user),
            settings: Settings = Depends(get_settings),
        ) -> JSONResponse:
            return await _invoke(
                fn, config_cls, body, omodul_name, pool, principal, settings, success_status, request, post_hook
            )

    else:

        async def handler(
            body: input_cls,  # type: ignore[valid-type]
            request: Request,
            pool: PgPool = Depends(get_pool),
            settings: Settings = Depends(get_settings),
        ) -> JSONResponse:
            return await _invoke(
                fn, config_cls, body, omodul_name, pool, ANONYMOUS_PRINCIPAL, settings, success_status, request, post_hook
            )

    handler.__name__ = f"handle_{fn.__name__}"
    handler.__qualname__ = f"handle_{fn.__name__}"
    return handler


async def _invoke(
    fn: Callable[..., Any],
    config_cls: type,
    body: Any,
    omodul_name: str,
    pool: PgPool,
    principal: dict,
    settings: Settings,
    success_status: int,
    request: Request,
    post_hook: Callable[[dict, Request], Any] | None,
) -> JSONResponse:
    """实例化 config、拼 output_dir、调 omodul、completed 后跑 post_hook、翻译结果。

    失败不 raise (omodul 自返 failed)。post_hook 是旁路 (如事件派发), 异常吞掉只记日志。
    """
    config = config_cls()
    output_dir = build_output_dir(settings, principal, omodul_name)
    output_dir.mkdir(parents=True, exist_ok=True)

    result = fn(config, body, output_dir, pool=pool)
    if inspect.iscoroutine(result):
        result = await result

    if post_hook is not None and result.get("status") == "completed":
        try:
            hook_result = post_hook(result, request)
            if inspect.iscoroutine(hook_result):
                await hook_result
        except Exception:  # noqa: BLE001 - 旁路失败不阻断主响应
            import logging

            logging.getLogger("hemall.respond").warning("post_hook failed for %s", omodul_name, exc_info=True)

    return omodul_to_response(result, success_status=success_status)
