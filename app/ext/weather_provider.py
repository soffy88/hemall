"""app.ext.weather_provider — 内存态天气 provider。

obase 没有"天气"这个 provider 类别 (跟 payout 一样，是 hemall 扩展域特有的外部
依赖)，风格对齐 obase.payment_providers.ManualPaymentProvider / 本域自己的
ManualPayoutProvider：进程内内存态，无需真实气象 API 凭据即可本地/测试跑通。

默认全城晴天 (rain_probability=0.0, rain_intensity=0)；set_forecast 允许按
坐标覆写，供测试/演练模拟暴雨场景，不是"假装拉到了真实预报"——这就是
provider 抽象本身的用途：真实气象 API 接入时只要注册一个新的 "weather"
provider 实现替换掉它，上层 oprim.ext_weather_forecast / oservi 引擎不用改。
"""

from __future__ import annotations

from typing import Any


class ManualWeatherProvider:
    """内存态天气 provider：默认晴天，可用 set_forecast 覆写指定坐标的预报。"""

    def __init__(self) -> None:
        self._overrides: dict[tuple[float, float], dict[str, Any]] = {}

    def set_forecast(
        self, *, lat: float, lon: float, rain_probability: float, rain_intensity: int
    ) -> None:
        """覆写指定坐标未来 2 小时的预报 (测试/演练用；真实 provider 不需要这个方法)。"""
        self._overrides[(lat, lon)] = {
            "rain_probability": rain_probability,
            "rain_intensity": rain_intensity,
        }

    async def forecast(self, *, lat: float, lon: float) -> dict[str, Any]:
        """拉取指定坐标点未来 2 小时的降水概率与灾害级别。"""
        override = self._overrides.get((lat, lon))
        rain_probability = override["rain_probability"] if override else 0.0
        rain_intensity = override["rain_intensity"] if override else 0
        return {
            "lat": lat,
            "lon": lon,
            "rain_probability": rain_probability,
            "rain_intensity": rain_intensity,
        }
