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


def test_vlm_settings_defaults() -> None:
    settings = Settings()
    assert settings.default_vlm_provider == "ollama"
    assert settings.vlm_ollama_base_url == "http://172.17.0.1:11434"
    assert settings.vlm_ollama_model == "minicpm-v47-1b-local:latest"
    assert settings.vlm_ollama_timeout_s == 120.0
