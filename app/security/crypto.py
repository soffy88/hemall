"""敏感数据加密 — 基于 cryptography.Fernet (AES-128-CBC + HMAC-SHA256)。

用途:
    客户手机号、收货地址、身份证等 PII (Personally Identifiable Information)
    在落库前透明加密，读取时解密。

配置:
    环境变量 ENCRYPTION_KEY (Fernet key, 32 字节 base64)
    自动生成: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

用法:
    from app.security.crypto import encrypt_field, decrypt_field

    # 写入时
    customer.phone = encrypt_field(customer.phone)

    # 读取时
    customer.phone = decrypt_field(customer.phone)

    # 或使用 Pydantic 字段加密装饰器
    @encrypt_on_save("phone", "address", "id_card")
    class CustomerOutput(BaseModel):
        ...
"""

from __future__ import annotations

import base64
import logging
import os
from functools import wraps
from typing import Any, Callable, TypeVar

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger("hemall.security.crypto")

_fernet_instance: Fernet | None = None


def _get_fernet() -> Fernet:
    """获取 Fernet 实例 (懒初始化，进程内单例)。"""
    global _fernet_instance
    if _fernet_instance is None:
        key = os.getenv("ENCRYPTION_KEY")
        if not key:
            # 开发环境自动生成 (警告！生产环境必须显式配置)
            key = Fernet.generate_key().decode()
            logger.warning(
                "ENCRYPTION_KEY not set — auto-generated key (NOT for production!)"
            )
        _fernet_instance = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet_instance


def encrypt_field(value: str | None) -> str | None:
    """加密单个字段值。

    返回 base64 编码的密文 (可直接存入 VARCHAR 列)。
    None 输入返回 None (跳过加密)。
    """
    if value is None or value == "":
        return value
    try:
        encrypted = _get_fernet().encrypt(value.encode("utf-8"))
        return encrypted.decode("utf-8")
    except Exception as exc:
        logger.error("encryption failed for field value (len=%d): %s", len(value), exc)
        raise ValueError("field encryption failed") from exc


def decrypt_field(value: str | None) -> str | None:
    """解密单个字段值。

    输入为 encrypt_field 返回的 base64 密文。
    None 输入返回 None。解密失败时返回原始值 (降级处理)。
    """
    if value is None or value == "":
        return value
    try:
        decrypted = _get_fernet().decrypt(value.encode("utf-8"))
        return decrypted.decode("utf-8")
    except InvalidToken:
        # 可能是未加密的明文 (历史数据迁移前), 降级返回原值。这是唯一允许的
        # "原样返回" 分支——仅针对 Fernet 判定的非法/明文 token。warning（不记值）
        # 让明密混存可被观测，迁移完成后应清零此日志。
        logger.warning("decrypt_field: value appears unencrypted, returning as-is")
        return value
    except Exception as exc:
        # 其它异常 (如 key 配置错误) 属真实故障: 绝不能静默把密文当明文回传,
        # 否则加密形同虚设。向上抛出让调用方感知。
        logger.error("decryption failed: %s", exc)
        raise ValueError("field decryption failed") from exc


def encrypt_fields_dict(data: dict[str, Any], fields: list[str]) -> dict[str, Any]:
    """批量加密字典中的指定字段。"""
    result = dict(data)
    for field in fields:
        if field in result and result[field] is not None:
            result[field] = encrypt_field(str(result[field]))
    return result


def decrypt_fields_dict(data: dict[str, Any], fields: list[str]) -> dict[str, Any]:
    """批量解密字典中的指定字段。"""
    result = dict(data)
    for field in fields:
        if field in result and result[field] is not None:
            result[field] = decrypt_field(result[field])
    return result


# ── 敏感字段声明 ─────────────────────────────────────────────────────

#: 客户表中的敏感字段列表
CUSTOMER_PII_FIELDS = ["phone", "address", "id_card", "email"]

#: 收货地址表中的敏感字段列表
ADDRESS_PII_FIELDS = ["phone", "address_1", "address_2", "postal_code"]

#: 支付会话中的敏感字段列表
PAYMENT_PII_FIELDS = ["payer_info"]


def mask_phone(phone: str | None) -> str:
    """脱敏显示手机号: 138****1234。"""
    if not phone or len(phone) < 7:
        return phone or ""
    return phone[:3] + "****" + phone[-4:]


def mask_id_card(id_card: str | None) -> str:
    """脱敏显示身份证: 110***********1234。"""
    if not id_card or len(id_card) < 8:
        return id_card or ""
    return id_card[:3] + "*" * (len(id_card) - 7) + id_card[-4:]
