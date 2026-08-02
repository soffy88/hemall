"""app.ext.cv_provider — 内存态计算机视觉 provider。

obase 没有"计算机视觉/OCR"这个 provider 类别 (跟 weather/vlm 一样，是 hemall
域特有的外部依赖)。真实实现应该接顶棚摄像头的物体追踪/OCR 服务，或第三方
OCR API 解析购物小票；这个开发环境里没有现成的凭据/网关，风格对齐
ManualWeatherProvider/ManualVLMProvider：进程内内存态，默认返回一个可预测的
占位结果，可用 set_intake_result / set_receipt_result 覆写指定输入的解析结果，
供测试/演练模拟真实识别场景。真实视觉服务接入时只要注册一个新的 "cv" provider
实现替换掉它，oprim 及以上都不用改。
"""

from __future__ import annotations

from typing import Any


class ManualCVProvider:
    """内存态计算机视觉 provider：顶棚入库流追踪 + 小票 OCR，默认可预测占位结果。"""

    def __init__(self) -> None:
        self._intake_overrides: dict[bytes, dict[str, Any]] = {}
        self._receipt_overrides: dict[bytes, list[dict[str, Any]]] = {}

    def set_intake_result(
        self, *, video_stream: bytes, batch_id: str, shelf_slot: str, snapshot_url: str
    ) -> None:
        """覆写指定视频流的入库识别结果 (测试/演练用)。"""
        self._intake_overrides[video_stream] = {
            "batch_id": batch_id,
            "shelf_slot": shelf_slot,
            "snapshot_url": snapshot_url,
        }

    def set_receipt_result(
        self, *, image_bytes: bytes, items: list[dict[str, Any]]
    ) -> None:
        """覆写指定小票图片的 OCR 解析结果 (测试/演练用)。"""
        self._receipt_overrides[image_bytes] = items

    async def parse_intake_stream(self, *, video_stream: bytes) -> dict[str, Any]:
        """接收微仓顶棚摄像头数据，执行 OCR 和物体追踪，返回批次定位信息。"""
        override = self._intake_overrides.get(video_stream)
        if override is not None:
            return dict(override)
        return {"batch_id": None, "shelf_slot": None, "snapshot_url": None}

    async def parse_retail_receipt(self, *, image_bytes: bytes) -> list[dict[str, Any]]:
        """OCR 解析竞争对手 (传统超市) 购物小票，返回标准化的商品明细。"""
        return list(self._receipt_overrides.get(image_bytes, []))
