"""spider_listing 引擎单测 — FakePool/FakeConn + mock 蜘蛛/截图/VLM。

覆盖 (不依赖真实 PG / 真实源站 / 真实模型):
- 上架段: 新品以 draft 建档 (product+variant, 幂等 slug 跳过已有,
  option_values 留痕, reference_price_cents=NULL, 分类关键词映射/兜底,
  max_per_keyword 截断, 未知 source 跳过)
- 填价段: 截图 → VLM 读价 → 事务建批次 (价/成本/库存/媒体/72h 曲线)
  + product 翻 published + price_benchmark 落 spider_vlm 行 + 库位轮转
- 填价段跳过: 未配置截图服务时整段跳过 (skipped_no_screenshot)
- 填价失败不中断: 无 detail_url / VLM 无价 / 低置信度 → failed 计数,
  不建批次
- 分类映射: 海鲜水产规则优先于肉禽蛋 (虾/蟹/海鲜/鲜鱼 已移出肉禽蛋);
  库里无海鲜水产分类时含虾标题回退为映射不到 (None)
- 填价节流: 单轮尝试数 (priced+failed) 不超过
  spider_listing_max_price_per_tick; 未定价集合按 created_at 倒序
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import asyncpg
import httpx
import pytest

from app.config import Settings
from app.ext import oservi
from tests.service_fakes import FakeConn, FakePool

_DETAIL_URL = "https://m.suning.com/product/0000000000/12451761730.html"
_IMAGE_URL = "https://imgservice3.suning.cn/uimg1/b2c/image/abc.jpg"

_CATEGORIES = [
    {"id": "cat-daily", "name": "日用百货"},
    {"id": "cat-fruit", "name": "时令水果"},
    {"id": "cat-veg", "name": "蔬菜豆品"},
    {"id": "cat-dairy", "name": "乳品烘焙"},
]


class _ShotResponse:
    def __init__(self, content: bytes = b"png-bytes") -> None:
        self.content = content

    def raise_for_status(self) -> None:
        return None


class _FakeShotClient:
    """截图服务替身: 记录请求, 返回脚本化 PNG。"""

    next_response: _ShotResponse | None = None
    last_get: dict[str, Any] | None = None
    calls: list[dict[str, Any]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _FakeShotClient:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def get(self, url: str, params: Any = None, headers: Any = None) -> _ShotResponse:
        call = {"url": url, "params": params, "headers": headers}
        type(self).last_get = call
        type(self).calls.append(call)
        assert type(self).next_response is not None
        return type(self).next_response


@pytest.fixture(autouse=True)
def _patch_shot_client(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeShotClient.next_response = _ShotResponse()
    _FakeShotClient.last_get = None
    _FakeShotClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", _FakeShotClient)


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "spider_listing_keywords": ["草莓"],
        "spider_listing_max_per_keyword": 5,
        "spider_listing_default_stock": 30,
    }
    base.update(overrides)
    return Settings(**base)


def _listing_conn(existing_slugs: set[str] | None = None) -> FakeConn:
    """上架段脚本: 分类全量 + slug 查重 (按参数判定) + 两张表插入。"""
    conn = FakeConn()
    conn.on(r'FROM "product_category"', list(_CATEGORIES))

    def _slug_check(_sql: str, args: tuple[Any, ...]) -> Any:
        return {"id": "prod-old"} if args and args[0] in (existing_slugs or set()) else None

    conn.handlers[r"WHERE slug"] = _slug_check
    conn.on(r'INSERT INTO "product" \(', "prod-new")
    conn.on(r'INSERT INTO "product_variant" \(', "var-new")
    return conn


def _suning_item(source_id: str, title: str) -> dict[str, Any]:
    return {
        "item": title,
        "image_url": _IMAGE_URL,
        "description": "产地直发",
        "detail_url": _DETAIL_URL,
        "source_id": source_id,
        "store": "苏宁自营",
        "source": "suning",
    }


async def _run(engine: Any) -> dict[str, Any]:
    results = await engine.run_once()
    result = results[0]
    assert "error" not in result, result
    return result


class TestListingPhase:
    async def test_creates_draft_products_maps_category_and_skips(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            oservi,
            "spider_fetch_products",
            AsyncMock(
                return_value=[
                    _suning_item("111", "丹东草莓 新鲜水果 2斤装"),
                    {
                        "item": "Organic Whole Milk 1L",
                        "image_url": "https://img.off.org/m.jpg",
                        "description": "Whole milk",
                        "detail_url": "https://off.org/p/999",
                        "source_id": "999",
                        "store": "OpenFoodFacts",
                        "source": "off",
                    },
                    _suning_item("222", "重复商品"),  # 已上架 → 跳过
                    {
                        "item": "未知来源商品",
                        "image_url": "",
                        "description": "",
                        "detail_url": "https://example.com/x",
                        "source_id": "333",
                        "store": "某站",
                        "source": "jddj",  # 不在来源白名单 → 跳过
                    },
                ]
            ),
        )
        conn = _listing_conn(existing_slugs={"ext-suning-222"})
        engine = oservi.build_spider_listing_engine(FakePool(conn), settings=_settings())

        result = await _run(engine)

        assert result == {
            "listed": 2,
            "priced": 0,
            "skipped_no_screenshot": True,
            "failed": 0,
            "skipped_has_batch": 0,
        }
        product_inserts = conn.issued(r'INSERT INTO "product" \(')
        assert len(product_inserts) == 2
        # 草莓 → 时令水果 (关键词映射); 英文奶名无关键词命中 → NULL, 不再兜底
        assert "cat-fruit" in product_inserts[0][1]
        assert product_inserts[1][1][3] is None
        assert "draft" in product_inserts[0][0]
        # 描述只留介绍本身, 不带来源行与外链; 店铺/详情页只记 option_values 内部留痕
        assert product_inserts[0][1][2] == "产地直发"
        assert "来源" not in str(product_inserts[0][1])
        assert _DETAIL_URL not in str(product_inserts[0][1])

        variant_inserts = conn.issued(r'INSERT INTO "product_variant" \(')
        assert len(variant_inserts) == 2
        v_args = variant_inserts[0][1]
        assert "EXT-suning-111" in v_args
        option_values = json.loads(v_args[2])
        assert option_values["detail_url"] == _DETAIL_URL
        assert option_values["image_url"] == _IMAGE_URL
        assert option_values["source_id"] == "111"
        assert option_values["来源"] == "苏宁自营"
        # reference_price_cents 以字面 NULL 入库, 不拿 0 冒充未定价
        assert ", NULL, 'active')" in variant_inserts[0][0]

    async def test_listing_strips_brand_tokens_and_source_trailer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            oservi,
            "spider_fetch_products",
            AsyncMock(
                return_value=[
                    {
                        "item": "苏鲜生 [苏宁自有品牌]可生食鲜鸡蛋1.8kg(30枚)",
                        "image_url": _IMAGE_URL,
                        "description": "可生食 无抗生素\n来源：苏宁自营 https://m.suning.com/product/0000000000/12417909119.html",
                        "detail_url": _DETAIL_URL,
                        "source_id": "999",
                        "store": "苏宁自营",
                        "source": "suning",
                    }
                ]
            ),
        )
        conn = _listing_conn()
        engine = oservi.build_spider_listing_engine(FakePool(conn), settings=_settings())

        result = await _run(engine)

        assert result["listed"] == 1
        product_inserts = conn.issued(r'INSERT INTO "product" \(')
        assert len(product_inserts) == 1
        title, _slug, description, _cat = product_inserts[0][1]
        assert title == "可生食鲜鸡蛋1.8kg(30枚)"
        assert description == "可生食 无抗生素"

    async def test_max_per_keyword_caps_new_listings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            oservi,
            "spider_fetch_products",
            AsyncMock(
                return_value=[
                    _suning_item("1", "商品一"),
                    _suning_item("2", "商品二"),
                    _suning_item("3", "商品三"),
                ]
            ),
        )
        conn = _listing_conn()
        engine = oservi.build_spider_listing_engine(
            FakePool(conn), settings=_settings(spider_listing_max_per_keyword=2)
        )

        result = await _run(engine)
        assert result["listed"] == 2
        assert len(conn.issued(r'INSERT INTO "product" \(')) == 2

    async def test_fetch_failure_counts_and_continues(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            oservi,
            "spider_fetch_products",
            AsyncMock(side_effect=RuntimeError("source down")),
        )
        conn = _listing_conn()
        engine = oservi.build_spider_listing_engine(
            FakePool(conn),
            settings=_settings(spider_listing_keywords=["草莓", "牛奶"]),
        )

        result = await _run(engine)
        assert result["listed"] == 0
        assert result["failed"] == 2  # 两关键词各计一次, 整轮不中断


def _unpriced_row(pid: str, vid: str, option_values: Any) -> dict[str, Any]:
    return {
        "product_id": pid,
        "title": "丹东草莓",
        "variant_id": vid,
        "option_values": option_values,
    }


def _pricing_conn(rows: list[dict[str, Any]]) -> FakeConn:
    conn = FakeConn()
    conn.on(r"slug LIKE", rows)
    conn.on(
        r'SELECT id FROM "stock_location"',
        [{"id": "loc-1"}, {"id": "loc-2"}],
    )
    conn.on(r'FROM "product_category"', list(_CATEGORIES))
    return conn


def _priced_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "vlm_screenshot_url": "http://shot.local/capture",
        "vlm_screenshot_token": "tok-123",
        "spider_listing_keywords": [],  # 上架段本轮无关键词, 专测填价段
    }
    base.update(overrides)
    return _settings(**base)


class TestPricingPhase:
    async def test_prices_product_batch_benchmark_publish_and_rotation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vlm_mock = AsyncMock(
            return_value={"price_yuan": 29.9, "price_text": "¥29.90", "confidence": 0.9}
        )
        monkeypatch.setattr(oservi, "vlm_extract_price", vlm_mock)
        rows = [
            _unpriced_row(
                "prod-1",
                "var-1",
                {
                    "来源": "苏宁自营",
                    "detail_url": _DETAIL_URL,
                    "source_id": "111",
                    "image_url": _IMAGE_URL,
                },
            ),
            _unpriced_row(
                "prod-2",
                "var-2",
                json.dumps(  # option_values 也可能是 JSON 串 (无 codec 时)
                    {
                        "来源": "苏宁自营",
                        "detail_url": _DETAIL_URL,
                        "source_id": "222",
                        "image_url": _IMAGE_URL,
                    }
                ),
            ),
        ]
        conn = _pricing_conn(rows)
        engine = oservi.build_spider_listing_engine(FakePool(conn), settings=_priced_settings())

        result = await _run(engine)

        assert result["priced"] == 2
        assert result["failed"] == 0
        assert result["skipped_no_screenshot"] is False
        # 截图服务契约: GET ?url= + X-Shot-Token 头 (在调用历史中查找:
        # 填价段还会尝试下载商品图, 截图不再是最后一次调用)
        shot_calls = [c for c in _FakeShotClient.calls if c["url"] == "http://shot.local/capture"]
        assert len(shot_calls) == 2
        assert shot_calls[0]["params"] == {"url": _DETAIL_URL}
        assert shot_calls[0]["headers"] == {"X-Shot-Token": "tok-123"}
        # VLM 吃到的是截图 PNG bytes
        assert vlm_mock.await_args.kwargs["image_bytes"] == b"png-bytes"

        batch_inserts = conn.issued(r'INSERT INTO "inventory_batch" \(')
        assert len(batch_inserts) == 2
        args = batch_inserts[0][1]
        assert args[1] == "var-1"
        assert args[2] == "loc-1"  # 库位按商品序轮转
        assert batch_inserts[1][1][2] == "loc-2"
        assert args[4] == 1794  # cost = round(2990 * 0.6)
        assert args[5] == 2990  # retail = 29.9 元 → 分
        assert args[6] == 30  # 默认库存
        assert json.loads(args[7]) == [_IMAGE_URL]  # media = 商品图
        assert args[8] == _IMAGE_URL  # shelf = 裸链接（2026-10-28 回归）
        curve = json.loads(args[9])
        assert len(curve["expected_cumulative"]) == 72
        assert curve["expected_cumulative"][-1] == 30

        # product 翻 published + benchmark 落 spider_vlm 行
        assert len(conn.issued(r'UPDATE "product" SET status')) == 2
        bench = conn.issued(r'INSERT INTO "price_benchmark" \(')
        assert len(bench) == 2
        assert "'spider_vlm'" in bench[0][0]
        assert bench[0][1][3] == "苏宁自营"
        assert bench[0][1][4] == 2990

    async def test_pricing_localizes_product_image(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        vlm_mock = AsyncMock(
            return_value={"price_yuan": 29.9, "price_text": "¥29.90", "confidence": 0.9}
        )
        monkeypatch.setattr(oservi, "vlm_extract_price", vlm_mock)

        jpeg = b"\xff\xd8\xff" + b"\x00" * 5000

        class _BranchingClient:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            async def __aenter__(self) -> _BranchingClient:
                return self

            async def __aexit__(self, *exc: Any) -> bool:
                return False

            async def get(self, url: str, params: Any = None, headers: Any = None) -> Any:
                if url == _IMAGE_URL:
                    return _ShotResponse(jpeg)
                return _ShotResponse(b"png-bytes")

        monkeypatch.setattr(httpx, "AsyncClient", _BranchingClient)
        rows = [
            _unpriced_row(
                "prod-1",
                "var-1",
                {
                    "来源": "苏宁自营",
                    "detail_url": _DETAIL_URL,
                    "source_id": "111",
                    "image_url": _IMAGE_URL,
                },
            )
        ]
        conn = _pricing_conn(rows)
        engine = oservi.build_spider_listing_engine(
            FakePool(conn), settings=_priced_settings(output_root=str(tmp_path))
        )

        result = await _run(engine)

        assert result["priced"] == 1
        batch_inserts = conn.issued(r'INSERT INTO "inventory_batch" \(')
        assert len(batch_inserts) == 1
        args = batch_inserts[0][1]
        shelf = args[8]
        assert shelf.startswith("/media/spider/")
        assert shelf.endswith(".jpg")
        assert json.loads(args[7]) == [shelf]
        assert (tmp_path / "ext_bespoke" / "spider_media" / shelf.rsplit("/", 1)[1]).exists()

    async def test_missing_detail_url_fails_single_item(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            oservi,
            "vlm_extract_price",
            AsyncMock(return_value={"price_yuan": 9.9, "confidence": 0.9}),
        )
        conn = _pricing_conn([_unpriced_row("prod-x", "var-x", {"来源": "苏宁自营"})])
        engine = oservi.build_spider_listing_engine(FakePool(conn), settings=_priced_settings())

        result = await _run(engine)
        assert result["priced"] == 0
        assert result["failed"] == 1
        assert not conn.did_issue(r'INSERT INTO "inventory_batch"')

    async def test_vlm_no_price_and_low_confidence_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows = [_unpriced_row("prod-1", "var-1", {"detail_url": _DETAIL_URL})]
        for verdict in (
            {"price_yuan": None, "price_text": "", "confidence": 0.2},
            {"price_yuan": 10.0, "price_text": "¥10", "confidence": 0.3},
            {"price_yuan": 100000.0, "price_text": "", "confidence": 0.9},
        ):
            monkeypatch.setattr(oservi, "vlm_extract_price", AsyncMock(return_value=verdict))
            conn = _pricing_conn(rows)
            engine = oservi.build_spider_listing_engine(FakePool(conn), settings=_priced_settings())
            result = await _run(engine)
            assert result["priced"] == 0, verdict
            assert result["failed"] == 1, verdict
            assert not conn.did_issue(r'INSERT INTO "inventory_batch"')

    async def test_no_screenshot_url_skips_pricing_phase(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(oservi, "spider_fetch_products", AsyncMock(return_value=[]))
        conn = _pricing_conn([_unpriced_row("prod-1", "var-1", {"detail_url": _DETAIL_URL})])
        engine = oservi.build_spider_listing_engine(
            FakePool(conn),
            settings=_settings(),  # vlm_screenshot_url 默认空串
        )

        result = await _run(engine)
        assert result["skipped_no_screenshot"] is True
        assert result["priced"] == 0
        assert not conn.did_issue(r"slug LIKE")  # 未定价集合根本没查

    async def test_pricing_phase_caps_attempts_per_tick(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vlm_mock = AsyncMock(
            return_value={"price_yuan": 9.9, "price_text": "¥9.90", "confidence": 0.9}
        )
        monkeypatch.setattr(oservi, "vlm_extract_price", vlm_mock)
        rows = [
            _unpriced_row(f"prod-{i}", f"var-{i}", {"detail_url": _DETAIL_URL}) for i in range(5)
        ]
        conn = _pricing_conn(rows)
        engine = oservi.build_spider_listing_engine(
            FakePool(conn),
            settings=_priced_settings(spider_listing_max_price_per_tick=2),
        )

        result = await _run(engine)

        # 5 个待填价, 单轮上限 2: 只尝试前 2 个, 其余留待下一轮
        assert result["priced"] == 2
        assert result["failed"] == 0
        assert vlm_mock.await_count == 2
        assert len(conn.issued(r'INSERT INTO "inventory_batch" \(')) == 2
        # 新品优先: 未定价集合按 created_at 倒序取
        unpriced_queries = conn.issued(r"slug LIKE")
        assert "ORDER BY p.created_at DESC" in unpriced_queries[0][0]

    async def test_pricing_cap_counts_failures_as_attempts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vlm_mock = AsyncMock(
            return_value={"price_yuan": 9.9, "price_text": "¥9.90", "confidence": 0.9}
        )
        monkeypatch.setattr(oservi, "vlm_extract_price", vlm_mock)
        rows = [
            _unpriced_row("prod-bad", "var-bad", {"来源": "苏宁自营"}),  # 无 detail_url → 失败
            _unpriced_row("prod-1", "var-1", {"detail_url": _DETAIL_URL}),
            _unpriced_row("prod-2", "var-2", {"detail_url": _DETAIL_URL}),
        ]
        conn = _pricing_conn(rows)
        engine = oservi.build_spider_listing_engine(
            FakePool(conn),
            settings=_priced_settings(spider_listing_max_price_per_tick=2),
        )

        result = await _run(engine)

        # 失败也算一次尝试: 1 失败 + 1 成功即达上限, 第 3 个不碰
        assert result["failed"] == 1
        assert result["priced"] == 1
        assert vlm_mock.await_count == 1
        assert len(conn.issued(r'INSERT INTO "inventory_batch" \(')) == 1

    def test_engine_schedule(self) -> None:
        engine = oservi.build_spider_listing_engine(FakePool(FakeConn()), settings=_settings())
        assert engine.name == "ext-spider-listing"
        assert engine.trigger == {"on_cron": "30 4 * * *"}


class TestCategoryMapping:
    """_map_spider_listing_category: 海鲜水产规则优先于肉禽蛋。"""

    _WITH_SEAFOOD = [
        {"id": "cat-meat", "name": "肉禽蛋"},
        {"id": "cat-seafood", "name": "海鲜水产"},
    ]

    def test_shrimp_title_maps_to_seafood(self) -> None:
        assert (
            oservi._map_spider_listing_category("鲜活基围虾 500g/盒", self._WITH_SEAFOOD)
            == "cat-seafood"
        )

    def test_shrimp_no_longer_maps_to_meat_when_seafood_exists(self) -> None:
        # 虾/蟹/海鲜/鲜鱼 已从肉禽蛋规则删除: 含虾标题只会落海鲜水产
        assert (
            oservi._map_spider_listing_category("冰鲜大虾仁 300g", self._WITH_SEAFOOD)
            == "cat-seafood"
        )

    def test_meat_title_still_maps_to_meat(self) -> None:
        assert (
            oservi._map_spider_listing_category("新鲜猪肉馅 500g", self._WITH_SEAFOOD) == "cat-meat"
        )

    def test_missing_seafood_category_falls_back_to_none(self) -> None:
        # 库里没有海鲜水产分类时整条规则跳过: 含虾标题映射不到 → None
        categories = [
            {"id": "cat-meat", "name": "肉禽蛋"},
            {"id": "cat-veg", "name": "蔬菜豆品"},
        ]
        assert oservi._map_spider_listing_category("鲜活基围虾 500g/盒", categories) is None
        assert oservi._map_spider_listing_category("新鲜猪肉馅 500g", categories) == "cat-meat"


class TestPricingRaceGuards:
    """填价段跨 worker 竞态防护 (生产多 worker 同 tick 并发填价)。

    - 锁内复查命中已有 active 批次 → 跳过 (skipped_has_batch),
      填价本体不触发 (截图/VLM/建批次都不发生), 锁仍成对释放
    - advisory lock 按行成对取/放: key1 为管线固定常数, key2 传
      变体 id 文本 (SQL 内 hashtext), 批次插入夹在 lock/unlock 之间
    - 部分唯一索引兜底触发 UniqueViolationError → 按竞态跳过计,
      不计 failed, 锁仍成对释放
    """

    async def test_recheck_hit_skips_without_pricing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        vlm_mock = AsyncMock(return_value={"price_yuan": 9.9, "confidence": 0.9})
        monkeypatch.setattr(oservi, "vlm_extract_price", vlm_mock)
        conn = _pricing_conn([_unpriced_row("prod-1", "var-1", {"detail_url": _DETAIL_URL})])
        # 锁内复查命中: 该变体已有 active 批次 (快照之后被另一 worker 建好)
        conn.on(r'FROM "inventory_batch" WHERE variant_id', 1)
        engine = oservi.build_spider_listing_engine(FakePool(conn), settings=_priced_settings())

        result = await _run(engine)

        assert result["priced"] == 0
        assert result["failed"] == 0
        assert result["skipped_has_batch"] == 1
        assert vlm_mock.await_count == 0  # 复查在截图/VLM 之前, 跳过即全不发生
        assert not conn.did_issue(r'INSERT INTO "inventory_batch"')
        # 跳过路径上锁也要成对释放
        assert len(conn.issued(r"pg_advisory_lock")) == 1
        assert len(conn.issued(r"pg_advisory_unlock")) == 1

    async def test_advisory_lock_pairs_around_each_priced_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            oservi,
            "vlm_extract_price",
            AsyncMock(return_value={"price_yuan": 9.9, "confidence": 0.9}),
        )
        rows = [
            _unpriced_row("prod-1", "var-1", {"detail_url": _DETAIL_URL}),
            _unpriced_row("prod-2", "var-2", {"detail_url": _DETAIL_URL}),
        ]
        conn = _pricing_conn(rows)
        engine = oservi.build_spider_listing_engine(FakePool(conn), settings=_priced_settings())

        result = await _run(engine)

        assert result["priced"] == 2
        assert result["skipped_has_batch"] == 0
        locks = conn.issued(r"pg_advisory_lock")
        unlocks = conn.issued(r"pg_advisory_unlock")
        assert [args for _sql, args in locks] == [
            (oservi._SPIDER_PRICING_LOCK_KEY1, "var-1"),
            (oservi._SPIDER_PRICING_LOCK_KEY1, "var-2"),
        ]
        assert [args for _sql, args in unlocks] == [
            (oservi._SPIDER_PRICING_LOCK_KEY1, "var-1"),
            (oservi._SPIDER_PRICING_LOCK_KEY1, "var-2"),
        ]
        # 全序: 每行 lock → 建批次 → unlock, 行间不交叉
        kinds = []
        for sql, _args in conn.statements:
            if "pg_advisory_unlock" in sql:
                kinds.append("unlock")
            elif "pg_advisory_lock" in sql:
                kinds.append("lock")
            elif 'INSERT INTO "inventory_batch"' in sql:
                kinds.append("insert")
        assert kinds == ["lock", "insert", "unlock", "lock", "insert", "unlock"]

    async def test_unique_violation_counts_as_race_skip_not_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            oservi,
            "vlm_extract_price",
            AsyncMock(return_value={"price_yuan": 9.9, "confidence": 0.9}),
        )
        conn = _pricing_conn([_unpriced_row("prod-1", "var-1", {"detail_url": _DETAIL_URL})])

        def _raise_unique(_sql: str, _args: tuple[Any, ...]) -> Any:
            raise asyncpg.UniqueViolationError(
                "duplicate key value violates unique constraint "
                '"uq_inventory_batch_spider_ext_variant"'
            )

        conn.handlers[r'INSERT INTO "inventory_batch" \('] = _raise_unique
        engine = oservi.build_spider_listing_engine(FakePool(conn), settings=_priced_settings())

        result = await _run(engine)

        assert result["priced"] == 0
        assert result["failed"] == 0
        assert result["skipped_has_batch"] == 1
        assert len(conn.issued(r"pg_advisory_lock")) == 1
        assert len(conn.issued(r"pg_advisory_unlock")) == 1
