"""app.ext.oskill — hemall 扩展层元技能：纯内存算法。

绝对无状态，不查库，不做任何 IO——全部 sync def (对齐全局契约"纯数学/内存
计算必须 sync def")。承载所有"动态定价"与"路由决策"的复杂逻辑，omodul 组装
调用，oskill 之间互调深度 ≤ 2。

不依赖 oprim (即使 oprim.math_haversine_distance 是同一个 haversine 公式)——
oskill 层刻意保持自包含，不跨层引入 IO 相关的包，verify_media_provenance
自己内联了一份 haversine 计算，不是遗漏。
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta
from typing import Any

import numpy as np
from scipy.spatial import Voronoi

# ── 3.1 路由与物理决策 ──────────────────────────────────────────────────────


def resolve_display_batch(
    available_batches: list[dict[str, Any]], *, strategy: str
) -> dict[str, Any]:
    """从同一 variant 的多个可售批次里选唯一一个展示，抛弃搜索/筛选框。

    Args:
        available_batches: 同一 variant 下的候选批次列表，每个 dict 至少含
            "intake_time" (datetime，fifo 策略用) 和 "retail_price" (int，
            price_desc 策略用)。
        strategy: "fifo" (最早入库优先，用于优先清老库存) 或 "price_desc"
            (零售价从高到低，新鲜/未衰减批次优先)。

    Returns:
        选中的那一个批次 dict (原样返回，不做拷贝之外的修改)。

    Raises:
        ValueError: available_batches 为空，或 strategy 不是已知策略。
    """
    if not available_batches:
        raise ValueError("resolve_display_batch: available_batches must not be empty")
    if strategy == "fifo":
        return min(available_batches, key=lambda b: b["intake_time"])
    if strategy == "price_desc":
        return max(available_batches, key=lambda b: b["retail_price"])
    raise ValueError(
        f"resolve_display_batch: unknown strategy {strategy!r}; must be 'fifo' or 'price_desc'"
    )


#: wave (慢送) 档固定运费——单独提出为模块级常量，因为它同时是
#: oservi.delivery_wave_engine 识别"这是一笔 wave 订单"的依据 (orders 表没有
#: 单独的 shipping_type 列，wave 档运费恒为此值，跟 express 的按距离/重量
#: 浮动价、pickup 恒为 0 三者互斥，可以拿运费金额反查配送方式)，两处共用同一个
#: 常量，不允许出现两份互相脱节的硬编码 200。
WAVE_SHIPPING_PRICE_CENTS = 200

# ── 履约 SLA (P1 冲刺: 时效承诺 + 超时赔付) ────────────────────────────

#: 履约超时赔付额 (算力金, 分)。架构师定调 3-5 元区间，取中值 4 元。
SLA_COMPENSATION_CENTS = 400
#: 邻里直达 (express) 承诺送达时长 (分钟)。
EXPRESS_SLA_MINUTES = 120
#: 自提 (pickup) 承诺备货时长 (分钟)。
PICKUP_SLA_MINUTES = 60
#: 班车波次承诺送达时长 (分钟)：发车后 + 邻里送达缓冲。
WAVE_SLA_MINUTES = 120
#: 班车波次定点 (每日两班，与 oservi.delivery_wave_engine 对齐)。
WAVE_SCHEDULE_HOURS = (10, 16)


def compute_delivery_sla(
    now: Any,
    *,
    shipping_cents: int,
    paid_at: Any | None = None,
    next_wave_at: Any | None = None,
    express_lead_minutes: int = EXPRESS_SLA_MINUTES,
    pickup_lead_minutes: int = PICKUP_SLA_MINUTES,
    wave_lead_minutes: int = WAVE_SLA_MINUTES,
) -> dict:
    """按配送方式计算时效承诺 (纯函数，可单测)。

    规则 (跟邻里配送引擎的现状对齐，不做路径优化承诺)：
        - wave (运费 = WAVE_SHIPPING_PRICE_CENTS): 承诺 = 下一班车发车时间 +
          wave_lead_minutes。下一班车由 WAVE_SCHEDULE_HOURS 定点推演；
          next_wave_at 可由调用方注入 (引擎内用 SQL 算下一班)。
        - express (运费 > wave 且非 0): 承诺 = 支付时刻 + express_lead_minutes。
        - pickup (运费 = 0): 承诺 = 支付时刻 + pickup_lead_minutes。

    Returns:
        {"shipping_type": "wave"|"express"|"pickup", "promised_at": datetime,
         "lead_minutes": int, "compensation_cents": int}
    """
    from datetime import timedelta

    if paid_at is None:
        paid_at = now

    if shipping_cents == WAVE_SHIPPING_PRICE_CENTS:
        shipping_type = "wave"
        base = next_wave_at or _next_wave_at(now)
        promised = base + timedelta(minutes=wave_lead_minutes)
        lead = wave_lead_minutes
    elif shipping_cents == 0:
        shipping_type = "pickup"
        promised = paid_at + timedelta(minutes=pickup_lead_minutes)
        lead = pickup_lead_minutes
    else:
        shipping_type = "express"
        promised = paid_at + timedelta(minutes=express_lead_minutes)
        lead = express_lead_minutes

    return {
        "shipping_type": shipping_type,
        "promised_at": promised,
        "lead_minutes": lead,
        "compensation_cents": SLA_COMPENSATION_CENTS,
    }


def _next_wave_at(now: Any) -> Any:
    """下一班车时间：当天 10:00/16:00 之后最近的一个波次点。"""
    from datetime import timedelta

    for hour in WAVE_SCHEDULE_HOURS:
        candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if candidate > now:
            return candidate
    return (now + timedelta(days=1)).replace(
        hour=WAVE_SCHEDULE_HOURS[0], minute=0, second=0, microsecond=0
    )


def compute_transparent_shipping_options(
    distance_km: float, *, weight: int
) -> list[dict[str, Any]]:
    """硬核透明三档运费：express (按距离+重量计价) / wave (慢送固定价) / pickup (免费)。

    Args:
        distance_km: 配送距离 (公里)，非负。
        weight: 包裹重量 (克)，非负。

    Returns:
        [{"type": "express", "price": int}, {"type": "wave", "price": 200},
         {"type": "pickup", "price": 0}]  —— price 单位分。

    Raises:
        ValueError: distance_km 或 weight 为负。
    """
    if distance_km < 0:
        raise ValueError(
            "compute_transparent_shipping_options: distance_km must be non-negative"
        )
    if weight < 0:
        raise ValueError(
            "compute_transparent_shipping_options: weight must be non-negative"
        )

    base_fee = 500  # 起步价 5 元
    distance_fee = round(distance_km * 100)  # 每公里 1 元
    weight_fee = round(weight / 1000 * 50)  # 每公斤 0.5 元
    express_price = base_fee + distance_fee + weight_fee

    return [
        {"type": "express", "price": express_price},
        {"type": "wave", "price": WAVE_SHIPPING_PRICE_CENTS},
        {"type": "pickup", "price": 0},
    ]


def recompute_voronoi_grid(
    node_coordinates: list[tuple[str, float, float]],
) -> dict[str, dict[str, Any]]:
    """输入全城微仓坐标，重新划分每个仓的最近邻辐射区域，实现新店上线秒级分流。

    用 scipy.spatial.Voronoi 算真实的 Voronoi 图，取 ridge_points (哪两个
    site 的 Voronoi 胞体共享一条边 = 互为最近邻) 推导每个仓的"接壤邻居"
    列表——这是"最近邻辐射多边形"在业务上真正要用的信息 (新店上线后，
    只要知道它从谁那儿抢流量、该往谁那儿分流)；具体多边形几何顶点坐标
    下游用不上，且无边界的 Voronoi 胞体需要额外裁剪到一个包围盒才有限，
    这里不做那层几何复杂度。

    Args:
        node_coordinates: [(location_id, lat, lon), ...]，至少 2 个点
            (少于 2 个点 Voronoi 图没有意义)，location_id 不能重复。

    Returns:
        {location_id: {"lat": float, "lon": float, "neighbors": [location_id, ...]}}

    Raises:
        ValueError: 少于 2 个坐标点，或 location_id 重复。
    """
    if len(node_coordinates) < 2:
        raise ValueError(
            "recompute_voronoi_grid: need at least 2 locations to compute a Voronoi grid"
        )

    ids = [n[0] for n in node_coordinates]
    if len(set(ids)) != len(ids):
        raise ValueError(
            "recompute_voronoi_grid: duplicate location_id in node_coordinates"
        )

    points = np.array([(lon, lat) for _, lat, lon in node_coordinates])
    vor = Voronoi(points)

    neighbor_idx: dict[int, set[int]] = {i: set() for i in range(len(node_coordinates))}
    for p1, p2 in vor.ridge_points:
        neighbor_idx[p1].add(p2)
        neighbor_idx[p2].add(p1)

    grid: dict[str, dict[str, Any]] = {}
    for i, (loc_id, lat, lon) in enumerate(node_coordinates):
        grid[loc_id] = {
            "lat": lat,
            "lon": lon,
            "neighbors": sorted(ids[j] for j in neighbor_idx[i]),
        }
    return grid


#: verify_media_provenance 允许的最大 GPS 漂移 (公里)，超过视为跟供应商登记地址对不上。
_MAX_PROVENANCE_DRIFT_KM = 1.0


def verify_media_provenance(
    video_meta: dict[str, Any], *, supplier_loc: tuple[float, float]
) -> bool:
    """对比视频 EXIF 的 GPS 坐标与供应商登记坐标，防旧视频/异地视频造假。

    Args:
        video_meta: 视频元数据，需含 "gps_lat"/"gps_lon" (从 EXIF/设备元数据
            提取，不在本函数内解析视频文件本身——那是采集层 oprim 的职责)。
        supplier_loc: (lat, lon) 供应商登记坐标。

    Returns:
        两点距离 <= 1 公里视为通过。video_meta 缺 GPS 字段直接判 False
        (无法验证 = 不通过，不是宽松放行)。
    """
    lat = video_meta.get("gps_lat")
    lon = video_meta.get("gps_lon")
    if lat is None or lon is None:
        return False

    sup_lat, sup_lon = supplier_loc
    earth_radius_km = 6371.0088
    phi1, phi2 = math.radians(lat), math.radians(sup_lat)
    dphi = math.radians(sup_lat - lat)
    dlambda = math.radians(sup_lon - lon)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    distance_km = earth_radius_km * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    return distance_km <= _MAX_PROVENANCE_DRIFT_KM


# ── 3.2 动态金融与劳动力算法 ─────────────────────────────────────────────────

_DECAY_INITIAL_MARKUP = 1.5  # 入库时刻零售价 = 1.5x 成本价
_DECAY_FLOOR_MARKUP = 1.0  # 清仓底价下限 = 成本价 (不做亏本甩卖)


def calculate_decay_price(
    intake_time: datetime, *, shelf_life_hours: int, base_cost: int
) -> int:
    """按时间衰减曲线计算当前应报的零售价 (清仓底价)。

    从入库时刻的 1.5x 成本价，随保质期临近线性衰减到 1.0x 成本价；超过
    保质期的部分价格钉死在成本价 (不继续往下砍——过期之后是
    mark_batch_for_disposal 的职责，不是继续降价甩卖)。

    Args:
        intake_time: 入库时刻 (datetime；建议 tz-aware，naive 时按本机时区处理)。
        shelf_life_hours: 保质期 (小时)，必须为正。
        base_cost: 采购成本价 (分)，必须为正。

    Returns:
        当前时刻应报的零售价 (分，整数)。

    Raises:
        ValueError: shelf_life_hours 或 base_cost 非正。
    """
    if shelf_life_hours <= 0:
        raise ValueError("calculate_decay_price: shelf_life_hours must be positive")
    if base_cost <= 0:
        raise ValueError("calculate_decay_price: base_cost must be positive")

    now = datetime.now(intake_time.tzinfo)
    elapsed_hours = (now - intake_time).total_seconds() / 3600
    progress = min(max(elapsed_hours / shelf_life_hours, 0.0), 1.0)

    markup = (
        _DECAY_INITIAL_MARKUP - (_DECAY_INITIAL_MARKUP - _DECAY_FLOOR_MARKUP) * progress
    )
    return round(base_cost * markup)


def adjust_demand_curve_by_weather(
    base_price: int, *, rain_probability: float, rain_intensity: int
) -> int:
    """暴雨阻断自提场景下的时空套利：概率高 + 强度大，自动暴降零售价促销。

    只有达到"阻断自提"的严重程度 (概率 >= 0.6 且强度 >= 2) 才会调价；没到
    那个阈值原价返回，不是概率一高就乱降价。

    Args:
        base_price: 未受天气影响的基准零售价 (分)，必须为正。
        rain_probability: 未来降水概率 [0.0, 1.0]。
        rain_intensity: 降水强度等级 (0=无, 1=小雨, 2=中雨, 3=大雨/暴雨)，非负。

    Returns:
        调整后的零售价 (分，整数)，最多打 5 折 (不低于 base_price 的一半)。

    Raises:
        ValueError: base_price 非正，rain_probability 不在 [0,1]，
            rain_intensity 为负。
    """
    if base_price <= 0:
        raise ValueError("adjust_demand_curve_by_weather: base_price must be positive")
    if not 0.0 <= rain_probability <= 1.0:
        raise ValueError(
            "adjust_demand_curve_by_weather: rain_probability must be within [0, 1]"
        )
    if rain_intensity < 0:
        raise ValueError(
            "adjust_demand_curve_by_weather: rain_intensity must be non-negative"
        )

    if rain_probability < 0.6 or rain_intensity < 2:
        return base_price

    discount = min(rain_probability * (rain_intensity / 3) * 0.5, 0.5)
    return round(base_price * (1 - discount))


def calculate_piece_rate_wage(queue_depth: int, *, base_wage: int) -> int:
    """激增计件工资：积压订单越多，单次拣货佣金越高，吸引更多大妈上线干活。

    每积压 10 单，佣金上浮 10%，封顶 3 倍基础工资。

    Args:
        queue_depth: 当前待拣货订单积压深度，非负。
        base_wage: 基础计件单价 (分)，非积压状态下的标准单价，必须为正。

    Returns:
        本次拣货应付佣金 (分，整数)。

    Raises:
        ValueError: queue_depth 为负，base_wage 非正。
    """
    if queue_depth < 0:
        raise ValueError("calculate_piece_rate_wage: queue_depth must be non-negative")
    if base_wage <= 0:
        raise ValueError("calculate_piece_rate_wage: base_wage must be positive")

    surge_multiplier = min(1.0 + (queue_depth // 10) * 0.1, 3.0)
    return round(base_wage * surge_multiplier)


def compute_user_savings_yield(market_price: int, *, node_retail_price: int) -> int:
    """对比传统商超价格，算出用户在 hemall 下单能省多少钱，用于推销硬核会员制。

    Args:
        market_price: 传统商超参考价 (分)，非负。
        node_retail_price: hemall 当前零售价 (分)，非负。

    Returns:
        净省金额 (分)：``max(0, market_price - node_retail_price)``——hemall
        价格反而更贵时不返回负数误导用户，直接判 0。

    Raises:
        ValueError: 任一价格为负。
    """
    if market_price < 0 or node_retail_price < 0:
        raise ValueError("compute_user_savings_yield: prices must be non-negative")
    return max(0, market_price - node_retail_price)


#: predict_household_burn_rate 的启发式基线用参照人口基数——样本购买间隔
#: 默认按这个家庭规模标定，实际 family_size 越大，见底越快 (间隔线性缩短)。
_REFERENCE_FAMILY_SIZE = 3


def predict_household_burn_rate(
    purchase_history: list[dict[str, Any]], *, family_size: int
) -> datetime:
    """基于历史购买记录，预测家庭现有存货大概什么时候见底。

    简单消耗速率启发式：用历史购买间隔的平均值估计"多久买一次"，按家庭
    规模相对参照基数 (3 人) 线性缩放 (人多消耗快，见底更早)。不是 ARIMA/
    Prophet 那一套完整时序模型——先用一个可解释、好运维的基线把
    execute_ambient_replenishment 的依赖打通，以后有真实购买数据了再换
    更精细的模型。

    Args:
        purchase_history: 购买记录列表，每条至少含 "purchased_at"
            (datetime) 和 "quantity" (int)；至少需要 2 条记录才能算出
            购买间隔 (不排序输入顺序，函数内部会按时间排序)。
        family_size: 家庭人口数，必须为正。

    Returns:
        预测见底时间：最近一次购买时间 + 缩放后的平均购买间隔。

    Raises:
        ValueError: purchase_history 少于 2 条记录，family_size 非正。
    """
    if len(purchase_history) < 2:
        raise ValueError(
            "predict_household_burn_rate: need at least 2 purchase records to estimate an interval"
        )
    if family_size <= 0:
        raise ValueError("predict_household_burn_rate: family_size must be positive")

    sorted_history = sorted(purchase_history, key=lambda p: p["purchased_at"])
    intervals_seconds = [
        (
            sorted_history[i]["purchased_at"] - sorted_history[i - 1]["purchased_at"]
        ).total_seconds()
        for i in range(1, len(sorted_history))
    ]
    avg_interval_seconds = sum(intervals_seconds) / len(intervals_seconds)
    scaled_interval_seconds = avg_interval_seconds * (
        _REFERENCE_FAMILY_SIZE / family_size
    )

    last_purchase = sorted_history[-1]["purchased_at"]
    return last_purchase + timedelta(seconds=scaled_interval_seconds)


# ── 3.2 (v2.0) 贝叶斯信誉与仲裁裁决 ───────────────────────────────────────

#: 综合风险分超过此阈值 -> honeypot (强制物理退货桶验证)，否则 -> instant (直接退款)。
_HONEYPOT_RISK_THRESHOLD = 0.3


def evaluate_claim_credibility(
    user_trust_score: int, *, batch_anomaly_rate: float, route_risk: float
) -> str:
    """贝叶斯信誉判定：综合用户信誉、批次历史异常率、路由风险，决定客诉走向。

    风险分 = 用户不信任度(权重 0.5) + 批次异常率(权重 0.3) + 路由风险(权重 0.2)，
    超过阈值判 "honeypot" (强制走物理退货桶验证防薅羊毛)，否则判 "instant"
    (高信誉直接退款)。三个权重体现"用户自身信誉是最主要依据，批次历史次之，
    路由风险影响最小"这个业务判断——目前没有真实仲裁结果数据可以反向校准，
    先用这组可解释、好运维的固定权重，跟 predict_household_burn_rate 用启发式
    基线暂代精细模型是同一个工程取舍。

    Args:
        user_trust_score: 用户信誉分，取值 [0, 100]。
        batch_anomaly_rate: 该批次历史客诉/异常比例，取值 [0.0, 1.0]。
        route_risk: 配送路由风险评分 (如经手人数/中转次数换算)，取值 [0.0, 1.0]。

    Returns:
        "instant" (直接退款) 或 "honeypot" (强制物理验证)。

    Raises:
        ValueError: 任一参数超出取值范围。
    """
    if not 0 <= user_trust_score <= 100:
        raise ValueError(
            "evaluate_claim_credibility: user_trust_score must be within [0, 100]"
        )
    if not 0.0 <= batch_anomaly_rate <= 1.0:
        raise ValueError(
            "evaluate_claim_credibility: batch_anomaly_rate must be within [0, 1]"
        )
    if not 0.0 <= route_risk <= 1.0:
        raise ValueError("evaluate_claim_credibility: route_risk must be within [0, 1]")

    distrust = 1.0 - user_trust_score / 100
    risk_score = distrust * 0.5 + batch_anomaly_rate * 0.3 + route_risk * 0.2
    return "honeypot" if risk_score > _HONEYPOT_RISK_THRESHOLD else "instant"


def compute_supplier_trust_score(
    history: list[dict[str, Any]], *, new_violation: dict[str, Any] | None = None
) -> int:
    """根据无言退货桶的真实反馈，动态计算供应商信誉分。

    每条历史记录若 outcome="upheld" (客诉成立，坐实供应商问题) 扣 10 分；
    outcome="rejected" (客诉不成立) 小幅加 1 分 (证明这批货没问题)。
    new_violation (若有) 按其 severity [0,1] 再扣最多 30 分。分数封在 [0,100]。

    "低于 60 分返回阻断信号" (SPEC 原文) 这里按字面 -> int 签名实现为：
    仍然返回算出来的分数本身，"阻断"是调用方 (如 execute_slashing_workflow)
    看到 score < 60 时自己做的判断，不是这个纯函数越权改变返回类型去传信号。

    Args:
        history: 历史客诉记录列表，每条至少含 "outcome" ("upheld"/"rejected")。
        new_violation: 本次新增违规事件 (可选)，至少含 "severity" [0.0, 1.0]。

    Returns:
        供应商信誉分 [0, 100]。

    Raises:
        ValueError: new_violation 提供了但 severity 超出 [0, 1]。
    """
    score = 100
    for record in history:
        outcome = record.get("outcome")
        if outcome == "upheld":
            score -= 10
        elif outcome == "rejected":
            score += 1

    if new_violation is not None:
        severity = new_violation.get("severity", 0.0)
        if not 0.0 <= severity <= 1.0:
            raise ValueError(
                "compute_supplier_trust_score: new_violation.severity must be within [0, 1]"
            )
        score -= round(severity * 30)

    return max(0, min(100, score))


#: market_maker 单次改价的封顶/封底幅度——避免偏离度算出一个极端值时单次跳价过猛。
_MARKET_MAKER_MAX_MARKUP = 0.2
_MARKET_MAKER_MAX_MARKDOWN = -0.3


def calculate_vwap_deviation(
    current_sales: int, *, target_curve: dict[str, Any], elapsed_hours: int
) -> float:
    """计算当前销量偏离理想消化曲线的标准差倍数。返回负数代表滞销。

    Args:
        current_sales: 当前累计销量 (件)，非负。
        target_curve: 理想消化曲线，``{"expected_cumulative": [按小时索引的
            理想累计销量列表], "stddev": 预期波动标准差 (件)}``。
        elapsed_hours: 批次入库至今经过的小时数，非负；超出
            expected_cumulative 长度时钉在最后一个点 (曲线只定义到批次
            "理论售罄"那一刻，之后没有意义再往后插值)。

    Returns:
        (current_sales - 理想累计销量) / stddev，正数代表畅销、负数代表滞销。

    Raises:
        ValueError: current_sales/elapsed_hours 为负，target_curve 缺字段或
            stddev 非正。
    """
    if current_sales < 0:
        raise ValueError("calculate_vwap_deviation: current_sales must be non-negative")
    if elapsed_hours < 0:
        raise ValueError("calculate_vwap_deviation: elapsed_hours must be non-negative")
    expected_cumulative = target_curve.get("expected_cumulative")
    stddev = target_curve.get("stddev")
    if not expected_cumulative:
        raise ValueError(
            "calculate_vwap_deviation: target_curve.expected_cumulative is required"
        )
    if not stddev or stddev <= 0:
        raise ValueError(
            "calculate_vwap_deviation: target_curve.stddev must be positive"
        )

    idx = min(elapsed_hours, len(expected_cumulative) - 1)
    expected_at_hour = expected_cumulative[idx]
    return (current_sales - expected_at_hour) / stddev


def compute_market_maker_price(
    base_price: int, *, vwap_deviation: float, theta_decay: float
) -> int:
    """结合销量偏离度与时间衰减 (Theta)，算出当前的"最佳做市零售价"。

    滞销 (vwap_deviation 为负) 时降价刺激消化，畅销时可以小幅上浮；同时叠加
    theta_decay 这个跟偏离度无关的纯时间衰减 (越接近保质期终点，价格额外
    再打折)。单次调整幅度封顶 +20%/封底 -30% (_MARKET_MAKER_MAX_MARKUP/
    _MARKET_MAKER_MAX_MARKDOWN)，避免某次异常输入算出离谱的跳价。

    Args:
        base_price: 未做市调整前的基准零售价 (分)，必须为正。
        vwap_deviation: calculate_vwap_deviation 算出的偏离度 (标准差倍数)。
        theta_decay: 时间衰减系数 [0.0, 1.0]，0 表示无衰减。

    Returns:
        做市后的零售价 (分，整数)，不低于 0。

    Raises:
        ValueError: base_price 非正，theta_decay 不在 [0, 1]。
    """
    if base_price <= 0:
        raise ValueError("compute_market_maker_price: base_price must be positive")
    if not 0.0 <= theta_decay <= 1.0:
        raise ValueError(
            "compute_market_maker_price: theta_decay must be within [0, 1]"
        )

    deviation_adjustment = max(
        min(vwap_deviation * 0.05, _MARKET_MAKER_MAX_MARKUP), _MARKET_MAKER_MAX_MARKDOWN
    )
    price = base_price * (1 + deviation_adjustment) * (1 - theta_decay)
    return max(round(price), 0)


#: calculate_membership_dividend 每邀请 1 个活跃邻居对应的减免/分红比例。
_REFERRAL_DIVIDEND_RATE = 0.1


def calculate_membership_dividend(active_referrals: int, *, base_fee: int) -> int:
    """每邀请 1 个活跃邻居，算出对应的算力费减免/分红总额度。

    返回的是总额度 (可能超过 base_fee 本身)——调用方负责拆分："min(额度,
    base_fee)" 部分是减免 (冲抵当期会员费)，超出 base_fee 的部分 ("max(0,
    额度 - base_fee)") 才是 SPEC 说的"超过阈值转为正向现金分红"。这个函数
    只管算总额度，不管钱怎么分渠道发放 (减免 vs 打款是 omodul 层的职责)。

    Args:
        active_referrals: 成功邀请的活跃邻居数，非负。
        base_fee: 会员基础算力费 (分)，必须为正。

    Returns:
        总减免/分红额度 (分)，非负整数。

    Raises:
        ValueError: active_referrals 为负，base_fee 非正。
    """
    if active_referrals < 0:
        raise ValueError(
            "calculate_membership_dividend: active_referrals must be non-negative"
        )
    if base_fee <= 0:
        raise ValueError("calculate_membership_dividend: base_fee must be positive")
    return round(base_fee * _REFERRAL_DIVIDEND_RATE) * active_referrals


#: match_neighbor_route 的最大匹配半径 (公里)，SPEC 原文"100 米"。
_NEIGHBOR_MATCH_RADIUS_KM = 0.1
#: 匹配成功时的佣金报价上限/下限 (分)。
_NEIGHBOR_COMMISSION_MAX_CENTS = 500
_NEIGHBOR_COMMISSION_MIN_CENTS = 100


def match_neighbor_route(
    buyer_loc: tuple[float, float], pending_orders: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """在 100 米内匹配一个愿意代提货的邻居订单，生成"顺手履约"佣金报价。

    不是真正的蚁群算法 (ACO 是概率性/迭代式启发式，用于求解一批订单的联合
    路径最优化问题)——这里只做单点最近邻筛选 (100 米硬阈值内取距离最近的一
    个)，是"给定一个待处理买家，在候选池里挑一个顺路邻居"这个子问题的最小
    可用实现，不是完整的多订单路径规划；真要上 ACO 需要邻居数量级足够大、
    且能接受非确定性的近似解，跟这里"单次同步调用要给出确定结果"的场景不
    匹配，先用确定性最近邻。

    Args:
        buyer_loc: 买家坐标 (lat, lon)。
        pending_orders: 候选订单列表，每条至少含 "order_id" / "loc" (lat,lon)
            / "willing_to_deliver" (bool，是否愿意代提货)。

    Returns:
        None (100 米内没有愿意代提货的邻居) 或 {"neighbor_order_id",
        "distance_km", "commission_cents"} (越近佣金越高，封顶 5 元/封底 1 元)。
    """
    candidates: list[tuple[float, dict[str, Any]]] = []
    for order in pending_orders:
        if not order.get("willing_to_deliver"):
            continue
        dist = _haversine_km(buyer_loc, order["loc"])
        if dist <= _NEIGHBOR_MATCH_RADIUS_KM:
            candidates.append((dist, order))

    if not candidates:
        return None

    candidates.sort(key=lambda pair: pair[0])
    dist, best = candidates[0]
    ratio = 1 - dist / _NEIGHBOR_MATCH_RADIUS_KM
    commission = round(
        _NEIGHBOR_COMMISSION_MIN_CENTS
        + ratio * (_NEIGHBOR_COMMISSION_MAX_CENTS - _NEIGHBOR_COMMISSION_MIN_CENTS)
    )
    return {
        "neighbor_order_id": best["order_id"],
        "distance_km": dist,
        "commission_cents": commission,
    }


def _haversine_km(p1: tuple[float, float], p2: tuple[float, float]) -> float:
    """match_neighbor_route 专用的内联 haversine——oskill 层不引入 oprim 依赖。"""
    earth_radius_km = 6371.0088
    lat1, lon1 = p1
    lat2, lon2 = p2
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return earth_radius_km * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ── 3.3 (v4.0) 社交播报文案算子与队列控制 ─────────────────────────────────


def build_fomo_system_prompt() -> str:
    """固定不变的冷血营销系统指令，喂给 oprim.ext_llm_generate_text 当 system_prompt。"""
    return (
        "你是一个极其冷酷但极具煽动性的生鲜播报员。你的任务是基于输入的数据，"
        "生成一段限制在 80 字以内的短视频文案。必须包含：\n"
        "1. 产地与GPS真实性强调。\n"
        "2. 绝对的价格对比暴击。\n"
        "3. 极其强烈的稀缺感（库存即将枯竭）。\n"
        "绝对不要使用任何虚假承诺，不要使用任何废话，语言风格要像战报一样硬核。"
    )


def construct_fomo_user_prompt(batch_info: dict[str, Any], *, market_price: int) -> str:
    """纯内存计算：把物理批次数据拼成 LLM 生成文案所需的用户上下文。

    Args:
        batch_info: 至少含 "id" / "variant_desc" / "supplier_polygon_name" /
            "retail_price" (分) / "stock_qty" / "broadcast_type"
            ("fresh_arrival"/"clearance")。
        market_price: 传统商超参考价 (分)，必须为正——用作折扣率对比锚点。

    Returns:
        拼好的用户提示词文本。

    Raises:
        ValueError: market_price 或 retail_price 非正 (除零/负数折扣没有意义)。
    """
    if market_price <= 0:
        raise ValueError("construct_fomo_user_prompt: market_price must be positive")
    retail_price = batch_info["retail_price"]
    if retail_price <= 0:
        raise ValueError(
            "construct_fomo_user_prompt: batch_info['retail_price'] must be positive"
        )

    retail_price_yuan = retail_price / 100.0
    market_price_yuan = market_price / 100.0
    discount_pct = int((1 - (retail_price_yuan / market_price_yuan)) * 100)

    return (
        f"批次号: {batch_info['id']}\n"
        f"商品: {batch_info['variant_desc']}\n"
        f"源头直拍: {batch_info['supplier_polygon_name']}\n"
        f"hemall 底价: {retail_price_yuan}元 (传统超市均价 {market_price_yuan}元，暴降 {discount_pct}%)\n"
        f"全城仅剩物理库存: {batch_info['stock_qty']} 箱\n"
        f"当前状态: {batch_info['broadcast_type']} (上新或清仓)"
    )


def calculate_broadcast_priority(stock_qty: int, *, theta_decay_hours: float) -> int:
    """播报排队优先级算子：库存越大、距销毁时间越短，优先级(权重)越高，插队最先发。

    theta_decay_hours 故意收 float 而不是 SPEC 原文标注的 int——用整数小时
    取整会把"还剩 59 分钟过期"和"已经过期"混为一谈 (两者取整都是 0 小时)，
    实际上前者应该是优先级最高的那一档 (最紧急)，不是跟"过期不用播了"同一
    档，调用方 (oservi.social_broadcast_engine) 传的是精确到秒的小时数，不
    在这里为了凑 SPEC 的字面类型标注而人为损失精度制造 bug。

    Args:
        stock_qty: 批次剩余库存 (件/箱)，非负。
        theta_decay_hours: 距离批次销毁 (过期) 还有多少小时 (可以有小数)。
            <=0 (已过期) 直接判 0 优先级，不是负数或异常。

    Returns:
        优先级权重，越大越优先。

    Raises:
        ValueError: stock_qty 为负。
    """
    if stock_qty < 0:
        raise ValueError("calculate_broadcast_priority: stock_qty must be non-negative")
    if theta_decay_hours <= 0:
        return 0
    return int((stock_qty * 100) / theta_decay_hours)


# ── 3.4 (v5.0) 价格归一化与试探做市算子 ────────────────────────────────────

#: 从原始单位字符串里提取数量+单位——SPEC 原文写的是 (g|kg|ml|L)，但入参先
#: .lower() 过再拿这个 pattern 去 re.search，大写 "L" 永远不可能匹配已经被
#: 转小写的字符串，等于升 (L/公升) 这个单位从来没被正确识别过；这里改成
#: 统一小写 "l"，真的能匹配上。支持小数数量 (1.5kg)——生鲜 O2O 里"1.5斤"
#: 类标注常见，只取整数会静默算错每克价。
_UNIT_PATTERN = re.compile(r"(\d+(?:\.\d+)?)(g|kg|ml|l)")


def normalize_sku_price(raw_price: int, *, raw_unit: str) -> float | None:
    """把外部非标单位强制归一化为基准单位 (分/克或分/毫升)。

    单位无法解析时返回 None，而不是 SPEC 原文那种"按件计"的 float(raw_price)
    兜底——price_benchmarks.normalized_price_per_unit 这一列的语义就是"每克
    价格"，塞一个"整件价格"进同一列，后续 AVG(normalized_price_per_unit)
    (submit_supplier_reverse_auction_workflow 用它算基准线) 会把单价和整件价
    混在一起平均，算出一个没有任何意义的数字，而且还是静默污染，比"漏了一条
    基线数据"严重得多。返回 None 让调用方存 NULL——Postgres 的 AVG() 原生
    跳过 NULL，不会污染基准线；raw_price/raw_unit 本身仍然可以原样入库留痕，
    只是不参与"每克价格"这个维度的平均。

    Args:
        raw_price: 原始抓取/OCR 价格 (分)，必须非负。
        raw_unit: 原始单位字符串 (如 "500g"/"1kg"/"2L")，大小写不敏感。

    Returns:
        每克 (或每毫升) 价格 (分)，单位无法解析或数量非正时返回 None。

    Raises:
        ValueError: raw_price 为负。
    """
    if raw_price < 0:
        raise ValueError("normalize_sku_price: raw_price must be non-negative")

    match = _UNIT_PATTERN.search(raw_unit.lower())
    if not match:
        return None

    qty = float(match.group(1))
    unit_type = match.group(2)
    if unit_type in ("kg", "l"):
        qty *= 1000
    if qty <= 0:
        return None

    return raw_price / qty


def validate_supplier_margin(
    supplier_bid_price: float, *, benchmark_price: float
) -> bool:
    """核算供应商申报底价是否能击穿毛利红线 (至少 50% 的物理套利空间)。

    两个参数必须是同一个计价口径 (如都是"分/克")——这个函数本身不做单位
    换算，也换算不了 (它不知道批次的总重量)，调用方 (omodul) 负责保证传进
    来的两个数字可以直接相减比较。

    Args:
        supplier_bid_price: 供应商申报价 (跟 benchmark_price 同一计价口径)，
            非负。
        benchmark_price: 市场基准价 (同一计价口径)，必须为正 (拿来当分母)。

    Returns:
        True 表示毛利空间 >= 50%，允许放行；False 表示报价太高，拒绝。

    Raises:
        ValueError: supplier_bid_price 为负，或 benchmark_price 非正。
    """
    if supplier_bid_price < 0:
        raise ValueError(
            "validate_supplier_margin: supplier_bid_price must be non-negative"
        )
    if benchmark_price <= 0:
        raise ValueError("validate_supplier_margin: benchmark_price must be positive")

    gross_margin_spread = (benchmark_price - supplier_bid_price) / benchmark_price
    return gross_margin_spread >= 0.50


#: 试探单每次下调的比例——SPEC 明确写死 5%。
_PROBE_MARKDOWN_RATE = 0.95


def calculate_next_probe_price(
    current_price: int, *, observed_velocity: float, target_velocity: float
) -> int:
    """阶梯试探算法：观测流速达标就维持原价，达不到就下调 5% 继续探底。

    Args:
        current_price: 当前试探挂牌价 (分)，必须为正。
        observed_velocity: 观测到的真实销售流速 (单/分钟)，非负。
        target_velocity: 目标流速 (单/分钟)，必须为正。

    Returns:
        下一次试探价 (分，整数)：流速达标返回原价不变，否则打 95 折。

    Raises:
        ValueError: current_price 非正，observed_velocity 为负，或
            target_velocity 非正。
    """
    if current_price <= 0:
        raise ValueError("calculate_next_probe_price: current_price must be positive")
    if observed_velocity < 0:
        raise ValueError(
            "calculate_next_probe_price: observed_velocity must be non-negative"
        )
    if target_velocity <= 0:
        raise ValueError("calculate_next_probe_price: target_velocity must be positive")

    if observed_velocity >= target_velocity:
        return current_price
    return int(current_price * _PROBE_MARKDOWN_RATE)


# ── 3.5 (v6.0) 冷血佣金与领主税算子 ────────────────────────────────────────

_MERCENARY_MAX_BOUNTY_RATE = 0.80
_MERCENARY_SAFE_HORIZON_HOURS = 24.0
_MERCENARY_PANIC_HORIZON_HOURS = 4.0
_LORD_TAX_RATE = 0.002
_LORD_CLAIM_CONFLICT_RADIUS_KM = 1.0


def compute_mercenary_bounty_rate(hours_to_expiry: float) -> float:
    """临期雇佣兵算子：距离销毁越近，抖音达人悬赏佣金比例越高。

    - 距离销毁 >= 24 小时：0% (不开放给达人，靠自身网络消化)。
    - 距离销毁 <= 4 小时：80% (终极悬赏，挽回 20% 现金流总比全损好)。
    - 4~24 小时之间：从 80% 线性递减到 0%。

    SPEC 原文的函数签名多带了一个 ``current_price: int`` 参数，但函数体内
    从头到尾没用过它——纯粹的死参数，这里删掉，不为了跟 SPEC 字面签名一致
    保留一个没有任何作用的入参。

    Args:
        hours_to_expiry: 距离批次销毁的剩余小时数 (可以是负数，代表已过期
            但尚未清理的库存，视同 "极其危险" 按 80% 处理)。

    Returns:
        佣金比例 (0.0 ~ 0.80 之间)。
    """
    if hours_to_expiry >= _MERCENARY_SAFE_HORIZON_HOURS:
        return 0.0
    if hours_to_expiry <= _MERCENARY_PANIC_HORIZON_HOURS:
        return _MERCENARY_MAX_BOUNTY_RATE

    span = _MERCENARY_SAFE_HORIZON_HOURS - _MERCENARY_PANIC_HORIZON_HOURS
    urgency_rate = (
        _MERCENARY_MAX_BOUNTY_RATE
        - ((hours_to_expiry - _MERCENARY_PANIC_HORIZON_HOURS) / span)
        * _MERCENARY_MAX_BOUNTY_RATE
    )
    return round(urgency_rate, 4)


def calculate_lord_tax(order_amount: int, *, is_active_node: bool) -> int:
    """领主税算子：节点是某个抖音达人 (数字领主) 点火建起来的，该节点此后
    产生的每一笔交易 (无论买家是谁) 都要给领主抽万分之二的永久流水税。

    is_active_node 由调用方传入 (调用方已经查过该节点是否存在生效中的
    digital_lord 契约)——这个纯函数本身不碰数据库，只负责税率计算。

    Args:
        order_amount: 该节点这笔订单 (或该节点名下这部分子订单) 的金额 (分)。
        is_active_node: 该节点是否存在生效中的 digital_lord 契约。

    Returns:
        应缴领主税 (分，整数)；is_active_node=False 时恒为 0。
    """
    if not is_active_node:
        return 0
    return int(order_amount * _LORD_TAX_RATE)


def check_spatial_conflict(
    claim: tuple[float, float], existing_locations: list[tuple[float, float]]
) -> bool:
    """云加盟认领新节点时的 1 公里冲突半径检测。

    SPEC 原文用 PostGIS 的 ``ST_DWithin(geom, ...)`` 做这个查询，但这个项目的
    stock_locations 表从 v1.0 建表起就只有 lat/lon 两列 (DECIMAL)，没有
    PostGIS 的 geometry 列，也没有启用 PostGIS 扩展——跟 v2.0 供应商溯源用
    GeoJSON 多边形、v5.0 空间围栏点火全部走纯 Python haversine 是同一个既有
    约定 (见 match_neighbor_route)，这里复用同一个 _haversine_km 实现，不
    额外引入 PostGIS 依赖。

    Args:
        claim: 待认领坐标 (lat, lon)。
        existing_locations: 已有节点坐标列表 [(lat, lon), ...] (任意状态，
            包括还没装硬件的 pending_hardware 节点——它们也已经物理占了地)。

    Returns:
        True 表示方圆 1 公里内已有节点，认领应被拒绝。
    """
    return any(
        _haversine_km(claim, loc) <= _LORD_CLAIM_CONFLICT_RADIUS_KM
        for loc in existing_locations
    )


def find_nearest_location(
    locations: list[tuple[str, float, float]], *, lat: float, lon: float
) -> tuple[str, float]:
    """纯内存：返回距离查询点最近的 location_id 及其距离 (公里)。

    补天计划 Task 2.1 (get_nearby_feed) 的定位基座——"人找货"到"地理位置找货"
    的第一步就是找到离查询者最近的 active 微仓。直接 haversine 取最近，不做
    Voronoi 胞体判定：单点查询场景下"距离最近"就是正确的归属语义，Voronoi
    (recompute_voronoi_grid) 服务的是"节点间接壤邻居"关系，两者用途不同。

    Args:
        locations: [(location_id, lat, lon), ...]，至少 1 个点。
        lat/lon: 查询者坐标 (十进制)。

    Returns:
        (location_id, distance_km)。

    Raises:
        ValueError: locations 为空。
    """
    if not locations:
        raise ValueError("find_nearest_location: locations must not be empty")

    nearest_id = locations[0][0]
    nearest_dist = _haversine_km(
        (lat, lon), (float(locations[0][1]), float(locations[0][2]))
    )
    for loc_id, loc_lat, loc_lon in locations[1:]:
        dist = _haversine_km((lat, lon), (float(loc_lat), float(loc_lon)))
        if dist < nearest_dist:
            nearest_id = loc_id
            nearest_dist = dist
    return nearest_id, nearest_dist


def is_shelf_life_safe(expiration_time: Any, *, now: Any, margin_hours: float) -> bool:
    """纯内存：判断批次是否处于"安全货架期"内 (距过期还有至少 margin_hours)。

    补天计划 Task 2.1 的过期安全阀——get_nearby_feed 只把 "expiration_time
    安全" 的批次暴露给前端；即使 inventory_decay_engine 还没到下一个 tick，
    过期/临期批次也不可能流到顾客面前 (防御纵深，不依赖后台引擎的及时性)。

    Args:
        expiration_time: 批次过期时间 (tz-aware datetime)。
        now: 当前时间 (tz-aware datetime)。
        margin_hours: 安全余量 (小时)。

    Returns:
        True 表示还在安全期；expiration_time 为 None (没有过期时间的批次
        视作长期商品，永远安全) 返回 True。
    """
    if expiration_time is None:
        return True
    return expiration_time > now + timedelta(hours=margin_hours)


# ── Phase 7 Task 3: 空间-行为矩阵 (商品关联度算子) ──────────────────────
# nearby-feed 的升维：从"距离最近 + 有货 + 未过期"升级为"行为序列加权"。
# 极简协同过滤——用全局订单的共现关系算商品关联度，用户买过牛肉就把他
# 历史上跟牛肉一起买过的番茄/洋葱插队到 Feed 最前方。三个纯函数都只吃
# 数据结构、不碰 DB，SQL 层 (routers) 负责取数据，这里只做权重运算，
# 可单测。


def build_cooccurrence_matrix(
    order_product_pairs: list[list[str]],
) -> dict[tuple[str, str], int]:
    """从订单×商品矩阵构建共现矩阵 (无向边，去自环)。

    全局关联度的数据源：每个订单下单的商品 id 列表。共现次数越多，两个
    商品越"经常一起被买"。矩阵键为 (a, b) 且 a < b (规范化无向边)，查询
    时按 (min, max) 取键即可，不存两份。

    Args:
        order_product_pairs: 每个元素是一个订单里下单的商品 id 列表。

    Returns:
        {(a, b): 共现次数}，a < b。
    """
    matrix: dict[tuple[str, str], int] = {}
    for products in order_product_pairs:
        uniq = sorted({str(p) for p in products})
        for i in range(len(uniq)):
            for j in range(i + 1, len(uniq)):
                key = (uniq[i], uniq[j])
                matrix[key] = matrix.get(key, 0) + 1
    return matrix


def compute_batch_affinity_scores(
    user_product_ids: list[str],
    candidate_product_ids: list[str],
    cooccurrence: dict[tuple[str, str], int],
) -> dict[str, float]:
    """计算候选商品相对用户历史购买序列的关联度得分。

    对每个候选商品，累加它与用户买过的每个商品的共现次数，除以用户商品数
    (归一化到 0~1 区间，弱关联不放大)。得分 0 = 无历史关联 (不插队，保持
    距离排序)；> 0 才参与插队。

    Args:
        user_product_ids: 用户 (设备) 最近购买过的商品 id 列表。
        candidate_product_ids: 候选批次对应的商品 id 列表 (与候选一一对应，
            按同一下标对齐)。
        cooccurrence: build_cooccurrence_matrix 的产物。

    Returns:
        {商品 id: 关联度得分}——只含候选集里有分 (得分 > 0) 的商品。
    """
    if not user_product_ids:
        return {}
    user_set = set(user_product_ids)
    scores: dict[str, float] = {}
    for cand in candidate_product_ids:
        if not cand or cand in user_set:
            # 自己买过的东西不叫"关联"，不参与插队
            continue
        total = 0
        for u in user_set:
            key = tuple(sorted((u, cand)))
            total += cooccurrence.get(key, 0)
        if total > 0:
            scores[cand] = total / len(user_set)
    return scores


def rerank_feed_by_affinity(
    batches: list[dict[str, Any]],
    affinity_scores: dict[str, float],
    *,
    boost_threshold: float = 0.0,
) -> list[dict[str, Any]]:
    """把关联商品插队到 Feed 最前方 (稳定排序，不动原始顺序)。

    实现：给每项加 ``affinity`` (得分或 0.0) 与 ``boosted`` 标记，按
    (boosted desc, 原始下标 asc) 稳定排序——有关联的先整体上浮，组内保持
    原有"按过期时间/距离"的相对顺序，避免破坏货架期优先语义。

    Args:
        batches: nearby-feed 的候选批次 dict 列表 (每项含 product_id)。
        affinity_scores: compute_batch_affinity_scores 的结果 (按 product_id
            索引)。
        boost_threshold: 得分大于该值才插队 (默认 > 0 即插队)。

    Returns:
        重排后的列表，每项新增 ``affinity`` / ``boosted`` 两个字段。
    """
    scored: list[dict[str, Any]] = []
    for idx, batch in enumerate(batches):
        score = affinity_scores.get(str(batch.get("product_id")), 0.0)
        boosted = score > boost_threshold
        scored.append(
            {
                **batch,
                "affinity": round(float(score), 4),
                "boosted": boosted,
                "_feed_index": idx,
            }
        )
    scored.sort(key=lambda b: (0 if b["boosted"] else 1, b["_feed_index"]))
    for b in scored:
        b.pop("_feed_index", None)
    return scored


# ── Phase 9 (补天): 战报声誉与贡献激励算子 ────────────────────────────────
# 全自动战报式评价体系的 oskill 层：纯内存计算，把新产生的战报融入批次的
# 动态特征池 (贝叶斯平滑) 与算力金发奖 (不看好坏，只看是否带图)。


def compute_batch_dynamic_rating(
    current_rating: float, current_count: int, new_polarity: float
) -> tuple[float, int]:
    """贝叶斯平滑算子：把单条情感极性 (-1.0 到 1.0) 融入批次累计评分 (0-100)。

    避免单一极端评价直接毁掉一个批次——新极性先映射到 0-100 常规可视化评分，
    再按历史条数加权平均 (历史评分持有更高的惯性权重：样本越多，单条新战报
    能撬动的幅度越小)。这是纯增量的滚动平均，累计评分可以一直挂在内存/DB
    快照上，不必每次全量重算。

    Args:
        current_rating: 批次当前累计动态评分 [0, 100]，无战报时为 0。
        current_count: 已参与累计的战报条数，非负。
        new_polarity: 新战报的情感极性 [-1.0, 1.0]，-1 为愤怒，1 为极度满意。

    Returns:
        (新累计评分 [0, 100], 新累计条数)。

    Raises:
        ValueError: current_rating 超出 [0, 100]，current_count 为负，或
            new_polarity 超出 [-1, 1]。
    """
    if not 0 <= current_rating <= 100:
        raise ValueError(
            "compute_batch_dynamic_rating: current_rating must be within [0, 100]"
        )
    if current_count < 0:
        raise ValueError(
            "compute_batch_dynamic_rating: current_count must be non-negative"
        )
    if not -1.0 <= new_polarity <= 1.0:
        raise ValueError(
            "compute_batch_dynamic_rating: new_polarity must be within [-1, 1]"
        )

    # 情感极性 (-1.0 到 1.0) 映射到常规的可视化评分 (0 到 100 分制)。
    normalized_new_score = (new_polarity + 1.0) / 2.0 * 100.0

    # 给历史评分更高的惯性权重 (贝叶斯平滑)。
    if current_count == 0:
        return (round(normalized_new_score, 2), 1)

    new_avg = (
        (current_rating * current_count) + normalized_new_score
    ) / (current_count + 1)
    return (round(new_avg, 2), current_count + 1)


#: 战报基础奖励 (分)。SPEC 写死：文本战报 0.2 元。
_BATTLE_REPORT_BASE_REWARD_CENTS = 20
#: 带图战报追加奖励 (分)。SPEC 写死：有图加 0.3 元。
_BATTLE_REPORT_IMAGE_BONUS_CENTS = 30


def calculate_battle_report_reward(has_image: bool) -> int:
    """贡献激励算子：系统为高质量战报付费 (最高 50 分 = 0.5 元算力金)。

    SPEC 原文的函数签名多带了一个 ``sentiment_polarity: float`` 参数，但函数体
    从头到尾没用过它——这里删掉，不为了跟 SPEC 字面签名一致保留一个没有任何
    作用的入参 (跟 compute_mercenary_bounty_rate 删掉死参数 current_price 是
    同一个取舍)。删掉它的业务理由正是 SPEC 自己要的：不以"好评"为奖励条件，
    真实的差评同样有价值 (帮系统排雷)，差评战报也照发基础奖励，只按是否带图
    加钱。

    Args:
        has_image: 战报是否附带实拍图。

    Returns:
        算力金奖励 (分)：20 + (30 if has_image else 0)，封顶 50。
    """
    reward = _BATTLE_REPORT_BASE_REWARD_CENTS
    if has_image:
        reward += _BATTLE_REPORT_IMAGE_BONUS_CENTS
    return reward


# ── Phase 10: IoT 边桥防腐层的物理→商业算子 ─────────────────────────────
# 纯内存计算，把重力传感器的原始读数翻译成商业决策。网桥 (MQTT 域) 不感知
# 商业语义，主干 (hemall) 不感知 MQTT——这些函数是两者在数值层面的咬合点。


def compute_cart_delta(delta_weight_grams: int, unit_weight_grams: int) -> int:
    """重力货架读数的增量 → 购物车数量增量 (可负：顾客把货放回)。

    ESP32 重力货架上报 delta_weight (拿走为负、放回为正)，单件标准重
    unit_weight 来自 hardware_shelf 映射。qty = round(delta / unit)——
    称重有噪声，round 到最近整数件，不累积小数。

    Args:
        delta_weight_grams: 重量变化 (克)，可负。
        unit_weight_grams: 单件标准重 (克)，必须为正。

    Returns:
        数量增量 (整数，可负)。

    Raises:
        ValueError: unit_weight_grams 非正。
    """
    if unit_weight_grams <= 0:
        raise ValueError("compute_cart_delta: unit_weight_grams must be positive")
    return int(round(delta_weight_grams / unit_weight_grams))


def decide_gate_reconcile(
    raw_weight_grams: int, expected_weight_grams: int, tolerance_grams: int
) -> str:
    """闸口对账裁决：实测重量 vs 期望重量 (tare + 已拣货累计)，三档判决。

    - |raw - expected| <= tolerance → "pass"   (绿：对账一致，放行)
    - <= tolerance * 2                          → "recheck" (黄：超差但可复核)
    - 否则                                      → "block"   (红：对不上，拦截)

    Args:
        raw_weight_grams: 闸口实测重量 (克)。
        expected_weight_grams: 期望重量 (克)。
        tolerance_grams: 公差 (克)，非负。

    Returns:
        "pass" / "recheck" / "block"。

    Raises:
        ValueError: tolerance_grams 为负。
    """
    if tolerance_grams < 0:
        raise ValueError("decide_gate_reconcile: tolerance_grams must be non-negative")

    deviation = abs(raw_weight_grams - expected_weight_grams)
    if deviation <= tolerance_grams:
        return "pass"
    if deviation <= tolerance_grams * 2:
        return "recheck"
    return "block"


def compute_tare_adjustment(
    raw_weight_grams: int, expected_weight_grams: int, max_tare_drift_grams: int
) -> int:
    """放行后的皮重漂移补偿：把残差吸收进 tare，但封顶防单次大幅跳变。

    对账一致 (pass) 时 raw 与 expected 的微小残差来自托盘/筐体本身的物理
    漂移 (温差、残留)。把残差 clamp 到 [-max_tare_drift, +max_tare_drift]
    后并入 tare——闸口长期不用人工校零。

    Args:
        raw_weight_grams: 实测重量 (克)。
        expected_weight_grams: 期望重量 (克)。
        max_tare_drift_grams: 单次允许的最大皮重调整 (克)，非负。

    Returns:
        皮重调整量 (克，可负)，绝对值不超过 max_tare_drift_grams。

    Raises:
        ValueError: max_tare_drift_grams 为负。
    """
    if max_tare_drift_grams < 0:
        raise ValueError(
            "compute_tare_adjustment: max_tare_drift_grams must be non-negative"
        )
    drift = raw_weight_grams - expected_weight_grams
    return round(max(-max_tare_drift_grams, min(max_tare_drift_grams, drift)))

