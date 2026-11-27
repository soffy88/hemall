"""app.scale — 秤接入（硬件无关）：纯函数 + 幂等 DDL + 可插拔协议解析器。

功能：
- 协议解析：大华/顶尖/托利多连续输出、通用 ASCII 重量帧
- 稳定判定、去皮、清零
- 本地小桥接程序（Python，串口→localhost WebSocket）供不支持 Web Serial 的设备用
- 内置「模拟秤」用于开发与测试
- 不 import 任何 app 内模块（与 app/weigh.py 同一纪律）

协议说明（常见计价秤 RS232/USB 连续输出模式）：
1. 大华: "ST,GS,+0000.000kg" 或 "ST,GS,+0000.000g"
2. 顶尖: "GS  0.000 kg" 或 "NT  0.000 kg"
3. 托利多: "S  0.000 kg" 或 "ST  0.000 kg"
4. 通用 ASCII: 任意包含数字+单位的行，如 "Weight: 1234 g"、"1.500kg"

稳定判定：连续 N 次读数差值 ≤ 阈值（默认 3 次、≤2g）视为稳定。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any, Protocol

# ──────────────────────────────────────────────────────────────────────────────
# 常量与类型
# ──────────────────────────────────────────────────────────────────────────────

logger = logging.getLogger("hemall.scale")

# 秤最大称重 15KG（商祺一体秤规格）：单次称重超过即拒绝，防误触/漂移脏数据。
MAX_WEIGHT_GRAMS = 15000

# 稳定判定默认参数
DEFAULT_STABLE_COUNT = 3  # 连续稳定读数次数
DEFAULT_STABLE_THRESHOLD_G = 2  # 相邻读数差值阈值（克）

# 协议正则
# 大华: ST,GS,+0000.000kg / ST,GS,+0000.000g
RE_DAHUA = re.compile(r"ST,GS,([+\-]?\d+(?:\.\d+)?)\s*(kg|g)", re.IGNORECASE)
# 顶尖: GS  0.000 kg / NT  0.000 kg
RE_DINGJIAN = re.compile(r"(?:GS|NT)\s+([+\-]?\d+(?:\.\d+)?)\s*(kg|g)", re.IGNORECASE)
# 托利多: S  0.000 kg / ST  0.000 kg
RE_TOLEDO = re.compile(r"S[T]?\s+([+\-]?\d+(?:\.\d+)?)\s*(kg|g)", re.IGNORECASE)
# 通用 ASCII: 任意包含数字+单位
RE_GENERIC = re.compile(r"([+\-]?\d+(?:\.\d+)?)\s*(kg|g|KG|G)", re.IGNORECASE)


class ScaleSource(StrEnum):
    """秤数据来源"""

    SCALE = "scale"  # 真实秤直发
    SIMULATOR = "simulator"  # 模拟器注入（演示与验证专用）
    BRIDGE = "bridge"  # 本地桥接程序转发


class ScaleStatus(StrEnum):
    """秤连接状态"""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


@dataclass(frozen=True)
class ScaleReading:
    """单次秤读数（原始）"""

    weight_grams: int  # 克，已换算
    unit: str  # 原始单位 "g" 或 "kg"
    raw: str  # 原始帧文本
    timestamp: float = field(default_factory=time.time)
    stable: bool = False  # 硬件自带稳定位（如有）
    source: ScaleSource = ScaleSource.SCALE


@dataclass(frozen=True)
class StableWeight:
    """稳定后的重量结果"""

    weight_grams: int
    readings: tuple[ScaleReading, ...]  # 用于稳定判定的窗口读数
    source: ScaleSource


# ──────────────────────────────────────────────────────────────────────────────
# 协议解析器（纯函数，可单测）
# ──────────────────────────────────────────────────────────────────────────────


def parse_dahua(line: str) -> ScaleReading | None:
    """解析大华协议帧"""
    m = RE_DAHUA.search(line)
    if not m:
        return None
    val = Decimal(m.group(1))
    unit = m.group(2).lower()
    grams = int(
        (val * (Decimal(1000) if unit == "kg" else Decimal(1))).to_integral_value(
            rounding=ROUND_HALF_UP
        )
    )
    return ScaleReading(
        weight_grams=grams, unit=unit, raw=line.strip(), stable="ST" in line.upper()
    )


def parse_dingjian(line: str) -> ScaleReading | None:
    """解析顶尖协议帧"""
    m = RE_DINGJIAN.search(line)
    if not m:
        return None
    val = Decimal(m.group(1))
    unit = m.group(2).lower()
    grams = int(
        (val * (Decimal(1000) if unit == "kg" else Decimal(1))).to_integral_value(
            rounding=ROUND_HALF_UP
        )
    )
    return ScaleReading(
        weight_grams=grams, unit=unit, raw=line.strip(), stable="ST" in line.upper()
    )


def parse_toledo(line: str) -> ScaleReading | None:
    """解析托利多协议帧"""
    m = RE_TOLEDO.search(line)
    if not m:
        return None
    val = Decimal(m.group(1))
    unit = m.group(2).lower()
    grams = int(
        (val * (Decimal(1000) if unit == "kg" else Decimal(1))).to_integral_value(
            rounding=ROUND_HALF_UP
        )
    )
    return ScaleReading(
        weight_grams=grams, unit=unit, raw=line.strip(), stable="ST" in line.upper()
    )


def parse_generic(line: str) -> ScaleReading | None:
    """解析通用 ASCII 帧（兜底）"""
    m = RE_GENERIC.search(line)
    if not m:
        return None
    val = Decimal(m.group(1))
    unit = m.group(2).lower()
    grams = int(
        (val * (Decimal(1000) if unit == "kg" else Decimal(1))).to_integral_value(
            rounding=ROUND_HALF_UP
        )
    )
    return ScaleReading(weight_grams=grams, unit=unit, raw=line.strip())


# 解析器优先级：特定协议优先，通用兜底最后
PARSERS: list[Callable[[str], ScaleReading | None]] = [
    parse_dahua,
    parse_dingjian,
    parse_toledo,
    parse_generic,
]


def parse_scale_line(line: str) -> ScaleReading | None:
    """尝试所有解析器，返回首个成功的结果"""
    for parser in PARSERS:
        reading = parser(line)
        if reading is not None:
            return reading
    return None


# ──────────────────────────────────────────────────────────────────────────────
# 稳定判定（纯函数）
# ──────────────────────────────────────────────────────────────────────────────


def is_stable(
    window: deque[ScaleReading],
    *,
    count: int = DEFAULT_STABLE_COUNT,
    threshold_g: int = DEFAULT_STABLE_THRESHOLD_G,
) -> bool:
    """判定窗口内读数是否稳定：最近 count 次读数两两差值均 ≤ threshold_g"""
    if len(window) < count:
        return False
    recent = list(window)[-count:]
    for i in range(1, len(recent)):
        if abs(recent[i].weight_grams - recent[i - 1].weight_grams) > threshold_g:
            return False
    return True


def get_stable_weight(
    window: deque[ScaleReading],
    *,
    count: int = DEFAULT_STABLE_COUNT,
    threshold_g: int = DEFAULT_STABLE_THRESHOLD_G,
) -> StableWeight | None:
    """若窗口稳定，返回稳定重量（取窗口中位数，抗抖动）"""
    if not is_stable(window, count=count, threshold_g=threshold_g):
        return None
    recent = list(window)[-count:]
    weights = sorted(r.weight_grams for r in recent)
    median = weights[len(weights) // 2]
    return StableWeight(
        weight_grams=median,
        readings=tuple(recent),
        source=ScaleSource.SCALE,
    )


# ──────────────────────────────────────────────────────────────────────────────
# 协议解析器协议（可插拔，供扩展自定义协议）
# ──────────────────────────────────────────────────────────────────────────────


class ScaleParser(Protocol):
    """协议解析器接口：输入一行文本，返回 ScaleReading 或 None"""

    name: str

    def parse(self, line: str) -> ScaleReading | None: ...


class CompositeParser:
    """组合解析器：按注册顺序尝试"""

    def __init__(self) -> None:
        self._parsers: list[ScaleParser] = []

    def register(self, parser: ScaleParser) -> None:
        self._parsers.append(parser)

    def parse(self, line: str) -> ScaleReading | None:
        for p in self._parsers:
            r = p.parse(line)
            if r is not None:
                return r
        return None


# 默认组合解析器（内置四大协议）
DEFAULT_PARSER = CompositeParser()
for fn in [parse_dahua, parse_dingjian, parse_toledo, parse_generic]:

    class _Wrapper:
        name = fn.__name__

        def parse(self, line: str, _fn=fn) -> ScaleReading | None:
            return _fn(line)

    DEFAULT_PARSER.register(_Wrapper())


# ──────────────────────────────────────────────────────────────────────────────
# 模拟秤（开发/测试用）
# ──────────────────────────────────────────────────────────────────────────────


class SimulatorScale:
    """模拟秤：产生可配置的重量序列，用于无硬件时的开发与 E2E 测试。

    用法：
        sim = SimulatorScale()
        sim.set_sequence([0, 500, 1000, 1000, 1000])  # 克
        reading = await sim.next_reading()
    """

    def __init__(
        self,
        *,
        stable_count: int = DEFAULT_STABLE_COUNT,
        stable_threshold_g: int = DEFAULT_STABLE_THRESHOLD_G,
        noise_g: int = 0,
    ) -> None:
        self._sequence: list[int] = []
        self._index = 0
        self._stable_count = stable_count
        self._stable_threshold_g = stable_threshold_g
        self._noise_g = noise_g
        self._window: deque[ScaleReading] = deque(maxlen=stable_count * 3)
        self._connected = False

    def set_sequence(self, weights_grams: list[int]) -> None:
        """设置重量序列（克）。负数表示错误/断开。"""
        self._sequence = weights_grams
        self._index = 0
        self._window.clear()

    def add_noise(self, noise_g: int) -> None:
        self._noise_g = noise_g

    async def connect(self) -> None:
        self._connected = True
        logger.info("Simulator scale connected")

    async def disconnect(self) -> None:
        self._connected = False
        logger.info("Simulator scale disconnected")

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def next_reading(self) -> ScaleReading | None:
        """产生下一帧读数（模拟秤连续输出）"""
        if not self._connected or self._index >= len(self._sequence):
            return None
        weight = self._sequence[self._index]
        self._index += 1
        if weight < 0:
            await self.disconnect()
            return None
        # 加噪声
        if self._noise_g > 0:
            import random

            weight += random.randint(-self._noise_g, self._noise_g)
            weight = max(0, weight)
        reading = ScaleReading(
            weight_grams=weight,
            unit="g",
            raw=f"SIM {weight}g",
            stable=False,
        )
        self._window.append(reading)
        return reading

    async def get_stable_weight(self) -> StableWeight | None:
        """阻塞等待直到稳定（或序列耗尽）"""
        while self._connected:
            reading = await self.next_reading()
            if reading is None:
                return None
            stable = get_stable_weight(
                self._window,
                count=self._stable_count,
                threshold_g=self._stable_threshold_g,
            )
            if stable:
                return StableWeight(
                    weight_grams=stable.weight_grams,
                    readings=stable.readings,
                    source=ScaleSource.SIMULATOR,
                )
        return None

    def tare(self) -> None:
        """去皮：在模拟器中表现为重置序列基准（实际硬件需发送指令）"""
        logger.info("Simulator tare requested")

    def zero(self) -> None:
        """清零"""
        logger.info("Simulator zero requested")


# ──────────────────────────────────────────────────────────────────────────────
# Web Serial 前端接口定义（供前端 TypeScript 复用）
# ──────────────────────────────────────────────────────────────────────────────

# 前端需实现的 Web Serial 接口（TypeScript 类型定义，此处仅作文档）
"""
interface ScalePortInfo {
  usbVendorId?: number;
  usbProductId?: number;
}

