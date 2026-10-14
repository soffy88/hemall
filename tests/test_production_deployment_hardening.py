from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_production_compose_does_not_publish_internal_services():
    config = yaml.safe_load((ROOT / "docker-compose.prod.yml").read_text())
    services = config["services"]
    for name in ("api", "postgres", "redis", "elasticsearch"):
        assert not services[name].get("ports"), f"{name} must not publish a host port"
    assert config["networks"]["data-network"]["internal"] is True
    assert "data-network" in services["api"]["networks"]
    for name in ("postgres", "redis", "elasticsearch"):
        assert services[name]["networks"] == ["data-network"]
    assert services["nginx"]["networks"] == ["hemall-network"]


def test_production_elasticsearch_uses_credentials_and_ca_validation():
    config = yaml.safe_load((ROOT / "docker-compose.prod.yml").read_text())
    api_env = config["services"]["api"]["environment"]
    assert any("HEMALL_ELASTICSEARCH_URL=https://elasticsearch:9200" in str(v) for v in api_env)
    assert any("HEMALL_ELASTICSEARCH_USERNAME=elastic" in str(v) for v in api_env)
    assert any("HEMALL_ELASTICSEARCH_PASSWORD=" in str(v) for v in api_env)
    assert any("HEMALL_ELASTICSEARCH_CA_CERT=" in str(v) for v in api_env)
    es_env = config["services"]["elasticsearch"]["environment"]
    assert not any("xpack.security.enabled=false" in str(v) for v in es_env)
    assert "elasticsearch_certs:/usr/share/elasticsearch/config/certs" in config["services"]["elasticsearch"]["volumes"]


def test_production_compose_requires_admin_and_real_payment_configuration():
    api_env = yaml.safe_load((ROOT / "docker-compose.prod.yml").read_text())["services"]["api"]["environment"]
    joined = "\n".join(map(str, api_env))
    assert "HEMALL_PRIVILEGED_STAFF_IDS=${HEMALL_PRIVILEGED_STAFF_IDS:?" in joined
    assert "HEMALL_PAYMENT_GATEWAY_PROVIDER=${HEMALL_PAYMENT_GATEWAY_PROVIDER:?" in joined
    assert "HEMALL_CORS_ORIGINS=${HEMALL_CORS_ORIGINS:?" in joined


def test_production_dockerfile_uses_frozen_lockfile():
    dockerfile = (ROOT / "Dockerfile.prod").read_text()
    assert "COPY pyproject.toml uv.lock README.md ./" in dockerfile
    assert "UV_PROJECT_ENVIRONMENT=/opt/venv" in dockerfile
    assert "uv sync --frozen --no-dev --extra observability --extra phase2" in dockerfile
    assert "uv pip install -e" not in dockerfile


def test_mall_compose_has_one_cors_setting_and_requires_real_provider_and_admins():
    text = (ROOT / "docker-compose.mall-sxueji.yml").read_text()
    parsed = yaml.safe_load(text)
    api_env = parsed["services"]["api"]["environment"]
    assert text.count("HEMALL_CORS_ORIGINS:") == 1
    assert "HEMALL_PRIVILEGED_STAFF_IDS" in api_env
    assert "HEMALL_PAYMENT_GATEWAY_PROVIDER" in api_env
    # 密钥现在通过 Docker secrets 管理，检查 _FILE 变量
    assert "HEMALL_STRIPE_SECRET_KEY_FILE" in api_env
    assert "HEMALL_WECHAT_PAY_PRIVATE_KEY_FILE" in api_env
    assert "HEMALL_JWT_SECRET_FILE" in api_env
    assert "ENCRYPTION_KEY_FILE" in api_env


def test_helm_requires_external_production_secret_and_rejects_insecure_embedded_search():
    values = yaml.safe_load((ROOT / "charts/hemal/values.yaml").read_text())
    assert values["secrets"]["existingSecret"] == ""
    secret_template = (ROOT / "charts/hemal/templates/secret.yaml").read_text()
    deployment_template = (ROOT / "charts/hemal/templates/deployment.yaml").read_text()
    assert "Set .Values.secrets.existingSecret" in secret_template
    assert "Bundled Elasticsearch is not configured with verified HTTPS authentication" in secret_template
    assert ".Values.secrets.existingSecret" in deployment_template
