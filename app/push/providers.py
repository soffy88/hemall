"""Firebase Cloud Messaging (FCM) HTTP v1 客户端。

Phase 2: 为 hemall 提供 Android/iOS 设备的离线消息推送能力。

使用 Firebase Admin SDK (firebase-admin) 或手动维护服务账号密钥。

关键特性:
    - 支持 HTTPS v1 API (最新标准)
    - Token 批量推送 (up to 500 tokens per request)
    - Topic 订阅/退订 (按用户群组推送)
    - 设备 token 注册管理
    - 自动重试 + 失败 token 清理
    - 优雅降级 (无 credentials 时使用 sandbox)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from datetime import timedelta
from typing import Any

from ..config import Settings
from .models import PushNotification, PushPriority, PushResult

logger = logging.getLogger("hemall.push.fcm")


class FCMClient:
    """FCM HTTP v1 客户端封装。

    依赖:
        firebase-admin (可选，用于 credential 管理)

    环境变量:
        FIREBASE_CREDENTIALS_JSON: Base64 编码的 service-account-key.json
        FCM_SANDBOX: "1" 启用模拟模式 (无需真实 key)
    """

    _instance: FCMClient | None = None

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._initialized = False
        self._credentials_loaded = False
        self._project_id: str | None = None

    @classmethod
    async def create(cls, settings: Settings) -> FCMClient:
        if cls._instance is None:
            cls._instance = cls(settings)
        await cls._instance.initialize()
        return cls._instance

    @classmethod
    def get_instance(cls) -> FCMClient:
        if cls._instance is None:
            raise RuntimeError("FCMClient not initialized. Call create() first.")
        return cls._instance

    async def initialize(self) -> None:
        """加载 FCM credentials。"""
        try:
            credentials_json = self._settings.firebase_credentials or ""
            if credentials_json:
                # 尝试导入 firebase-admin
                try:
                    import firebase_admin
                    from firebase_admin import credentials

                    if not firebase_admin._apps:
                        cred_dict = json.loads(credentials_json)
                        cred = credentials.Certificate(cred_dict)
                        firebase_admin.initialize_app(cred)
                        self._credentials_loaded = True
                        logger.info("FCM credentials loaded (production mode)")
                except ImportError:
                    logger.warning("firebase-admin not installed — using sandbox mode")
                except Exception as exc:
                    logger.warning("FCM credential load failed (sandbox mode): %s", exc)

            if not self._credentials_loaded:
                # Sandbox mode
                logger.info("FCM running in SANDBOX mode (no real push)")

        except Exception as exc:
            logger.warning("FCM init failed (sandbox fallback): %s", exc)

        self._initialized = True

    async def send_to_device(self, notification: PushNotification) -> PushResult:
        """向单个设备发送推送通知。"""
        if not self._initialized:
            return self._mock_result(
                notification.target_device_ids[0] if notification.target_device_ids else ""
            )

        device_token = notification.target_device_ids[0] if notification.target_device_ids else ""

        try:
            # 构造 HTTP v1 payload (字段语义的单一真源; 生产路径据此映射为 SDK Message)
            payload = self._build_v1_payload(notification, device_token)

            # Sandbox: 无 credentials 时只记日志, 不真发 (行为不变)
            if not self._credentials_loaded:
                logger.debug(
                    "[FCM SANDBOX] Would POST %s: %s",
                    device_token,
                    payload,
                )
                return PushResult(device_token=device_token, success=True)

            # 生产: 经 Firebase Admin SDK 真发。messaging.send 是同步阻塞
            # 调用, 放到线程里避免堵事件循环。message_id 透出到 PushResult。
            message = self._to_messaging_message(payload)
            message_id = await asyncio.to_thread(self._send_via_sdk, message)
            logger.info("FCM sent to %s: %s", device_token, message_id)
            return PushResult(device_token=device_token, success=True, message_id=message_id)

        except Exception as exc:
            logger.error("FCM send failed for %s: %s", device_token, exc)
            return PushResult(
                device_token=device_token,
                success=False,
                error_code=getattr(exc, "http_status", None) or 500,
                error_message=str(exc),
            )

    @staticmethod
    def _send_via_sdk(message: Any) -> str:
        """同步发送一条 FCM 消息 (在线程中调用), 返回 FCM message_id。"""
        from firebase_admin import messaging

        result: str = messaging.send(message)
        return result

    def _to_messaging_message(self, payload: dict[str, Any]) -> Any:
        """把 _build_v1_payload 的 HTTP v1 请求体映射为 messaging.Message。

        字段语义与 _build_v1_payload 保持一致: android priority
        HIGH/NORMAL 映射 SDK 的 high/normal, ttl "Ns" 映射 timedelta,
        apns headers 与 aps (alert/sound/badge) 原样搬运。
        """
        from firebase_admin import messaging

        body = payload["message"]

        android_body = body.get("android", {})
        android_notification = android_body.get("notification", {})
        ttl: timedelta | None = None
        raw_ttl = android_body.get("ttl")
        if isinstance(raw_ttl, str) and raw_ttl.endswith("s"):
            ttl = timedelta(seconds=float(raw_ttl[:-1]))
        android_config = messaging.AndroidConfig(
            priority="high" if android_body.get("priority") == "HIGH" else "normal",
            ttl=ttl,
            collapse_key=android_body.get("collapse_key"),
            notification=messaging.AndroidNotification(
                sound=android_notification.get("sound"),
            ),
        )

        apns_body = body.get("apns", {})
        aps_body = apns_body.get("payload", {}).get("aps", {})
        alert_body = aps_body.get("alert", {})
        apns_config = messaging.APNSConfig(
            headers=dict(apns_body.get("headers", {})),
            payload=messaging.APNSPayload(
                aps=messaging.Aps(
                    alert=messaging.ApsAlert(
                        title=alert_body.get("title"),
                        body=alert_body.get("body"),
                    ),
                    sound=aps_body.get("sound"),
                    badge=aps_body.get("badge"),
                )
            ),
        )

        notification_body = body.get("notification", {})
        return messaging.Message(
            token=body.get("token", ""),
            notification=messaging.Notification(
                title=notification_body.get("title"),
                body=notification_body.get("body"),
            ),
            data=dict(body.get("data") or {}),
            android=android_config,
            apns=apns_config,
        )

    async def send_batch(self, notifications: list[PushNotification]) -> dict[str, Any]:
        """批量发送推送通知。"""
        results: list[PushResult] = []
        total = 0
        success = 0
        failed = 0

        for notification in notifications:
            result = await self.send_to_device(notification)
            results.append(result)
            total += 1
            if result.success:
                success += 1
            else:
                failed += 1

        logger.info("FCM batch complete: %d sent, %d success, %d failed", total, success, failed)
        return {
            "total": total,
            "success": success,
            "failed": failed,
            "results": [r.model_dump(mode="json") for r in results],
        }

    async def subscribe_topic(self, topic: str, tokens: list[str]) -> bool:
        """订阅 topic (群组推送)。

        Args:
            topic: 话题名称 (如: orders:user-123, promotions:new-year)
            tokens: 设备 token 列表
        """
        if not self._credentials_loaded:
            logger.debug("[FCM SANDBOX] Subscribe topic %s with %d tokens", topic, len(tokens))
            return True

        try:
            from firebase_admin import messaging

            response = await asyncio.to_thread(messaging.subscribe_to_topic, tokens, topic)
            if response.failure_count:
                logger.error(
                    "FCM topic subscribe partial failure for %s: %s",
                    topic,
                    response.errors,
                )
                return False
            return True
        except Exception as exc:
            logger.error("FCM topic subscribe failed: %s", exc)
            return False

    async def unsubscribe_topic(self, topic: str, tokens: list[str]) -> bool:
        """退订 topic。"""
        if not self._credentials_loaded:
            return True
        try:
            from firebase_admin import messaging

            response = await asyncio.to_thread(messaging.unsubscribe_from_topic, tokens, topic)
            return not response.failure_count
        except Exception:
            return False

    def _build_v1_payload(
        self, notification: PushNotification, device_token: str
    ) -> dict[str, Any]:
        """构建 FCM HTTP v1 请求体。"""
        priority_map = {
            PushPriority.LOW: "NORMAL",
            PushPriority.NORMAL: "NORMAL",
            PushPriority.HIGH: "HIGH",
        }

        payload: dict[str, Any] = {
            "message": {
                "token": device_token,
                "notification": {
                    "title": notification.title,
                    "body": notification.body,
                },
                "data": notification.data,
                "android": {
                    "priority": priority_map.get(notification.priority, "NORMAL"),
                    "notification": {
                        "sound": notification.sound,
                    },
                },
                "apns": {
                    "headers": {
                        "apns-priority": "10" if notification.priority == PushPriority.HIGH else "5"
                    },
                    "payload": {
                        "aps": {
                            "alert": {
                                "title": notification.title,
                                "body": notification.body,
                            },
                            "sound": notification.sound,
                        }
                    },
                },
            }
        }

        if notification.ttl_seconds:
            payload["message"]["android"]["ttl"] = f"{notification.ttl_seconds}s"

        if notification.collapse_key:
            payload["message"]["android"]["collapse_key"] = notification.collapse_key

        if notification.badge is not None:
            payload["message"]["apns"]["payload"]["aps"]["badge"] = notification.badge

        return payload

    def _mock_result(self, device_token: str) -> PushResult:
        """SANDBOX 模式下的模拟结果。"""
        return PushResult(device_token=device_token, success=True)


# ── APNs Apple Push Notification Service ───────────────────────────


class APNSClient:
    """APNs HTTPS Provider 客户端 (httpx HTTP/2 直连, 无 apns2 依赖)。

    apns2 已于 2025-02 从依赖中移除 (见 pyproject.toml: 其 hyper 钉死
    hyperframe<4, 与 Py3.12 的 h2 4.x 根本冲突)。这里改走官方 HTTP/2
    接口: provider token 用 PyJWT 以 ES256 现签 (header kid=key_id,
    claims iss=team_id + iat), 缓存 50 分钟 (Apple 上限 60 分钟)。

    配置 (app/config.py Settings):
        APNS_KEY_ID: 推送密钥 ID (Apple Developer Portal)
        APNS_TEAM_ID: Apple Developer Team ID
        APNS_CERT_BASE: Base64 编码的 .p8 私钥内容
          (Settings 字段名是 apns_cert_base; 历史代码误读
          apns_cert_base64, 已修正为真实字段)
        APNS_BUNDLE_ID: iOS App Bundle ID (作 apns-topic)
        APNS_SANDBOX: "1" 使用 sandbox 网关, 生产置 "0"
    """

    # provider token 缓存时长: Apple 允许最长 60 分钟, 提前刷新
    _JWT_TTL_SECONDS = 50 * 60

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._initialized = False
        self._client_available = False
        self._private_key_pem: str | None = None
        self._http_client: Any = None
        self._jwt_token: str | None = None
        self._jwt_expires_at: float = 0.0

    async def initialize(self) -> None:
        """校验 APNs 配置: key_id/team_id + base64 编码的 .p8 私钥。

        材料齐全且私钥可加载 → 生产模式; 否则保持 SANDBOX (只记日志)。
        """
        try:
            key_id = self._settings.apns_key_id or ""
            team_id = self._settings.apns_team_id or ""
            cert_base = self._settings.apns_cert_base or ""

            if key_id and team_id and cert_base:
                pem = self._decode_private_key(cert_base)
                if pem is not None:
                    self._private_key_pem = pem
                    self._client_available = True
                    logger.info("APNs initialized: %s", self._host())
                else:
                    logger.warning("APNs apns_cert_base is not a loadable .p8 key - SANDBOX mode")
            else:
                logger.info("APNs running in SANDBOX mode (no certificate)")

        except Exception as exc:
            logger.warning("APNs init failed: %s", exc)

        self._initialized = True

    @staticmethod
    def _decode_private_key(cert_base: str) -> str | None:
        """把 settings.apns_cert_base 解码为 PEM 私钥文本; 不可用返回 None。

        约定格式: Base64 编码的 .p8 (PKCS#8 PEM) 内容。值本身已是 PEM
        文本 (含 PRIVATE KEY 头) 时也接受。解码后实际加载一遍私钥,
        坏材料直接判不可用, 避免发送时才炸。
        """
        candidate = cert_base.strip()
        if "PRIVATE KEY" not in candidate:
            try:
                candidate = base64.b64decode(candidate, validate=True).decode("utf-8")
            except Exception:
                return None
        if "PRIVATE KEY" not in candidate:
            return None
        try:
            from cryptography.hazmat.primitives.serialization import (
                load_pem_private_key,
            )

            load_pem_private_key(candidate.encode("utf-8"), password=None)
        except Exception:
            return None
        return candidate

    def _host(self) -> str:
        return (
            "api.sandbox.push.apple.com"
            if self._settings.apns_sandbox == "1"
            else "api.push.apple.com"
        )

    def _provider_token(self) -> str:
        """生成/复用 APNs provider token (ES256 JWT, 缓存 50 分钟)。"""
        now = time.time()
        if self._jwt_token and now < self._jwt_expires_at:
            return self._jwt_token

        key_pem = self._private_key_pem
        if key_pem is None:
            raise RuntimeError("APNs private key not loaded")

        import jwt

        token: str = jwt.encode(
            {"iss": self._settings.apns_team_id, "iat": int(now)},
            key_pem,
            algorithm="ES256",
            headers={"kid": self._settings.apns_key_id},
        )
        self._jwt_token = token
        self._jwt_expires_at = now + self._JWT_TTL_SECONDS
        return token

    def _get_http_client(self) -> Any:
        """懒创建并复用 httpx AsyncClient (HTTP/2)。"""
        if self._http_client is None:
            import httpx

            self._http_client = httpx.AsyncClient(http2=True, timeout=10.0)
        return self._http_client

    async def send_notification(self, notification: PushNotification) -> PushResult:
        """发送 APNs 通知。"""
        device_token = notification.target_device_ids[0] if notification.target_device_ids else ""

        try:
            if not self._initialized or not self._client_available:
                logger.debug("[APNS SANDBOX] Would send: %s", notification.title)
                return PushResult(device_token=device_token, success=True)

            # 构造 APNs payload
            aps_payload: dict[str, Any] = {
                "alert": {
                    "title": notification.title,
                    "body": notification.body,
                },
                "sound": notification.sound,
            }

            if notification.badge is not None:
                aps_payload["badge"] = notification.badge

            # 合并自定义 data
            apns_payload: dict[str, Any] = {"aps": aps_payload}
            apns_payload.update(notification.data)

            # 转换为 JSON
            payload = json.dumps(apns_payload)

            # 转换 device_token (移除空格)
            clean_token = device_token.replace(" ", "")

            headers = {
                "authorization": f"bearer {self._provider_token()}",
                "apns-topic": self._settings.apns_bundle_id or "com.hemall.app",
                "apns-priority": "10" if notification.priority == PushPriority.HIGH else "5",
            }
            if notification.ttl_seconds:
                headers["apns-expiration"] = str(int(time.time()) + notification.ttl_seconds)

            client = self._get_http_client()
            response = await client.post(
                f"https://{self._host()}/3/device/{clean_token}",
                content=payload.encode("utf-8"),
                headers=headers,
            )

            if response.status_code == 200:
                logger.debug(
                    "APNs sent to %s (apns-id: %s)",
                    clean_token,
                    response.headers.get("apns-id", ""),
                )
                return PushResult(device_token=device_token, success=True)

            try:
                reason = str(response.json().get("reason", ""))
            except Exception:
                reason = response.text
            logger.error(
                "APNs send failed for %s: HTTP %s %s",
                device_token,
                response.status_code,
                reason,
            )
            return PushResult(
                device_token=device_token,
                success=False,
                error_code=response.status_code,
                error_message=reason or f"HTTP {response.status_code}",
            )

        except Exception as exc:
            logger.error("APNs send failed for %s: %s", device_token, exc)
            return PushResult(
                device_token=device_token,
                success=False,
                error_code=500,
                error_message=str(exc),
            )