interface ScaleSerialOptions {
  baudRate: 9600 | 19200 | 38400 | 57600 | 115200;
  dataBits: 7 | 8;
  stopBits: 1 | 2;
  parity: "none" | "even" | "odd";
  bufferSize?: number;
  flowControl?: "none" | "hardware";
}

interface ScaleReadingEvent {
  weight_grams: number;
  unit: "g" | "kg";
  raw: string;
  stable: boolean;
  timestamp: number;
}

interface StableWeightEvent {
  weight_grams: number;
  readings: ScaleReadingEvent[];
  source: "scale" | "simulator" | "bridge";
}

type ScaleEventHandler = (event: ScaleReadingEvent | StableWeightEvent) => void;
"""


# ──────────────────────────────────────────────────────────────────────────────
# 本地桥接程序（串口 → WebSocket）——独立运行脚本
# ──────────────────────────────────────────────────────────────────────────────

BRIDGE_HELP = """
scale_bridge.py — 串口秤 → 本地 WebSocket 桥接程序

用途：为不支持 Web Serial 的浏览器/设备（如 Safari、iPad、旧版 Chrome）提供秤数据通道。
      前端通过 ws://localhost:8765/scale 连接，消息格式与 Web Serial 一致。

用法：
  python -m app.scale_bridge --port /dev/ttyUSB0 --baud 9600 --ws-port 8765
  python -m app.scale_bridge --list-ports           # 列出可用串口
  python -m app.scale_bridge --test --port /dev/ttyUSB0  # 测试读取 10 帧并退出

