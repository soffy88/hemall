"""合规文档、顾客同意记录与短信验证码配置门禁。

法定文本只从 ``content/compliance`` 或部署时挂载的 manifest 读取。仓库默认
不放伪造文本：缺失文件时 API 明确返回“即将公布”，站点状态保持未正式营业。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request

from .config import Settings

DOCUMENT_SLUGS: tuple[str, ...] = (
    "privacy",
    "terms",
    "returns",
    "food-license",
    "business-license",
    "invoice",
    "complaint",
    "about",
    "contact",
)

_DEFAULT_TITLES = {
    "privacy": "隐私政策",
    "terms": "用户协议",
    "returns": "退换货规则（含生鲜规则）",
    "food-license": "食品经营资质",
    "business-license": "营业执照",
    "invoice": "发票说明",
    "complaint": "投诉与售后",
    "about": "关于我们",
    "contact": "联系我们",
}


def _manifest_path(settings: Settings) -> Path:
    path = Path(settings.compliance_manifest_path)
    if path.is_absolute():
        return path
    return Path(__file__).resolve().parents[1] / path


def load_manifest(settings: Settings) -> dict[str, Any]:
    """读取并规范化 manifest；缺失/损坏时全部文档按未发布处理。"""
    raw: dict[str, Any] = {}
    path = _manifest_path(settings)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            raw = value
    except (OSError, ValueError, TypeError):
        raw = {}

    raw_docs = raw.get("documents")
    docs_raw = raw_docs if isinstance(raw_docs, dict) else {}
    docs: dict[str, dict[str, Any]] = {}
    for slug in DOCUMENT_SLUGS:
        candidate = docs_raw.get(slug)
        item = candidate if isinstance(candidate, dict) else {}
        title = str(item.get("title") or _DEFAULT_TITLES[slug]).strip()[:80]
        filename = str(item.get("file") or "").strip()
        content = ""
        if filename:
            try:
                content = (path.parent / filename).read_text(encoding="utf-8").strip()
            except OSError:
                content = ""
        published = bool(item.get("published")) and bool(content)
        docs[slug] = {
            "slug": slug,
            "title": title,
            "version": str(item.get("version") or "placeholder").strip()[:80],
            "published": published,
            "status": "published" if published else "coming_soon",
            "content": content if published else "即将公布",
        }

    consent_version = str(raw.get("consent_version") or "placeholder").strip()[:80]
    if not consent_version:
        consent_version = "placeholder"
    all_published = all(doc["published"] for doc in docs.values())
    official_open = bool(settings.site_official_open) and all_published
    return {
        "official_open": official_open,
        "site_notice": (
            "正式营业"
            if official_open
            else "本站尚未正式营业；展示内容仅供内部验收，请勿对外收款或承诺履约。"
        ),
        "consent_version": consent_version,
        "all_required_documents_published": all_published,
        "documents": docs,
    }


def document_payload(settings: Settings, slug: str) -> dict[str, Any]:
    manifest = load_manifest(settings)
    doc = manifest["documents"].get(slug)
    if doc is None:
        raise HTTPException(404, "compliance document not found")
    return {
        **doc,
        "official_open": manifest["official_open"],
        "site_notice": manifest["site_notice"],
        "consent_version": manifest["consent_version"],
    }


def validate_consent(
    settings: Settings,
    *,
    privacy_consent: bool,
    terms_consent: bool,
    consent_version: str,
) -> str:
    """校验注册/下单的显式同意，并返回服务端当前版本号。"""
    manifest = load_manifest(settings)
    if not privacy_consent:
        raise HTTPException(400, "必须同意隐私政策后才能继续")
    if not terms_consent:
        raise HTTPException(400, "必须同意用户协议后才能继续")
    if not consent_version or consent_version != manifest["consent_version"]:
        raise HTTPException(409, "隐私政策或用户协议已更新，请刷新页面后重新确认")
    return str(manifest["consent_version"])


def request_metadata(request: Request | None) -> dict[str, Any]:
    if request is None:
        return {}
    client_ip = request.client.host if request.client else ""
    user_agent = request.headers.get("user-agent", "")[:500]
    return {
        "ip_hash": (hashlib.sha256(client_ip.encode("utf-8")).hexdigest() if client_ip else ""),
        "user_agent": user_agent,
    }


async def record_consents(
    conn: Any,
    *,
    subject_type: str,
    subject_id: str,
    version: str,
    request: Request | None = None,
) -> None:
    """幂等记录隐私政策与用户协议同意。"""
    metadata = json.dumps(request_metadata(request), ensure_ascii=False)
    for slug in ("privacy", "terms"):
        await conn.execute(
            """
            INSERT INTO consent_record
                (subject_type, subject_id, document_slug, document_version,
                 consented_at, metadata)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            ON CONFLICT (subject_type, subject_id, document_slug, document_version)
            DO NOTHING
            """,
            subject_type,
            str(subject_id),
            slug,
            version,
            datetime.now(UTC),
            metadata,
        )


def sms_status(settings: Settings) -> dict[str, Any]:
    provider = str(settings.sms_provider or "disabled").strip().lower()
    configured = provider == "webhook" and bool(settings.sms_webhook_url.strip())
    return {
        "provider": provider,
        "configured": configured,
        "enabled": configured,
        "message": (
            "短信验证码已配置" if configured else "短信验证码未配置；当前注册不发送短信验证码"
        ),
    }
