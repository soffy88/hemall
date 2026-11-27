"""P0-12/P0-10: 合规占位、显式同意、短信关闭层与商家通知配置状态。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.compliance import DOCUMENT_SLUGS, load_manifest, validate_consent
from app.config import Settings
from app.notifications import notification_status


def test_default_manifest_is_placeholder_and_site_not_open():
    manifest = load_manifest(Settings())
    assert manifest["official_open"] is False
    assert manifest["all_required_documents_published"] is False
    assert manifest["consent_version"] == "2026-11-08-placeholder"
    assert set(manifest["documents"]) == set(DOCUMENT_SLUGS)
    assert all(doc["content"] == "即将公布" for doc in manifest["documents"].values())


def test_site_cannot_open_until_all_documents_are_published(tmp_path: Path):
    manifest_path = tmp_path / "manifest.json"
    docs = {}
    for slug in DOCUMENT_SLUGS:
        (tmp_path / f"{slug}.md").write_text(f"{slug} official text", encoding="utf-8")
        docs[slug] = {
            "title": slug,
            "file": f"{slug}.md",
            "version": "v1",
            "published": True,
        }
    manifest_path.write_text(
        json.dumps({"consent_version": "v1", "documents": docs}),
        encoding="utf-8",
    )
    settings = Settings(
        compliance_manifest_path=str(manifest_path),
        site_official_open=True,
    )
    manifest = load_manifest(settings)
    assert manifest["official_open"] is True
    assert manifest["documents"]["privacy"]["content"] == "privacy official text"

    docs["privacy"]["published"] = False
    manifest_path.write_text(
        json.dumps({"consent_version": "v1", "documents": docs}),
        encoding="utf-8",
    )
    assert load_manifest(settings)["official_open"] is False


@pytest.mark.parametrize(
    "privacy,terms,version",
    [
        (False, True, "2026-11-08-placeholder"),
        (True, False, "2026-11-08-placeholder"),
        (True, True, "stale-version"),
    ],
)
def test_consent_rejects_missing_or_stale_acceptance(privacy, terms, version):
    with pytest.raises(HTTPException) as exc:
        validate_consent(
            Settings(),
            privacy_consent=privacy,
            terms_consent=terms,
            consent_version=version,
        )
    assert exc.value.status_code in {400, 409}


def test_consent_accepts_current_versions():
    assert (
        validate_consent(
            Settings(),
            privacy_consent=True,
            terms_consent=True,
            consent_version="2026-11-08-placeholder",
        )
        == "2026-11-08-placeholder"
    )


def test_sms_is_disabled_by_default():
    from app.compliance import sms_status

    status = sms_status(Settings())
    assert status["provider"] == "disabled"
    assert status["enabled"] is False
    assert "未配置" in status["message"]


def test_merchant_notification_is_unconfigured_by_default():
    status = notification_status(Settings())
    assert status["configured"] is False
    assert status["real_delivery_enabled"] is False
    assert "未配置" in status["message"]


def test_merchant_webhook_requires_url():
    unconfigured = notification_status(Settings(merchant_notification_provider="webhook"))
    configured = notification_status(
        Settings(
            merchant_notification_provider="webhook",
            merchant_notification_webhook_url="https://example.invalid/hook",
        )
    )
    assert unconfigured["configured"] is False
    assert configured["configured"] is True
    assert configured["real_delivery_enabled"] is True


def test_compliance_status_route_is_public(client):
    response = client.get("/store/compliance/status")
    assert response.status_code == 200
    body = response.json()
    assert body["official_open"] is False
    assert body["sms"]["enabled"] is False
    assert len(body["documents"]) == len(DOCUMENT_SLUGS)


def test_checkout_without_consent_fails_before_database(client):
    response = client.post(
        "/store/checkout",
        json={"cart_id": "00000000-0000-0000-0000-000000000001"},
    )
    assert response.status_code == 400
    assert "隐私政策" in response.json()["detail"]
