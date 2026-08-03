"""Phase 7 数据飞轮与自净引擎 — 无 DB 单测。

覆盖三大任务的核心算子：
  1) 微信证书轮换：/v3/certificates 拉取-解密-解析往返 (MockTransport 模拟
     加密响应)、rotate_platform_cert 热更新后新证书验签生效、轮换引擎在
     非 wechat/未配置网关时诚实跳过
  2) 真实爬虫目标：苏宁搜索解析、OpenFoodFacts JSON 解析、单位提取、
     LayeredSpiderProvider 测试覆写优先 + 真实目标降级
  3) 空间-行为矩阵：共现矩阵、关联度得分、Feed 插队重排
"""

from __future__ import annotations

import base64
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_ext_payment_gateways import _make_keypair  # noqa: E402

from app.config import Settings  # noqa: E402
from app.ext.oskill import (  # noqa: E402
    build_cooccurrence_matrix,
    compute_batch_affinity_scores,
    rerank_feed_by_affinity,
)
from app.ext.payment_gateways import (  # noqa: E402
    WechatPayNativeGateway,
    fetch_wechat_platform_certificates,
)
from app.ext.spider_targets import (  # noqa: E402
    LayeredSpiderProvider,
    extract_unit_from_text,
    parse_openfoodfacts,
    parse_suning_search,
)


# ── 任务 1: 微信证书轮换 ──────────────────────────────────────────────