协议：自动识别大华/顶尖/托利多/通用 ASCII，输出 JSON Lines：
  {"type":"reading","weight_grams":1234,"unit":"g","raw":"ST,GS,+001.234kg","stable":false,"ts":1234567890.123}
  {"type":"stable","weight_grams":1234,"readings":[...],"source":"bridge"}

依赖：pip install pyserial websockets
"""


def bridge_main() -> None:
    """桥接程序入口（由 __main__ 或 CLI 调用）"""
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Scale Serial → WebSocket Bridge")
    parser.add_argument("--port", help="串口设备路径，如 /dev/ttyUSB0 或 COM3")
    parser.add_argument("--baud", type=int, default=9600, help="波特率 (默认 9600)")
    parser.add_argument("--ws-port", type=int, default=8765, help="WebSocket 端口 (默认 8765)")
    parser.add_argument(
        "--parser",
        default="auto",
        choices=["auto", "dahua", "dingjian", "toledo", "generic"],
        help="强制指定协议",
    )
    parser.add_argument("--list-ports", action="store_true", help="列出可用串口并退出")
    parser.add_argument("--test", action="store_true", help="测试模式：读取 10 帧后退出")
    parser.add_argument("--stable-count", type=int, default=DEFAULT_STABLE_COUNT)
    parser.add_argument("--stable-threshold", type=int, default=DEFAULT_STABLE_THRESHOLD_G)
    args = parser.parse_args()

    if args.list_ports:
        _list_serial_ports()
        return

    if not args.port:
        parser.error("--port 是必需的（除非 --list-ports）")

    # 延迟导入，避免无 pyserial 环境报错
    try:
        import serial
        import serial.tools.list_ports
    except ImportError:
        print("ERROR: 需要安装 pyserial: pip install pyserial", file=sys.stderr)
        sys.exit(1)

    try:
        import websockets
    except ImportError:
        print("ERROR: 需要安装 websockets: pip install websockets", file=sys.stderr)
        sys.exit(1)

    # 选择解析器
    if args.parser == "auto":
        parser_fn = parse_scale_line
    elif args.parser == "dahua":
        parser_fn = parse_dahua
    elif args.parser == "dingjian":
        parser_fn = parse_dingjian
    elif args.parser == "toledo":
        parser_fn = parse_toledo
    else:
        parser_fn = parse_generic

    ser = serial.Serial(
        port=args.port,
        baudrate=args.baud,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=1,
    )
    logger.info(f"Opened serial port {args.port} @ {args.baud}")

    window: deque[ScaleReading] = deque(maxlen=args.stable_count * 3)
    clients: set = set()

    async def broadcast(msg: dict) -> None:
        if not clients:
            return
        data = json.dumps(msg, ensure_ascii=False)
        await asyncio.gather(*[c.send(data) for c in clients], return_exceptions=True)

    async def handler(ws) -> None:
        clients.add(ws)
        logger.info(f"Client connected: {ws.remote_address}")
        try:
            async for _ in ws:
                pass  # 忽略客户端消息（如有指令可扩展）
        finally:
            clients.discard(ws)
            logger.info(f"Client disconnected: {ws.remote_address}")

    async def read_loop() -> None:
        frame_count = 0
        while True:
            line = ser.readline().decode("ascii", errors="ignore").strip()
            if not line:
                await asyncio.sleep(0.01)
                continue
            reading = parser_fn(line)
            if reading is None:
                logger.debug(f"Parse failed: {line}")
                continue
            window.append(reading)
            await broadcast(
                {
                    "type": "reading",
                    "weight_grams": reading.weight_grams,
                    "unit": reading.unit,
                    "raw": reading.raw,
                    "stable": reading.stable,
                    "ts": reading.timestamp,
                }
            )
            stable = get_stable_weight(
                window,
                count=args.stable_count,
                threshold_g=args.stable_threshold,
            )
            if stable:
                await broadcast(
                    {
                        "type": "stable",
                        "weight_grams": stable.weight_grams,
                        "readings": [
                            {
                                "weight_grams": r.weight_grams,
                                "unit": r.unit,
                                "raw": r.raw,
                                "stable": r.stable,
                                "ts": r.timestamp,
                            }
                            for r in stable.readings
                        ],
                        "source": "bridge",
                    }
                )
            frame_count += 1
            if args.test and frame_count >= 10:
                break

    async def main() -> None:
        server = await websockets.serve(handler, "0.0.0.0", args.ws_port)
        logger.info(f"WebSocket server started on ws://0.0.0.0:{args.ws_port}/scale")
        try:
            await read_loop()
        finally:
            server.close()
            await server.wait_closed()
            ser.close()

    asyncio.run(main())


def _list_serial_ports() -> None:
    try:
        import serial.tools.list_ports
    except ImportError:
        print("ERROR: 需要安装 pyserial: pip install pyserial")
        return
    ports = serial.tools.list_ports.comports()
    if not ports:
        print("未检测到串口设备")
        return
    for p in ports:
        print(f"  {p.device}  vid={p.vid:04x} pid={p.pid:04x}  {p.description}")


# ──────────────────────────────────────────────────────────────────────────────
# DDL（幂等，供 ensure_p0_schema 执行）
# ──────────────────────────────────────────────────────────────────────────────

SCALE_DDL = """
-- ── 秤接入：设备注册、读数流水、桥接配置 ──
CREATE TABLE IF NOT EXISTS scale_device (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    port_path TEXT NOT NULL,           -- /dev/ttyUSB0, COM3 等
    baud_rate INT NOT NULL DEFAULT 9600,
    protocol TEXT NOT NULL DEFAULT 'auto'
        CHECK (protocol IN ('auto','dahua','dingjian','toledo','generic')),
    parser_config JSONB NOT NULL DEFAULT '{}'::jsonb,  -- 协议特定参数
    stable_count INT NOT NULL DEFAULT 3,
    stable_threshold_g INT NOT NULL DEFAULT 2,
    tare_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    zero_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    bridge_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    bridge_ws_port INT,
    status VARCHAR(20) NOT NULL DEFAULT 'disconnected'
        CHECK (status IN ('disconnected','connecting','connected','error')),
    location_id UUID REFERENCES stock_location(id),
    note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (port_path, location_id)
);
CREATE INDEX IF NOT EXISTS idx_scale_device_location ON scale_device(location_id, status);

