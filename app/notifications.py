"""商家新订单通知与投递记录。

真实通知渠道通过环境变量注入；未配置时绝不假装已通知商家。每次投递尝试
落 ``merchant_notification_delivery``，后台可看到最近失败与未配置状态。
"""

from __future__ import annotations

import asyncio
import json
import logging
import smtplib
from email.message import EmailMessage
from typing import Any

import httpx

from .config import Settings

logger = logging.getLogger("hemall.notifications")


def notification_status(settings: Settings) -> dict[str, Any]:
    provider = str(settings.merchant_notification_provider or "disabled").strip().lower()
    if provider == "webhook":
        configured = bool(settings.merchant_notification_webhook_url.strip())
    elif provider == "email":
        configured = bool(
            settings.merchant_smtp_host.strip()
            and settings.merchant_smtp_from.strip()
            and settings.merchant_smtp_to.strip()
        )
    else:
        configured = False
    return {
        "provider": provider,
        "configured": configured,
        "real_delivery_enabled": configured and provider in {"webhook", "email"},
        "message": (
            "商家新订单通知已配置"
            if configured
            else "商家新订单通知未配置；后台不会收到真实新订单提醒"
        ),
    }


def _order_text(order: dict[str, Any]) -> str:
    return (
        "Hemall 新订单\n"
        f"订单号: {order.get('order_id', '')}\n"
        f"金额: {order.get('grand_total_cents', 0)} 分\n"
        f"状态: {order.get('status', 'pending')}\n"
        "请登录商家后台处理。"
    )


async def _send_webhook(settings: Settings, order: dict[str, Any]) -> None:
    headers = {"Content-Type": "application/json"}
    token = settings.merchant_notification_webhook_token.strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            settings.merchant_notification_webhook_url,
            headers=headers,
            json={
                "msgtype": "text",
                "text": {"content": _order_text(order)},
                "order": order,
            },
        )
        response.raise_for_status()


def _send_email_sync(settings: Settings, order: dict[str, Any]) -> None:
    message = EmailMessage()
    message["Subject"] = f"Hemall 新订单 {order.get('order_id', '')}"
    message["From"] = settings.merchant_smtp_from
    message["To"] = settings.merchant_smtp_to
    message.set_content(_order_text(order))
    with smtplib.SMTP(
        settings.merchant_smtp_host,
        int(settings.merchant_smtp_port),
        timeout=15,
    ) as smtp:
        if settings.merchant_smtp_starttls:
            smtp.starttls()
        if settings.merchant_smtp_username:
            smtp.login(
                settings.merchant_smtp_username,
                settings.merchant_smtp_password,
            )
        smtp.send_message(message)


async def send_new_order_notification(
    pool: Any,
    settings: Settings,
    *,
    order: dict[str, Any],
) -> dict[str, Any]:
    """投递并记录结果；通知失败不回滚订单。"""
    status = notification_status(settings)
    provider = status["provider"]
    if not status["real_delivery_enabled"]:
        result = {
            "status": "skipped",
            "provider": provider,
            "error": "notification provider is not configured",
        }
    else:
        try:
            if provider == "webhook":
                await _send_webhook(settings, order)
            elif provider == "email":
                await asyncio.to_thread(_send_email_sync, settings, order)
            result = {"status": "sent", "provider": provider, "error": None}
        except Exception as exc:  # noqa: BLE001 - notification must not break checkout
            logger.exception("merchant new-order notification failed")
            result = {"status": "failed", "provider": provider, "error": str(exc)[:500]}

    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO merchant_notification_delivery
                    (order_id, provider, status, payload, last_error)
                VALUES ($1, $2, $3, $4::jsonb, $5)
                """,
                str(order.get("order_id") or ""),
                provider,
                result["status"],
                json.dumps(order, ensure_ascii=False),
                result["error"],
            )
    except Exception:  # noqa: BLE001 - delivery record is best effort
        logger.exception("failed to persist merchant notification delivery")
    return result


async def delivery_summary(pool: Any, settings: Settings) -> dict[str, Any]:
    status = notification_status(settings)
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) FILTER (WHERE status = 'failed') AS failed,
                    COUNT(*) FILTER (WHERE status = 'sent') AS sent,
                    MAX(created_at) FILTER (WHERE status = 'sent') AS last_sent_at,
                    MAX(created_at) FILTER (WHERE status = 'failed') AS last_failed_at
                FROM merchant_notification_delivery
                """
            )
        status.update(dict(row or {}))
    except Exception:  # noqa: BLE001 - status page should still render
        status.update(
            {"failed": None, "sent": None, "last_sent_at": None, "last_failed_at": None}
        )
    return status
