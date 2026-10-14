"""agent_gateway.ingest_media 集成测试 — 需真实 Postgres (TEST_PG_DSN)。

覆盖：
- 正常上传 (有库位) → 事务内创建 product + variant + inventory_batch，返回 200
- 无库位时 → 返回 400，且不产生孤儿 product/variant
- 同秒同名文件上传 → slug 唯一化，不冲突
- 经 BFF 代理的同源请求 → CSRF 通过
- 跨站请求 → CSRF 返回 403
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import UploadFile
from fastapi.testclient import TestClient

TEST_DSN = os.environ.get("TEST_PG_DSN")


@pytest.fixture
async def cn_pool():
    from obase.notification_providers import LogNotificationProvider
    from obase.payment_providers import ManualPaymentProvider
    from obase.persistence.pool import PgPool
    from obase.provider_registry import ProviderRegistry

    from app.ext.payout_provider import ManualPayoutProvider
    from app.ext.schema import ensure_ext_schema

    reg = ProviderRegistry.get()
    reg.register_generic("payment", "manual", ManualPaymentProvider(), replace=True)
    reg.register_generic("payout", "manual", ManualPayoutProvider(), replace=True)
    log_notify = LogNotificationProvider()
    reg.register_generic("notification", "log", log_notify, replace=True)

    pool = await PgPool.create(
        name="hemall_agent_gateway_test", dsn=TEST_DSN, min_size=1, max_size=5
    )
    await ensure_ext_schema(pool)
    pool._test_log_notify = log_notify  # type: ignore[attr-defined]
    yield pool
    await pool.close()


@pytest.fixture
def mock_upload_file():
    """创建一个模拟的 UploadFile 对象。"""
    file = MagicMock(spec=UploadFile)
    file.filename = "丹东草莓实拍.mp4"
    file.content_type = "video/mp4"
    file.read = AsyncMock(return_value=b"fake video content")
    return file


@pytest.fixture
def principal():
    return {"user_id": "test-user-123"}


@pytest.fixture
def out_root(tmp_path: Path) -> Path:
    return tmp_path / "output"


@pytest.mark.skipif(not TEST_DSN, reason="TEST_PG_DSN not set; skipping DB integration")
class TestIngestMedia:
    """ingest_media 核心逻辑测试。"""

    @pytest.mark.asyncio
    async def test_ingest_media_success(self, cn_pool, mock_upload_file, principal, out_root):
        """有可用库位时，完整事务创建 product + variant + batch，返回 200。"""
        # 确保有库位
        from obase.uuid7 import uuid7

        from app.ext.agent_gateway import ingest_media

        async with cn_pool.acquire() as conn:
            loc_id = await conn.fetchval(
                (
                    "INSERT INTO stock_location (id, name, region_code, host_id, address, "
                    "lat, lng, status) "
                    "VALUES ($1,'test_loc','cn-east','host_test','测试仓库',31.0,121.0,'active') "
                    "RETURNING id"
                ),
                uuid7(),
            )
        location_id = str(loc_id)

        result = await ingest_media(
            mock_upload_file,
            location_id=location_id,
            pool=cn_pool,
            out_root=out_root,
            principal=principal,
            retail_price_cents=1999,
            stock_qty=50,
        )

        assert result["status"] == "created"
        # 文件名 "丹东草莓实拍.mp4" 清洗后标题为 "丹东草莓实拍"
        assert result["title"] == "丹东草莓实拍"
        assert result["retail_price_cents"] == 1999
        assert result["stock_qty"] == 50
        assert "product_id" in result
        assert "variant_id" in result
        assert "location_id" in result

        # 验证数据库里真实写入了三张表，且在同一事务中
        async with cn_pool.acquire() as conn:
            product = await conn.fetchrow(
                'SELECT id, title, slug, status FROM "product" WHERE id = $1', result["product_id"]
            )
            variant = await conn.fetchrow(
                'SELECT id, product_id, sku_code, status FROM "product_variant" WHERE id = $1',
                result["variant_id"],
            )
            batch = await conn.fetchrow(
                (
                    'SELECT id, variant_id, location_id, stock_qty, status FROM "inventory_batch" '
                    "WHERE id = $1"
                ),
                result.get("batch_id")
                or await conn.fetchval(
                    (
                        'SELECT id FROM "inventory_batch" WHERE variant_id = $1 '
                        "ORDER BY created_at DESC LIMIT 1"
                    ),
                    result["variant_id"],
                ),
            )
        assert product is not None
        assert product["title"] == "丹东草莓实拍"
        assert product["status"] == "published"
        assert variant is not None
        assert str(variant["product_id"]) == result["product_id"]
        assert variant["status"] == "active"
        assert batch is not None
        assert str(batch["variant_id"]) == result["variant_id"]
        assert str(batch["location_id"]) == location_id
        assert batch["stock_qty"] == 50
        assert batch["status"] == "active"

    @pytest.mark.asyncio
    async def test_ingest_media_no_location_returns_400_no_orphans(
        self, cn_pool, mock_upload_file, principal, out_root
    ):
        """无可用库位时返回 400，且不留下孤儿 product/variant。"""
        from fastapi import HTTPException

        from app.ext.agent_gateway import ingest_media

        # 先清理所有相关表，确保没有 active stock_location
        async with cn_pool.acquire() as conn:
            await conn.execute('DELETE FROM "inventory_batch"')
            await conn.execute('DELETE FROM "product_variant"')
            await conn.execute('DELETE FROM "product"')
            await conn.execute('DELETE FROM "stock_location" WHERE status = $1', "active")

        with pytest.raises(HTTPException) as exc_info:
            await ingest_media(
                mock_upload_file,
                location_id="",
                pool=cn_pool,
                out_root=out_root,
                principal=principal,
            )
        assert exc_info.value.status_code == 400
        assert "仓位" in exc_info.value.detail or "stock_location" in exc_info.value.detail

        # 验证没有产生孤儿 product/variant
        async with cn_pool.acquire() as conn:
            product_count = await conn.fetchval(
                'SELECT COUNT(*) FROM "product" WHERE title = $1', "丹东草莓实拍"
            )
            variant_count = await conn.fetchval(
                'SELECT COUNT(*) FROM "product_variant" WHERE sku_code LIKE $1', "AGT-%"
            )
            batch_count = await conn.fetchval(
                'SELECT COUNT(*) FROM "inventory_batch" WHERE batch_no LIKE $1', "AGT-%"
            )
        assert product_count == 0, f"Expected 0 products, got {product_count}"
        assert variant_count == 0, f"Expected 0 variants, got {variant_count}"
        assert batch_count == 0, f"Expected 0 batches, got {batch_count}"

    @pytest.mark.asyncio
    async def test_ingest_media_same_second_same_name_unique_slug(
        self, cn_pool, mock_upload_file, principal, out_root
    ):
        """同秒上传同名文件，slug 唯一化，不冲突。"""
        from obase.uuid7 import uuid7

        from app.ext.agent_gateway import ingest_media

        async with cn_pool.acquire() as conn:
            loc_id = await conn.fetchval(
                (
                    "INSERT INTO stock_location (id, name, region_code, host_id, address, "
                    "lat, lng, status) "
                    "VALUES ($1,'test_loc2','cn-east','host_test','测试仓库2',31.0,121.0,'active') "
                    "RETURNING id"
                ),
                uuid7(),
            )
        location_id = str(loc_id)

        # 清理可能存在的测试数据
        async with cn_pool.acquire() as conn:
            await conn.execute('DELETE FROM "inventory_batch"')
            await conn.execute('DELETE FROM "product_variant"')
            await conn.execute('DELETE FROM "product"')

        # 第一次上传
        result1 = await ingest_media(
            mock_upload_file,
            location_id=location_id,
            pool=cn_pool,
            out_root=out_root,
            principal=principal,
        )
        assert result1["status"] == "created"

        # 第二次上传 (同名文件，模拟同秒)
        file2 = MagicMock(spec=UploadFile)
        file2.filename = "丹东草莓实拍.mp4"
        file2.content_type = "video/mp4"
        file2.read = AsyncMock(return_value=b"fake video content 2")

        result2 = await ingest_media(
            file2,
            location_id=location_id,
            pool=cn_pool,
            out_root=out_root,
            principal=principal,
        )
        assert result2["status"] == "created"
        assert result2["product_id"] != result1["product_id"]

        # 验证两个 product 的 slug 都不重复
        async with cn_pool.acquire() as conn:
            slugs = await conn.fetch(
                'SELECT slug FROM "product" WHERE title = $1 ORDER BY created_at', "丹东草莓实拍"
            )
        assert len(slugs) == 2
        assert slugs[0]["slug"] != slugs[1]["slug"]


class TestCSRFWithBFF:
    """CSRF + BFF 代理集成测试。

    模拟 Next.js BFF 转发请求到后端的场景：
    - 合法同源请求：BFF 转发 Origin、Sec-Fetch-Site、X-Forwarded-Host → 后端校验通过
    - 跨站请求：Origin 为恶意域名 → 后端返回 403
    - development 环境下无 Origin 头的请求被允许 (方便本地开发测试)
    """

    @pytest.fixture
    def app_with_csrf(self):
        """创建带 CSRF 中间件的测试应用 (development 环境)。"""
        from fastapi import FastAPI

        from app.config import Settings
        from app.security.csrf import CSRFMiddleware

        app = FastAPI()

        # 使用 development 环境 (CSRF 放宽但仍做 Origin 校验)
        settings = Settings(
            environment="development",
            cors_origins=["https://mall.example.com", "https://admin.example.com"],
        )
        app.add_middleware(CSRFMiddleware, settings=settings)

        @app.post("/api/agent/ingest")
        async def ingest_endpoint():
            return {"status": "ok"}

        return app

    @pytest.fixture
    def app_staging_csrf(self):
        """创建带 CSRF 中间件的测试应用 (staging 环境，强制 CSRF)。"""
        from fastapi import FastAPI

        from app.config import Settings
        from app.security.csrf import CSRFMiddleware

        app = FastAPI()

        settings = Settings(
            environment="staging",
            cors_origins=["https://staging.mall.example.com"],
        )
        app.add_middleware(CSRFMiddleware, settings=settings)

        @app.post("/api/agent/ingest")
        async def ingest_endpoint():
            return {"status": "ok"}

        return app

    def test_bff_same_origin_request_passes(self, app_with_csrf):
        """BFF 转发合法同源请求 (Origin 在 cors_origins 中) → 200。"""
        client = TestClient(app_with_csrf)
        response = client.post(
            "/api/agent/ingest",
            headers={
                "Origin": "https://mall.example.com",
                "Sec-Fetch-Site": "same-origin",
                "X-Forwarded-Host": "mall.example.com",
            },
        )
        assert response.status_code == 200, response.json()

    def test_bff_cross_origin_request_blocked(self, app_with_csrf):
        """BFF 转发跨站请求 (Origin 为恶意域名) → 403。"""
        client = TestClient(app_with_csrf)
        response = client.post(
            "/api/agent/ingest",
            headers={
                "Origin": "https://evil.example",
                "Sec-Fetch-Site": "cross-site",
                "X-Forwarded-Host": "mall.example.com",
            },
        )
        assert response.status_code == 403
        assert "CSRF" in response.json().get("detail", "")

    def test_bff_missing_origin_allowed_in_development(self, app_with_csrf):
        """development 环境下无 Origin 头的请求被允许 (方便本地开发测试)。"""
        client = TestClient(app_with_csrf)
        response = client.post("/api/agent/ingest", headers={})
        assert response.status_code == 200, response.json()

    def test_bff_missing_origin_blocked_in_staging(self, app_staging_csrf):
        """staging 环境下无 Origin 头的请求被拦截 → 403。"""
        client = TestClient(app_staging_csrf)
        response = client.post("/api/agent/ingest", headers={})
        assert response.status_code == 403
        assert "CSRF" in response.json().get("detail", "")


class TestCSRFStagingProduction:
    """staging/production 环境下的 CSRF 强制模式测试。"""

    @pytest.fixture
    def app_staging(self):
        from fastapi import FastAPI

        from app.config import Settings
        from app.security.csrf import CSRFMiddleware

        app = FastAPI()
        settings = Settings(
            environment="staging",
            cors_origins=["https://staging.mall.example.com"],
        )
        app.add_middleware(CSRFMiddleware, settings=settings)

        @app.post("/api/test")
        async def dummy_endpoint():
            return {"status": "ok"}

        return app

    def test_staging_requires_csrf_token(self, app_staging):
        """staging 环境强制要求 CSRF token (双重提交)。"""
        client = TestClient(app_staging)
        # 无 cookie、无 header → 403
        response = client.post(
            "/api/test",
            headers={"Origin": "https://staging.mall.example.com"},
        )
        assert response.status_code == 403
        assert "CSRF: missing token" in response.json().get("detail", "")

    def test_staging_with_valid_csrf_passes(self, app_staging):
        """staging 环境带有效 CSRF token + 同源 Origin → 200。"""
        client = TestClient(app_staging)
        csrf_token = "test-csrf-token-123"
        response = client.post(
            "/api/test",
            headers={
                "Origin": "https://staging.mall.example.com",
                "X-CSRF-Token": csrf_token,
            },
            cookies={"hemall_csrf_token": csrf_token},
        )
        assert response.status_code == 200, response.json()

    def test_staging_csrf_token_mismatch_blocked(self, app_staging):
        """staging 环境 CSRF token 不匹配 → 403。"""
        client = TestClient(app_staging)
        response = client.post(
            "/api/test",
            headers={
                "Origin": "https://staging.mall.example.com",
                "X-CSRF-Token": "token-from-header",
            },
            cookies={"hemall_csrf_token": "token-from-cookie"},
        )
        assert response.status_code == 403
        assert "CSRF: token mismatch" in response.json().get("detail", "")


class TestAgentGatewayAuthBoundary:
    """/agent/* 保持 CSRF 豁免的边界锁（BFF 层已兜住跨站请求）。

    豁免理由（写进报告的结论）：
      1. /agent/* 全部走 ``get_current_user`` 的 ADMIN OPS JWT 鉴权；
      2. Bearer 头由浏览器外的 agent（Hermes/Cindy/curl）显式携带，
         浏览器永远不会自动附带 Authorization → 不构成 CSRF 向量；
      3. 浏览器侧唯一可能自动附带的是 HttpOnly 会话 cookie，而它是
         ``SameSite=Lax``，跨站 POST 根本不会带上 → 拿不到 token → 401；
      4. 即便 2/3 都被绕过，BFF (web/src/lib/proxy.ts) 还会对状态变更请求做
         Origin/Sec-Fetch-Site 同源核验，跨站直接 403 且不打后端。

    这里锁住后端这一侧的边界：无token 401，带 token 的跨站请求不被 CSRF 误杀。
    """

    @pytest.fixture
    def client(self) -> TestClient:
        from app.main import app

        with TestClient(app) as c:
            yield c

    def _staff_token(self, user_id: str = "staff-agent-1") -> str:
        from app.deps import get_settings
        from app.security.tokens import TOKEN_TYPE_STAFF, sign_token

        settings = get_settings()
        return sign_token(
            {"sub": user_id, "email": f"{user_id}@x.test", "role": "admin"},
            typ=TOKEN_TYPE_STAFF,
            secret=settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
            expires_in_minutes=10,
        )

    @staticmethod
    def _upload() -> dict:
        return {"file": ("边界锁测试.mp4", b"fake-video-bytes", "video/mp4")}

    def test_ingest_without_token_returns_401(self, client):
        """没有任何 token（无 Bearer、无会话 cookie）→ 401，绝不 200。"""
        response = client.post(
            "/agent/ingest",
            files=self._upload(),
            headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
        )
        assert response.status_code == 401, response.text

    def test_ingest_with_bearer_and_cross_site_origin_not_csrf_blocked(self, client):
        """带合法 Bearer 的跨站请求不被 CSRF 误杀（Bearer 非浏览器自动凭据）。

        没有 PG 时后续落到 "database not ready" (503)；有 PG 时走正常业务返回。
        两者都不是 403 —— 证明 /agent/ 的 CSRF 豁免只对 Bearer 客户端放行，
        而浏览器跨站场景的阻断在 BFF 层完成。
        """
        response = client.post(
            "/agent/ingest",
            files=self._upload(),
            headers={
                "Authorization": f"Bearer {self._staff_token()}",
                "Origin": "https://evil.example",
                "Sec-Fetch-Site": "cross-site",
            },
        )
        assert response.status_code != 403, response.text
        assert response.status_code != 401, response.text

    def test_ingest_with_cookie_only_cross_site_rejected(self, client):
        """只有会话 cookie（模拟浏览器跨站 POST 被 SameSite=Lax 挡住）→ 401。"""
        # HttpOnly 会话 cookie 是 SameSite=Lax：跨站 POST 根本带不进来，
        # 这里显式带上也只是为了证明"仅 cookie"无法通过 typ=staff 校验之外
        # 的任何路径 —— 没有有效 JWT 就一律 401。
        response = client.post(
            "/agent/ingest",
            files=self._upload(),
            headers={"cookie": "hemall_access_token=not-a-jwt"},
        )
        assert response.status_code == 401, response.text
