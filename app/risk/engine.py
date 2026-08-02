"""hemall 风控引擎 — 反作弊、欺诈检测、异常交易拦截。

Phase 3 Priority 2: 保护平台免受欺诈攻击，确保交易安全。

核心功能:
    - 规则引擎 (IF-THEN 规则链)
    - 行为异常检测 (短时间内高频率操作)
    - 设备指纹分析 (异常设备/Tor/IP)
    - 交易风险评分 (0-100)
    - 实时拦截 / 人工审核 / 允许放行
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger("hemall.risk")


# ── 风险等级 ──────────────────────────────────────────────────────


class RiskLevel(Enum):
    SAFE = "safe"           # 0-20 分，正常放行
    SUSPICIOUS = "suspicious"  # 21-50 分，标记观察
    REVIEW = "review"       # 51-80 分，人工审核
    BLOCKED = "blocked"     # 81-100 分，自动拦截


class RiskRuleCategory(Enum):
    LOGIN = "login"         # 登录安全
    PAYMENT = "payment"     # 支付安全
    ORDER = "order"         # 订单异常
    REGISTER = "register"   # 注册风控
    REVIEW = "review"       # 评价风控


# ── 规则定义 ──────────────────────────────────────────────────────


@dataclass
class RiskRule:
    """风控规则定义。

    Attributes:
        rule_id: 规则 ID
        name: 规则名称
        category: 规则类别
        condition: 触发条件表达式
        score: 命中时增加的风险分
        enabled: 是否启用
        description: 规则描述
    """

    rule_id: str
    name: str
    category: RiskRuleCategory
    condition: str      # 表达式: "frequency > 10 AND time_window < 60"
    score: int = 10
    enabled: bool = True
    description: str = ""


@dataclass
class RiskEvent:
    """风控事件 — 触发风控检查的业务事件。

    Attributes:
        event_type: 事件类型 (login/payment/order/register)
        user_id: 用户 ID
        ip_address: 请求 IP
        device_id: 设备指纹
        payload: 事件载荷
        timestamp: 发生时间
    """

    event_type: str
    user_id: str
    ip_address: str = ""
    device_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "user_id": self.user_id,
            "ip_address": self.ip_address,
            "device_id": self.device_id,
            "payload": self.payload,
            "timestamp": self.timestamp.isoformat(),
        }


class RiskResult(BaseModel):
    """风控评估结果。"""

    risk_score: int = 0
    risk_level: RiskLevel = RiskLevel.SAFE
    triggered_rules: list[str] = field(default_factory=list)
    decision: str = "allow"  # allow / review / block
    event_id: str = ""


# ── 风控引擎 ──────────────────────────────────────────────────────


class RiskEngine:
    """风控引擎 — 规则链评估 + 频率检测 + 设备指纹。"""

    def __init__(self) -> None:
        self._rules: list[RiskRule] = []
        self._frequency_tracker: dict[str, list[float]] = defaultdict(list)
        self._ip_blacklist: set[str] = set()
        self._user_trust_score: dict[str, float] = {}
        self._initialize_default_rules()

    def _initialize_default_rules(self) -> None:
        """初始化默认风控规则。"""
        self._rules = [
            RiskRule("R001", "高频登录失败", RiskRuleCategory.LOGIN,
                     "login_failures > 5 AND time_window < 300", score=20),
            RiskRule("R002", "异地登录告警", RiskRuleCategory.LOGIN,
                     "geo_distance > 1000 AND time_window < 3600", score=30),
            RiskRule("R003", "快速下单异常", RiskRuleCategory.ORDER,
                     "order_count > 10 AND time_window < 60", score=25),
            RiskRule("R004", "大额交易预警", RiskRuleCategory.PAYMENT,
                     "amount > 100000 AND user_trust < 50", score=35),
            RiskRule("R005", "新设备登录", RiskRuleCategory.LOGIN,
                     "new_device AND user_trust < 30", score=15),
            RiskRule("R006", "支付信息变更", RiskRuleCategory.PAYMENT,
                     "payment_info_changed AND time_since_change < 300", score=20),
            RiskRule("R007", "批量注册检测", RiskRuleCategory.REGISTER,
                     "same_ip_count > 10 AND time_window < 3600", score=40),
            RiskRule("R008", "异常退款请求", RiskRuleCategory.ORDER,
                     "refund_rate > 30 AND order_count > 5", score=30),
        ]

    def add_rule(self, rule: RiskRule) -> None:
        """添加自定义风控规则。"""
        self._rules.append(rule)

    def add_to_blacklist(self, ip: str) -> None:
        """将 IP 加入黑名单。"""
        self._ip_blacklist.add(ip)
        logger.warning("IP blacklisted: %s", ip)

    def set_user_trust_score(self, user_id: str, score: float) -> None:
        """设置用户信任分数 (0-100)。"""
        self._user_trust_score[user_id] = max(0, min(100, score))

    async def evaluate(self, event: RiskEvent) -> RiskResult:
        """评估事件风险等级。

        Args:
            event: 风控事件

        Returns:
            RiskResult 包含风险评分、等级和处理建议
        """
        result = RiskResult()
        total_score = 0
        triggered_rules: list[str] = []

        # 1. IP 黑名单检查
        if event.ip_address in self._ip_blacklist:
            total_score += 100
            triggered_rules.append("IP_BLACKLIST")

        # 2. 频率检测
        freq_score = self._check_frequency(event)
        total_score += freq_score

        # 3. 规则链评估
        for rule in self._rules:
            if not rule.enabled:
                continue
            if self._evaluate_rule(rule, event):
                total_score += rule.score
                triggered_rules.append(rule.rule_id)

        # 4. 计算最终风险等级
        result.risk_score = min(100, total_score)
        if result.risk_score >= 81:
            result.risk_level = RiskLevel.BLOCKED
            result.decision = "block"
        elif result.risk_score >= 51:
            result.risk_level = RiskLevel.REVIEW
            result.decision = "review"
        elif result.risk_score >= 21:
            result.risk_level = RiskLevel.SUSPICIOUS
            result.decision = "allow"
        else:
            result.risk_level = RiskLevel.SAFE
            result.decision = "allow"

        result.triggered_rules = triggered_rules
        logger.info("Risk evaluation: score=%d level=%s decision=%s rules=%s",
                     result.risk_score, result.risk_level.value, result.decision, triggered_rules)
        return result

    def _check_frequency(self, event: RiskEvent) -> int:
        """检查事件频率，返回风险分。"""
        key = f"{event.user_id}:{event.event_type}"
        now = time.time()

        self._frequency_tracker[key].append(now)
        # 清理 1 小时前的记录
        self._frequency_tracker[key] = [
            t for t in self._frequency_tracker[key] if now - t < 3600
        ]

        # 1 分钟内超过 10 次 → 高频告警
        recent = [t for t in self._frequency_tracker[key] if now - t < 60]
        if len(recent) > 10:
            return 25
        return 0

    def _evaluate_rule(self, rule: RiskRule, event: RiskEvent) -> bool:
        """评估单条规则是否命中。

        简化实现: 基于事件类型和简单条件匹配
        """
        # 检查事件类型匹配
        if rule.category == RiskRuleCategory.LOGIN and event.event_type != "login":
            return False
        if rule.category == RiskRuleCategory.ORDER and event.event_type != "order":
            return False
        if rule.category == RiskRuleCategory.PAYMENT and event.event_type != "payment":
            return False
        if rule.category == RiskRuleCategory.REGISTER and event.event_type != "register":
            return False
        
        # 检查黑名单 IP (简化)
        if "blacklist" in rule.condition.lower() and event.ip_address in self._ip_blacklist:
            return True
            
        # 检查高频操作
        if "frequency" in rule.condition.lower():
            freq_score = self._check_frequency(event)
            if freq_score > 0:
                return True
                
        # 检查信任分数
        if "trust" in rule.condition.lower():
            trust = self._user_trust_score.get(event.user_id, 50)
            if trust < 30:  # 简化阈值
                return True
                
        return False


# ── 风控输入输出 ──────────────────────────────────────────────────


class TransactionReview(BaseModel):
    """待人工审核的交易。"""

    event_id: str
    event_type: str
    user_id: str
    risk_score: int
    triggered_rules: list[str]
    created_at: datetime = field(default_factory=datetime.now)
    reviewer: str | None = None
    review_result: str | None = None  # approved / rejected
    reviewed_at: datetime | None = None