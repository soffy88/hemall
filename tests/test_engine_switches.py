"""引擎开关单测: settings 关时 build_ext_oservi 装配空任务占位引擎。

不依赖真实 PG (build_* 只构造闭包, tick 才用 pool), pool 传 None 即可。
"""

from __future__ import annotations

from app.config import Settings
from app.ext.oservi_lifecycle import build_ext_oservi


async def test_spider_disabled_placeholder() -> None:
    ext = build_ext_oservi(None, Settings(engine_spider_enabled=False))

    assert ext.competitor_spider.task_list == []
    assert await ext.competitor_spider.run_once() == []
    health = ext.competitor_spider.health()
    assert health["details"]["tasks_count"] == 0
    # 占位保留真引擎名, 指标/日志口径不变
    assert ext.competitor_spider.name == "ext-competitor-spider"
    # 没关的引擎保持真实装配
    assert ext.market_maker.task_list


async def test_market_maker_disabled_placeholder() -> None:
    ext = build_ext_oservi(None, Settings(engine_market_maker_enabled=False))

    assert ext.market_maker.task_list == []
    assert await ext.market_maker.run_once() == []
    assert ext.market_maker.name == "ext-market-maker"
    assert ext.competitor_spider.task_list


def test_defaults_keep_real_engines() -> None:
    settings = Settings()
    assert settings.engine_spider_enabled is True
    assert settings.engine_market_maker_enabled is True

    ext = build_ext_oservi(None, settings)
    assert ext.competitor_spider.task_list
    assert ext.market_maker.task_list


async def test_spider_listing_disabled_placeholder() -> None:
    ext = build_ext_oservi(None, Settings(engine_spider_listing_enabled=False))

    assert ext.spider_listing.task_list == []
    assert await ext.spider_listing.run_once() == []
    assert ext.spider_listing.name == "ext-spider-listing"
    # 没关的引擎保持真实装配
    assert ext.competitor_spider.task_list


def test_spider_listing_settings_defaults() -> None:
    settings = Settings()
    assert settings.engine_spider_listing_enabled is True
    # 词表已扩成完整超市目录 (app/config.py 按分类分组): 原 8 词全部保留,
    # 无重复, 且每个分类都有代表词在表内 (全量字面量不在此钉死)
    keywords = settings.spider_listing_keywords
    for kw in ["草莓", "牛奶", "鸡蛋", "牛肉", "大米", "苹果", "西红柿", "食用油"]:
        assert kw in keywords
    for kw in ["土豆", "香蕉", "猪肉", "基围虾", "酸奶", "面粉", "水饺", "薯片", "可乐", "纸巾"]:
        assert kw in keywords
    assert len(keywords) == len(set(keywords))
    assert len(keywords) >= 90
    assert settings.spider_listing_max_price_per_tick == 100
    assert settings.spider_listing_max_per_keyword == 5
    assert settings.spider_listing_default_stock == 30
    # 截图服务默认未配置: 填价段整段跳过, 不假装能填价
    assert settings.vlm_screenshot_url == ""
    assert settings.vlm_screenshot_token == ""

    ext = build_ext_oservi(None, settings)
    assert ext.spider_listing.task_list
    assert ext.spider_listing.trigger == {"on_cron": "30 4 * * *"}


def test_vlm_settings_defaults() -> None:
    settings = Settings()
    assert settings.default_vlm_provider == "ollama"
    assert settings.vlm_ollama_base_url == "http://172.17.0.1:11434"
    assert settings.vlm_ollama_model == "minicpm-v47-1b-local:latest"
    assert settings.vlm_ollama_timeout_s == 120.0
