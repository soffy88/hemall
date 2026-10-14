"""CSRF 保护中间件 — 双重提交 token + Origin/Sec-Fetch-Site 校验。

设计 (F-03):
- 双重提交模式: csrf_token cookie (非 HttpOnly) + X-CSRF-Token 请求头
- 额外校验 Origin / Sec-Fetch-Site 头 (同源请求必须包含)
- 仅对状态改变方法 (POST/PUT/PATCH/DELETE) 生效
- 登录/登出/刷新/CSRF-token 获取等公开端点豁免
- 生产环境强制启用，开发环境可配置放行
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, JSONResponse

from app.config import Settings
from app.deps import get_settings


CSRF_COOKIE_NAME = "hemall_csrf_token"
CSRF_HEADER_NAME = "X-CSRF-Token"

# 豁免 CSRF 校验的路径 (精确匹配或前缀匹配)
CSRF_EXEMPT_PATHS = {
    "/auth/login",
    "/auth/logout",
    "/auth/refresh",
    "/auth/csrf-token",
    "/health/live",
    "/health/ready",
    "/metrics",
}

CSRF_EXEMPT_PREFIXES = (
    "/store/",      # 零登录商城端点 (用 cart capability token)
    "/recommend/",  # 公开推荐端点
    "/agent/",      # 智能体网关 (Bearer token)
    "/media/",      # 静态文件
)


class CSRFMiddleware(BaseHTTPMiddleware):
    """CSRF 保护中间件。"""

    def __init__(self, app, settings: Settings | None = None):
        super().__init__(app)
        self._settings = settings

    @property
    def settings(self) -> Settings:
        if self._settings is None:
            self._settings = get_settings()
        return self._settings

    def _is_exempt(self, path: str) -> bool:
        """判断路径是否豁免 CSRF 校验。"""
        if path in CSRF_EXEMPT_PATHS:
            return True
        for prefix in CSRF_EXEMPT_PREFIXES:
            if path.startswith(prefix):
                return True
        return False

    def _is_state_changing(self, method: str) -> bool:
        """判断是否为状态改变方法。"""
        return method.upper() in {"POST", "PUT", "PATCH", "DELETE"}

    def _get_origin(self, request: Request) -> str | None:
        """获取请求的 Origin。"""
        return request.headers.get("Origin") or request.headers.get("Referer")

    def _is_same_origin(self, request: Request, origin: str | None) -> bool:
        """校验 Origin 是否同源。"""
        if not origin:
            return False
        # 允许配置的 CORS 源
        allowed_origins = self.settings.cors_origins
        return origin.rstrip("/") in [o.rstrip("/") for o in allowed_origins]

    def _check_sec_fetch_site(self, request: Request) -> bool:
        """校验 Sec-Fetch-Site 头 (现代浏览器自动发送)。
        
        same-origin: 同源请求
        same-site: 同站点跨子域
        cross-site: 跨站点
        none: 非浏览器请求或旧浏览器
        """
        sec_fetch_site = request.headers.get("Sec-Fetch-Site")
        # 如果浏览器发送了该头，要求必须是 same-origin 或 same-site
        if sec_fetch_site:
            return sec_fetch_site in {"same-origin", "same-site"}
        # 旧浏览器不发送该头，退回到 Origin 校验
        return True

    async def dispatch(self, request: Request, call_next):
        # 仅对状态改变方法且非豁免路径进行 CSRF 校验
        if self._is_state_changing(request.method) and not self._is_exempt(request.url.path):
            # Bearer 令牌认证的请求没有 CSRF 面: 浏览器跨站表单/简单请求无法设置
            # Authorization 头 (设置即触发 CORS 预检), 令牌不会被自动附带。
            # TOTP 绑定/登录第二步 (scope 令牌) 正走这条路, 不能要求双重提交。
            if request.headers.get("Authorization"):
                return await call_next(request)
            # 开发环境可选择性放行 (配合环境变量)
            if self.settings.environment.lower() != "production":
                # 开发环境仍做 Origin 校验，但不强制 CSRF token
                origin = self._get_origin(request)
                if origin and not self._is_same_origin(request, origin):
                    return JSONResponse(
                        {"detail": "CSRF: invalid origin"},
                        status_code=403,
                    )
                if not self._check_sec_fetch_site(request):
                    return JSONResponse(
                        {"detail": "CSRF: invalid sec-fetch-site"},
                        status_code=403,
                    )
            else:
                # 生产环境：强制双重提交 token + Origin + Sec-Fetch-Site
                csrf_cookie = request.cookies.get(CSRF_COOKIE_NAME)
                csrf_header = request.headers.get(CSRF_HEADER_NAME)

                if not csrf_cookie or not csrf_header:
                    return JSONResponse(
                        {"detail": "CSRF: missing token"},
                        status_code=403,
                    )

                if csrf_cookie != csrf_header:
                    return JSONResponse(
                        {"detail": "CSRF: token mismatch"},
                        status_code=403,
                    )

                # 额外校验 Origin
                origin = self._get_origin(request)
                if not origin or not self._is_same_origin(request, origin):
                    return JSONResponse(
                        {"detail": "CSRF: invalid origin"},
                        status_code=403,
                    )

                # 额外校验 Sec-Fetch-Site
                if not self._check_sec_fetch_site(request):
                    return JSONResponse(
                        {"detail": "CSRF: invalid sec-fetch-site"},
                        status_code=403,
                    )

        return await call_next(request)