CREATE TABLE IF NOT EXISTS scale_reading (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id UUID NOT NULL REFERENCES scale_device(id) ON DELETE CASCADE,
    weight_grams INT NOT NULL CHECK (weight_grams >= 0 AND weight_grams <= 15000),
    unit VARCHAR(2) NOT NULL CHECK (unit IN ('g','kg')),
    raw TEXT NOT NULL,
    stable BOOLEAN NOT NULL DEFAULT FALSE,
    source VARCHAR(20) NOT NULL DEFAULT 'scale'
        CHECK (source IN ('scale','simulator','bridge')),
    ticket_id UUID,                    -- 关联收银单据
    pick_order_id UUID,                -- 关联拣货单
    session_id TEXT,                   -- 会话标识（一次连续称重）
    captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_scale_reading_device ON scale_reading(device_id, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_scale_reading_ticket ON scale_reading(ticket_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_scale_reading_pick ON scale_reading(pick_order_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_scale_reading_session ON scale_reading(session_id, captured_at);

-- 稳定重量事件（仅记录稳定后的结果，供审计/复核）
CREATE TABLE IF NOT EXISTS scale_stable_event (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id UUID NOT NULL REFERENCES scale_device(id) ON DELETE CASCADE,
    weight_grams INT NOT NULL CHECK (weight_grams > 0 AND weight_grams <= 15000),
    readings JSONB NOT NULL,           -- 窗口内读数数组
    source VARCHAR(20) NOT NULL DEFAULT 'scale'
        CHECK (source IN ('scale','simulator','bridge')),
    ticket_id UUID,
    pick_order_id UUID,
    session_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_scale_stable_device
    ON scale_stable_event(device_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_scale_stable_ticket ON scale_stable_event(ticket_id, created_at);
CREATE INDEX IF NOT EXISTS idx_scale_stable_pick ON scale_stable_event(pick_order_id, created_at);
"""


# ──────────────────────────────────────────────────────────────────────────────
# 后端服务接口（供路由层调用，不含 FastAPI 依赖）
# ──────────────────────────────────────────────────────────────────────────────


class ScaleService:
    """秤设备管理与读数服务（供 API 路由调用）"""

    def __init__(self, pool: Any) -> None:
        self.pool = pool
        self._devices: dict[str, DeviceSession] = {}

    async def list_devices(self) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM scale_device ORDER BY created_at DESC")
        return [dict(r) for r in rows]

    async def get_device(self, device_id: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM scale_device WHERE id = $1", device_id)
        return dict(row) if row else None

    async def create_device(self, data: dict[str, Any], actor: str) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO scale_device
                (name, port_path, baud_rate, protocol, parser_config,
                 stable_count, stable_threshold_g, tare_enabled, zero_enabled,
                 bridge_enabled, bridge_ws_port, location_id, note, created_by)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                RETURNING *
                """,
                data["name"],
                data["port_path"],
                data.get("baud_rate", 9600),
                data.get("protocol", "auto"),
                json.dumps(data.get("parser_config", {})),
                data.get("stable_count", DEFAULT_STABLE_COUNT),
                data.get("stable_threshold_g", DEFAULT_STABLE_THRESHOLD_G),
                data.get("tare_enabled", True),
                data.get("zero_enabled", True),
                data.get("bridge_enabled", False),
                data.get("bridge_ws_port"),
                data.get("location_id"),
                data.get("note", ""),
                actor,
            )
        return dict(row)

    async def update_device(self, device_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE scale_device SET
                    name = COALESCE($2, name),
                    port_path = COALESCE($3, port_path),
                    baud_rate = COALESCE($4, baud_rate),
                    protocol = COALESCE($5, protocol),
                    parser_config = COALESCE($6, parser_config),
                    stable_count = COALESCE($7, stable_count),
                    stable_threshold_g = COALESCE($8, stable_threshold_g),
                    tare_enabled = COALESCE($9, tare_enabled),
                    zero_enabled = COALESCE($10, zero_enabled),
                    bridge_enabled = COALESCE($11, bridge_enabled),
                    bridge_ws_port = COALESCE($12, bridge_ws_port),
                    location_id = COALESCE($13, location_id),
                    note = COALESCE($14, note),
                    updated_at = NOW()
                WHERE id = $1 RETURNING *
                """,
                device_id,
                data.get("name"),
                data.get("port_path"),
                data.get("baud_rate"),
                data.get("protocol"),
                json.dumps(data["parser_config"]) if data.get("parser_config") else None,
                data.get("stable_count"),
                data.get("stable_threshold_g"),
                data.get("tare_enabled"),
                data.get("zero_enabled"),
                data.get("bridge_enabled"),
                data.get("bridge_ws_port"),
                data.get("location_id"),
                data.get("note"),
            )
        return dict(row) if row else None

    async def delete_device(self, device_id: str) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute("DELETE FROM scale_device WHERE id = $1", device_id)
        return result != "DELETE 0"


class DeviceSession:
    """单设备的连接会话（内存态，不持久化）"""

    def __init__(self, device: dict[str, Any], service: ScaleService) -> None:
        self.device = device
        self.service = service
        self._window: deque[ScaleReading] = deque(
            maxlen=device.get("stable_count", DEFAULT_STABLE_COUNT) * 3
        )
        self._connected = False
        self._session_id: str | None = None

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        # 实际连接逻辑：Web Serial 由前端发起，后端仅记录状态
        # 本地桥接由 bridge_main 独立进程处理
        self._connected = True
        self._session_id = f"sess_{int(time.time() * 1000)}"
        await self._update_status("connected")

    async def disconnect(self) -> None:
        self._connected = False
        await self._update_status("disconnected")

    async def _update_status(self, status: str) -> None:
        async with self.service.pool.acquire() as conn:
            await conn.execute(
                "UPDATE scale_device SET status = $1, updated_at = NOW() WHERE id = $2",
                status,
                self.device["id"],
            )

    def feed_line(self, line: str) -> ScaleReading | None:
        """输入一行文本，返回解析后的读数（供桥接程序/前端 WebSocket 回调）"""
        parser_name = self.device.get("protocol", "auto")
        if parser_name == "auto":
            reading = parse_scale_line(line)
        elif parser_name == "dahua":
            reading = parse_dahua(line)
        elif parser_name == "dingjian":
            reading = parse_dingjian(line)
        elif parser_name == "toledo":
            reading = parse_toledo(line)
        else:
            reading = parse_generic(line)
        if reading is None:
            return None
        self._window.append(reading)
        return reading

    def get_stable(self) -> StableWeight | None:
        """检查窗口是否稳定，返回稳定重量"""
        stable = get_stable_weight(
            self._window,
            count=self.device.get("stable_count", DEFAULT_STABLE_COUNT),
            threshold_g=self.device.get("stable_threshold_g", DEFAULT_STABLE_THRESHOLD_G),
        )
        if stable:
            return StableWeight(
                weight_grams=stable.weight_grams,
                readings=stable.readings,
                source=ScaleSource.SCALE,
            )
        return None

    async def record_reading(
        self,
        reading: ScaleReading,
        *,
        ticket_id: str | None = None,
        pick_order_id: str | None = None,
    ) -> None:
        """持久化单帧读数"""
        async with self.service.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO scale_reading
                (device_id, weight_grams, unit, raw, stable, source,
                 ticket_id, pick_order_id, session_id)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                """,
                self.device["id"],
                reading.weight_grams,
                reading.unit,
                reading.raw,
                reading.stable,
                reading.source.value if isinstance(reading.source, ScaleSource) else "scale",
                ticket_id,
                pick_order_id,
                self._session_id,
            )

    async def record_stable(
        self,
        stable: StableWeight,
        *,
        ticket_id: str | None = None,
        pick_order_id: str | None = None,
    ) -> None:
        """持久化稳定重量事件"""
        async with self.service.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO scale_stable_event
                (device_id, weight_grams, readings, source,
                 ticket_id, pick_order_id, session_id)
                VALUES ($1,$2,$3,$4,$5,$6,$7)
                """,
                self.device["id"],
                stable.weight_grams,
                json.dumps(
                    [
                        {
                            "weight_grams": r.weight_grams,
                            "unit": r.unit,
                            "raw": r.raw,
                            "stable": r.stable,
                            "ts": r.timestamp,
                        }
                        for r in stable.readings
                    ],
                    ensure_ascii=False,
                ),
                stable.source.value,
                ticket_id,
                pick_order_id,
                self._session_id,
            )

    def tare(self) -> None:
        """去皮（硬件指令由前端/桥接发送，此处仅记录）"""
        logger.info(f"Device {self.device['id']} tare requested")

    def zero(self) -> None:
        """清零"""
        logger.info(f"Device {self.device['id']} zero requested")


# ──────────────────────────────────────────────────────────────────────────────
# CLI 入口
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "bridge":
        # 以桥接程序模式运行：python -m app.scale bridge --port /dev/ttyUSB0
        sys.argv.pop(1)  # 移除 "bridge"
        bridge_main()
    else:
        print("用法: python -m app.scale bridge [--port PORT] [--baud BAUD] [--ws-port PORT] ...")
        print("      python -m app.scale bridge --list-ports")
        print("      python -m app.scale bridge --test --port PORT")
