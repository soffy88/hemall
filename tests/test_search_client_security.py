from __future__ import annotations

import asyncio

from app.config import Settings
from app.search.client import ElasticsearchClient


def test_https_search_client_uses_basic_auth_and_ca_certificate(monkeypatch):
    captured = {}

    class FakeElasticsearch:
        def __init__(self, hosts, **kwargs):
            captured["hosts"] = hosts
            captured.update(kwargs)

        async def ping(self):
            return False

    monkeypatch.setattr("app.search.client.AsyncElasticsearch", FakeElasticsearch)
    settings = Settings(
        elasticsearch_url="https://search.internal:9200",
        elasticsearch_username="elastic",
        elasticsearch_password="test-password",
        elasticsearch_ca_cert="/certs/http_ca.crt",
    )
    client = ElasticsearchClient(settings)
    asyncio.run(client.initialize())
    assert captured["hosts"] == ["https://search.internal:9200"]
    assert captured["basic_auth"] == ("elastic", "test-password")
    assert captured["ca_certs"] == "/certs/http_ca.crt"


def test_production_https_search_requires_auth_and_ca():
    settings = Settings(
        environment="production",
        payment_gateway_provider="stripe",
        stripe_secret_key="sk_live_test",
        stripe_webhook_secret="whsec_test",
        elasticsearch_url="https://search.internal:9200",
    )
    try:
        settings.validate_production_security()
    except RuntimeError as exc:
        assert "Elasticsearch HTTPS 生产认证配置缺失" in str(exc)
    else:
        raise AssertionError("production HTTPS Elasticsearch accepted missing credentials/CA")
