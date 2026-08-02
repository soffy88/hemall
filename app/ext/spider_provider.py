"""app.ext.spider_provider — 内存态竞对价格爬虫 provider。

obase 没有"O2O 商超比价爬虫"这个 provider 类别 (跟 weather/vlm/cv/llm/
wechat_channel 一样，是 hemall 扩展域特有的外部依赖)。真实实现应该接商业爬虫
集群或自建 Headless 浏览器抓取坐标周围商超的 O2O 价格；这个开发环境里没有
真实的爬虫服务，风格对齐 ManualWeatherProvider：进程内内存态，默认返回空
结果 (没抓到任何数据，不是伪造几条假数据糊弄过去)，可用 set_results 按坐标
覆写指定区域的抓取结果，供测试/演练模拟真实比价数据。真实爬虫接入时只要
注册一个新的 "spider" provider 实现替换掉它，oprim.spider_fetch_competitor_prices
及以上都不用改。
"""

from __future__ import annotations

from typing import Any


class ManualSpiderProvider:
    """内存态竞对价格爬虫 provider：默认空结果，可用 set_results 覆写指定坐标的抓取结果。"""

    def __init__(self) -> None:
        self._overrides: dict[tuple[float, float, int], list[dict[str, Any]]] = {}

    def set_results(
        self,
        *,
        lat: float,
        lon: float,
        radius_km: int,
        results: list[dict[str, Any]],
    ) -> None:
        """覆写指定坐标/半径的抓取结果 (测试/演练用；真实 provider 不需要这个方法)。"""
        self._overrides[(lat, lon, radius_km)] = results

    async def fetch_competitor_prices(
        self, *, lat: float, lon: float, radius_km: int, keywords: list[str]
    ) -> list[dict[str, Any]]:
        """抓取坐标周围的商超 O2O 价格，返回 [{"item","price","unit","store"}, ...]。"""
        return list(self._overrides.get((lat, lon, radius_km), []))