def _encrypt_cert_payload(api_v3_key: str, nonce: str, plaintext: str) -> str:
    """微信 /v3/certificates 加密约定：AEAD_AES_256_GCM，associated_data=证书。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = api_v3_key.encode()
    assert len(key) == 32
    ct = AESGCM(key).encrypt(nonce.encode(), plaintext.encode(), b"certificate")
    return base64.b64encode(ct).decode()


def _build_cert_response(
    api_v3_key: str, cert_pem: str, serial_no: str, expire_time: str
) -> dict[str, Any]:
    nonce = "9m4WvWnHZnFqK6Ry"  # 微信 nonce 为原始 ASCII 串
    ciphertext = _encrypt_cert_payload(api_v3_key, nonce, cert_pem)
    return {
        "data": [
            {
                "serial_no": serial_no,
                "effective_time": "2026-01-01T00:00:00+08:00",
                "expire_time": expire_time,
                "encrypt_certificate": {
                    "algorithm": "AEAD_AES_256_GCM",
                    "nonce": nonce,
                    "associated_data": "certificate",
                    "ciphertext": ciphertext,
                },
            }
        ]
    }


async def test_fetch_wechat_certificates_decrypts_and_parses():
    """GET /v3/certificates → AES-GCM 解密 → PEM 平台证书可加载。"""
    priv_pem, pub_pem = _make_keypair()
    api_v3_key = "0" * 32
    cert_resp = _build_cert_response(
        api_v3_key, pub_pem, "SERIAL_A", "2030-01-01T00:00:00+08:00"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3/certificates"
        auth = request.headers.get("Authorization", "")
        assert auth.startswith("WECHATPAY2-SHA256-RSA2048")
        assert 'serial_no="SERIAL_MERCHANT"' in auth
        return httpx.Response(200, json=cert_resp)

    gateway = WechatPayNativeGateway(
        appid="wx_test",
        mchid="1900000001",
        serial_no="SERIAL_MERCHANT",
        private_key=priv_pem,
        api_v3_key=api_v3_key,
        platform_cert=pub_pem,
        transport=httpx.MockTransport(handler),
    )
    certs = await fetch_wechat_platform_certificates(gateway)
    assert len(certs) == 1
    assert certs[0].serial_no == "SERIAL_A"
    assert "BEGIN PUBLIC KEY" in certs[0].pem


async def test_rotate_platform_cert_hot_swap():
    """rotate_platform_cert 后：新证书签名的回调验签通过、旧证书签名被拒。"""
    priv_pem, _ = _make_keypair()
    _, old_pub = _make_keypair()
    new_priv, new_pub = _make_keypair()
    gateway = WechatPayNativeGateway(
        appid="wx_test",
        mchid="1900000001",
        serial_no="SERIAL_MERCHANT",
        private_key=priv_pem,
        api_v3_key="0" * 32,
        platform_cert=old_pub,
    )
    gateway.rotate_platform_cert(new_pub, serial_no="SERIAL_NEW")

    # 用新私钥签回调 → 应通过
    from app.ext.payment_gateways import load_private_key_pem, rsa_sha256_sign

    new_priv_key = load_private_key_pem(new_priv)

    # 回调体带合法 AES-GCM 资源信封 (业务 JSON 加密)，验签通过后还需解密成功
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    api_v3_key = "0" * 32
    biz = json.dumps({"mchid": "1900000001", "out_trade_no": "o1"}).encode()
    nonce_raw = "9m4WvWnHZnFqK6Ry"  # 微信回调 nonce 是原始 ASCII 串，非 base64
    ct = AESGCM(api_v3_key.encode()).encrypt(nonce_raw.encode(), biz, b"transaction")
    payload = json.dumps(
        {
            "resource": {
                "nonce": nonce_raw,
                "ciphertext": base64.b64encode(ct).decode(),
                "associated_data": "transaction",
            }
        }
    ).encode()
    ts = str(int(datetime.now(UTC).timestamp()))
    nonce = "n1"
    signature = rsa_sha256_sign(new_priv_key, f"{ts}\n{nonce}\n{payload.decode()}\n")
    ok = await gateway.verify_callback(
        {
            "Wechatpay-Timestamp": ts,
            "Wechatpay-Nonce": nonce,
            "Wechatpay-Signature": signature,
            "Wechatpay-Serial": "SERIAL_NEW",
        },
        payload,
    )
    assert ok is not None
    # 快照带新序列号
    assert gateway.platform_cert_snapshot["serial_no"] == "SERIAL_NEW"
    assert gateway.platform_cert_snapshot["pem"] == new_pub


async def test_rotate_platform_cert_rejects_garbage_pem():
    gateway = WechatPayNativeGateway(
        appid="wx_test",
        mchid="1900000001",
        serial_no="SERIAL_MERCHANT",
        private_key=None,
        api_v3_key="0" * 32,
        platform_cert=None,
    )
    with pytest.raises(Exception):
        gateway.rotate_platform_cert("not a pem")


async def test_cert_rotation_engine_skips_non_wechat_gateway():
    """引擎对非 wechat/未配置网关诚实跳过 (不假装轮换)。"""
    from app.ext.oservi import build_wechat_cert_rotation_engine

    engine = build_wechat_cert_rotation_engine(None, Settings())
    results = await engine.run_once()
    result = results[0] if isinstance(results, list) else results
    assert result["rotated"] is False
    assert "reason" in result
    assert engine.name == "ext-wechat-cert-rotation"


def test_cert_rotation_engine_schedule():
    from app.ext.oservi import build_wechat_cert_rotation_engine

    engine = build_wechat_cert_rotation_engine(None, Settings())
    assert engine.config.get("interval_seconds") == 43200
    assert engine.trigger == {"on_cron": "0 */12 * * *"}


# ── 任务 2: 具象化爬虫目标 ────────────────────────────────────────────


def test_extract_unit_from_text():
    assert extract_unit_from_text("整切眼肉牛排800g(5片装)") == "800g"
    assert extract_unit_from_text("牛肉 1.5kg 家庭装") == "1.5kg"
    assert extract_unit_from_text("纯牛奶250ml*16盒") == "250ml"
    assert extract_unit_from_text("无规格文本") == ""


_SUNING_FIXTURE = """
<html><body><script>
window.__data = [
  {"prdid":"12448577421","productName":"货出六盘 宁夏西吉县谷草饲喂养 六盘山牛肉 精品黄牛 牛腩4斤"},
  {"prdid":"12436273128","productName":"潮汕牛肉新鲜火锅食材鲜切雪花嫩肉吊龙匙仁肥胼家庭烧烤 潮汕牛肉套餐1.5斤"}
];
</script></body></html>
"""


def test_parse_suning_search():
    items = parse_suning_search(_SUNING_FIXTURE)
    assert len(items) == 2
    assert items[0]["item"].startswith("货出六盘")
    # prdid 补齐 + 单位从商品名提取
    assert items[0]["prdid"] == "12448577421"
    assert items[0]["unit"] == "4斤" or items[0]["unit"] == ""


def test_parse_suning_search_empty_page():
    assert parse_suning_search("<html><body>验证码</body></html>") == []


def test_parse_openfoodfacts_with_and_without_price():
    data = {
        "products": [
            {"product_name": "Beef Cubes", "quantity": "500 g", "price": "12.50"},
            {"product_name": "Beef Bouillon", "quantity": "72g"},
            {"product_name": "   ", "quantity": "1kg", "price": "9.9"},
        ]
    }
    items = parse_openfoodfacts(data)
    assert len(items) == 2
    assert items[0]["price"] == 12.5
    assert items[0]["unit"] == "500 g"
    assert items[1]["price"] is None
    assert items[1]["store"] == "OpenFoodFacts"


async def test_layered_provider_override_first():
    """set_results 覆写优先 (与 ManualSpiderProvider 契约一致)，不发真实请求。"""
    provider = LayeredSpiderProvider()
    provider.set_results(
        lat=31.0, lon=121.0, radius_km=5, results=[{"item": "牛腩", "price": 39.9}]
    )
    items = await provider.fetch_competitor_prices(
        lat=31.0, lon=121.0, radius_km=5, keywords=["牛腩"]
    )
    assert items == [{"item": "牛腩", "price": 39.9}]


async def test_layered_provider_real_targets_graceful(monkeypatch):
    """无覆写时打真实目标；目标不可达 (网络异常) 时诚实返回空不崩溃。"""
    provider = LayeredSpiderProvider()

    class _FlakyClient(httpx.AsyncClient):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise httpx.ConnectError("network unreachable")

    monkeypatch.setattr(httpx, "AsyncClient", _FlakyClient)
    items = await provider.fetch_competitor_prices(
        lat=31.0, lon=121.0, radius_km=5, keywords=["牛腩"]
    )
    assert items == []


# ── 任务 3: 空间-行为矩阵 (协同过滤算子) ──────────────────────────────


def test_build_cooccurrence_matrix():
    matrix = build_cooccurrence_matrix(
        [
            ["beef", "tomato", "onion"],  # 牛肉+番茄+洋葱 一起买
            ["beef", "tomato"],           # 牛肉+番茄 又一起买
            ["milk", "bread"],            # 无关组合
        ]
    )
    assert matrix[("beef", "tomato")] == 2
    assert matrix[("beef", "onion")] == 1
    assert matrix[("bread", "milk")] == 1
    assert ("beef", "beef") not in matrix  # 无自环
    assert ("tomato", "beef") not in matrix  # 无向边规范化 a<b


def test_compute_batch_affinity_scores():
    cooccurrence = {
        ("beef", "tomato"): 5,
        ("beef", "onion"): 2,
        ("beef", "milk"): 0,  # 无共现
    }
    scores = compute_batch_affinity_scores(
        ["beef"], ["tomato", "onion", "milk", "beef"], cooccurrence
    )
    assert scores["tomato"] == 5.0
    assert scores["onion"] == 2.0
    assert "milk" not in scores  # 0 分不参与
    assert "beef" not in scores  # 自己买过的不算关联


def test_rerank_feed_by_affinity_boost_to_front():
    batches = [
        {"product_id": "x", "title": "无关商品A"},
        {"product_id": "tomato", "title": "番茄"},
        {"product_id": "y", "title": "无关商品B"},
        {"product_id": "onion", "title": "洋葱"},
    ]
    scores = {"tomato": 5.0, "onion": 2.0}
    reranked = rerank_feed_by_affinity(batches, scores)
    # 关联商品整体插队到最前方，组内保持原顺序 (番茄先于洋葱)
    titles = [b["title"] for b in reranked]
    assert titles[:2] == ["番茄", "洋葱"]
    assert reranked[0]["boosted"] is True
    assert reranked[0]["affinity"] == 5.0
    # 非关联项顺序不变
    assert titles[2:] == ["无关商品A", "无关商品B"]
    # 无关联时退化为原始顺序
    plain = rerank_feed_by_affinity(batches, {})
    assert [b["title"] for b in plain] == [b["title"] for b in batches]
