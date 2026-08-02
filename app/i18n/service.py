"""hemall 国际化与本地化支持模块 — i18n 多语言、货币、区域策略。

Phase 3 Priority 5: 支持多语言商品展示、区域价格策略和本地化支付。

核心功能:
    - 多语言翻译管理 (gettext .po/.mo 格式)
    - 商品信息多语言副本 (名称/描述/规格)
    - 区域价格策略 (按国家/地区定价)
    - 货币自动转换 (fx 汇率)
    - 本地化支付网关路由
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger("hemall.i18n")


# ── 语言与区域 ──────────────────────────────────────────────────────


class Language(Enum):
    ZH_CN = "zh-CN"  # 简体中文
    ZH_TW = "zh-TW"  # 繁体中文
    EN_US = "en-US"  # 美式英语
    EN_GB = "en-GB"  # 英式英语
    JA_JP = "ja-JP"  # 日语
    KO_KR = "ko-KR"  # 韩语
    FR_FR = "fr-FR"  # 法语
    DE_DE = "de-DE"  # 德语
    ES_ES = "es-ES"  # 西班牙语
    AR_AE = "ar-AE"  # 阿拉伯语


class Currency(Enum):
    CNY = "CNY"  # 人民币
    USD = "USD"  # 美元
    EUR = "EUR"  # 欧元
    JPY = "JPY"  # 日元
    GBP = "GBP"  # 英镑
    KRW = "KRW"  # 韩元
    HKD = "HKD"  # 港币
    SGD = "SGD"  # 新加坡元


# ── 翻译管理 ──────────────────────────────────────────────────────


@dataclass
class TranslationEntry:
    """翻译条目。"""

    key: str
    translations: dict[str, str] = field(default_factory=dict)

    def get(self, lang: str, default: str = "") -> str:
        return self.translations.get(lang, default or self.key)


class TranslationManager:
    """翻译管理器 — 支持按语言加载翻译。"""

    def __init__(self) -> None:
        self._translations: dict[str, dict[str, str]] = {}

    def load_from_json(self, lang: str, filepath: str) -> None:
        """从 JSON 文件加载翻译。"""
        if not os.path.exists(filepath):
            logger.warning("Translation file not found: %s", filepath)
            return
        with open(filepath, encoding="utf-8") as f:
            data = json.load(f)
        self._translations[lang] = data
        logger.info("Loaded %d translations for %s", len(data), lang)

    def register_translations(self, lang: str, entries: dict[str, str]) -> None:
        """注册翻译条目。"""
        if lang not in self._translations:
            self._translations[lang] = {}
        self._translations[lang].update(entries)

    def translate(self, key: str, lang: str = "zh-CN", default: str | None = None) -> str:
        """翻译 key 到指定语言。"""
        lang_data = self._translations.get(lang, {})
        return lang_data.get(key, default or key)

    def localize_product(self, product: dict[str, Any], lang: str) -> dict[str, Any]:
        """本地化商品信息。"""
        localized = {}
        for field_name, translations in product.items():
            if isinstance(translations, dict) and lang in translations:
                localized[field_name] = translations[lang]
            else:
                localized[field_name] = translations
        return localized


# ── 区域价格策略 ──────────────────────────────────────────────────


class RegionalPrice(BaseModel):
    """区域价格。"""

    product_id: str
    region: str
    currency: Currency
    price_cents: int
    tax_rate: float = 0.0
    valid_from: datetime = field(default_factory=datetime.now)
    valid_until: datetime | None = None


class CurrencyConverter:
    """货币转换器 (基于固定汇率或 API)。"""

    def __init__(self) -> None:
        # 默认汇率 (以 CNY 为基准)
        self._rates: dict[str, float] = {
            "CNY": 1.0,
            "USD": 0.14,
            "EUR": 0.13,
            "JPY": 21.0,
            "GBP": 0.11,
            "KRW": 185.0,
            "HKD": 1.09,
            "SGD": 0.19,
        }

    def convert(self, amount_cents: int, from_currency: str, to_currency: str) -> int:
        """转换金额 (分 -> 分)。"""
        if from_currency == to_currency:
            return amount_cents
        from_rate = self._rates.get(from_currency, 1.0)
        to_rate = self._rates.get(to_currency, 1.0)
        converted = amount_cents * (to_rate / from_rate)
        return round(converted)

    def format_price(self, amount_cents: int, currency: str) -> str:
        """格式化价格显示。"""
        amount = amount_cents / 100
        symbols = {
            "CNY": "¥", "USD": "$", "EUR": "€", "JPY": "¥",
            "GBP": "£", "KRW": "₩", "HKD": "HK$", "SGD": "S$",
        }
        symbol = symbols.get(currency, "")
        if currency == "JPY" or currency == "KRW":
            return f"{symbol}{amount_cents:,}"
        return f"{symbol}{amount:,.2f}"


# ── 本地化支付网关路由 ──────────────────────────────────────────


class LocalizedPaymentRouter:
    """本地化支付网关路由 — 按区域选择最佳支付方式。"""

    def __init__(self) -> None:
        self._region_providers: dict[str, list[str]] = {
            "CN": ["wechat", "alipay", "unionpay"],
            "US": ["stripe", "paypal", "apple_pay"],
            "EU": ["stripe", "paypal", "sofort"],
            "JP": ["paypay", "rakuten_pay", "stripe"],
            "KR": ["kakao_pay", "naver_pay", "toss"],
        }

    def get_available_providers(self, region: str) -> list[str]:
        """获取区域可用支付方式。"""
        return self._region_providers.get(region, ["stripe"])

    def get_default_provider(self, region: str) -> str:
        """获取区域默认支付方式。"""
        providers = self._region_providers.get(region, ["stripe"])
        return providers[0] if providers else "stripe"