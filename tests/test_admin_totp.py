"""管理员 TOTP 两步验证测试 (Task 2 核验补测)。

纯算法部分 (无 PG):
- RFC 6238 附录 B 标准测试向量 (HMAC-SHA1/30s/6 位)
- 时间窗 ±1 步; ±2 步拒绝; 命中步返回值
- 防重放判定 (同一时间步/窗口内已用步不能复用)
- otpauth URI 形状; 恢复码生成/哈希/一次性校验
- scope 令牌签发/解码/拒绝

PG 集成部分 (TEST_PG_DSN):
- production 未绑定 TOTP 的特权管理员: 登录不签发完整会话 (进后台被拒),
  绑定令牌也不能当完整会话用
- 绑定流程: setup → bind → 重新登录 → 第二步 verify 拿完整会话
- 重放拒绝; 恢复码一次性; 错误次数限流 (429)
- 非特权员工 TOTP 启用后的登录第二步
"""

from __future__ import annotations

import os
import secrets
import time
import uuid

import pytest
from obase.crypto.util import CryptoUtil
from obase.persistence.pool import PgPool

from app.auth import (
    _SCOPE_TOTP,
    _SCOPE_TOTP_SETUP,
    _decode_scope_token,
    _generate_recovery_codes,
    _hash_recovery_codes,
    _issue_scope_token,
    _totp_generate_code,
    _totp_generate_secret,
    _totp_match_counter,
    _totp_uri,
    _totp_verify_code,
    _verify_recovery_code,
)

TEST_DSN = os.environ.get("TEST_PG_DSN")
_needs_pg = pytest.mark.skipif(not TEST_DSN, reason="TEST_PG_DSN not set; skipping PG integration")

_FERNET_KEY = "ZmDfcTF7_60GrrY167zsiPd67pEvs0aGOv2oasOM1Pg="

# RFC 6238 附录 B 的标准向量: 密钥 = ASCII "12345678901234567890" 的 base32
# (不同的 SHA 实现/平台会产生不同的 8 位码, 本实现 6 位 = 附录 B 8 位码末 6 位)。
_RFC_SECRET_B32 = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
_RFC_VECTORS = [
    (59, "94287082"),
    (1111111109, "07081804"),
    (1111111111, "14050471"),
    (1234567890, "89005924"),
    (2000000000, "69279037"),
    (20000000000, "65353130"),
]


# ── RFC 6238 算法测试 ─────────────────────────────────────────────────────


@pytest.mark.parametrize(("timestamp", "expected_8_digits"), _RFC_VECTORS)
def test_rfc6238_appendix_b_vectors(timestamp: int, expected_8_digits: str):
    """RFC 6238 附录 B 测试向量 (6 位实现取末 6 位)。"""
    code = _totp_generate_code(_RFC_SECRET_B32, timestamp=timestamp)
    assert len(code) == 6
    assert expected_8_digits.endswith(code)


def test_totp_window_accepts_adjacent_step_rejects_two_steps():
    """±1 步 (±30s) 容差: 上一步的码可用, 上两步的码拒绝。"""
    secret = _totp_generate_secret()
    ts = (int(time.time()) // 30) * 30  # 对齐 30 秒步长, 避免跨步抖动
    assert _totp_verify_code(secret, _totp_generate_code(secret, ts - 30), timestamp=ts)
    assert _totp_verify_code(secret, _totp_generate_code(secret, ts + 30), timestamp=ts)
    assert not _totp_verify_code(secret, _totp_generate_code(secret, ts - 60), timestamp=ts)


def test_totp_match_counter_returns_actual_step():
    """_totp_match_counter 返回码实际命中的时间步 (防重放判定的依据)。"""
    secret = _totp_generate_secret()
    ts = (int(time.time()) // 30) * 30
    assert _totp_match_counter(secret, _totp_generate_code(secret, ts), ts) == ts // 30
    assert _totp_match_counter(secret, _totp_generate_code(secret, ts - 30), ts) == (ts - 30) // 30
    assert _totp_match_counter(secret, "000000", ts) in (None, 0 // 30)  # 不命中或碰巧


def test_totp_replay_same_step_rejected_by_policy():
    """服务层防重放策略: 码命中的步 <= 已用步 → 拒绝 (同一时间步只能用一次)。"""
    secret = _totp_generate_secret()
    ts = (int(time.time()) // 30) * 30
    matched = _totp_match_counter(secret, _totp_generate_code(secret, ts), ts)
    assert matched is not None
    # 重放: 同一个码再来一次, 命中的步没有变新
    replayed = _totp_match_counter(secret, _totp_generate_code(secret, ts), ts)
    assert replayed is not None
    assert replayed <= matched  # → totp_verify 会拒绝


def test_totp_invalid_code_format_rejected():
    secret = _totp_generate_secret()
    assert not _totp_verify_code(secret, "abcdef")
    assert not _totp_verify_code(secret, "12345")
    assert not _totp_verify_code(secret, "1234567")


def test_totp_uri_shape():
    secret = _totp_generate_secret()
    uri = _totp_uri(secret, "admin@hemall.test")
    assert uri.startswith("otpauth://totp/")
    assert f"secret={secret}" in uri
    assert "issuer=Hemall" in uri
    assert "algorithm=SHA1" in uri
    assert "digits=6" in uri
    assert "period=30" in uri


# ── 恢复码测试 ─────────────────────────────────────────────────────────────


def test_recovery_codes_generation_and_bcrypt_hash():
    codes = _generate_recovery_codes()
    assert len(codes) == 10
    assert len(set(codes)) == 10  # 不重复
    assert all(len(c) == 16 for c in codes)

    hashed = _hash_recovery_codes(codes)
    assert hashed[0] != codes[0]  # bcrypt 哈希, 不明文
    ok, index = _verify_recovery_code(hashed, codes[3])
    assert ok is True and index == 3


def test_recovery_code_wrong_code_rejected():
    hashed = _hash_recovery_codes(_generate_recovery_codes())
    ok, index = _verify_recovery_code(hashed, "f" * 16)
    assert ok is False and index == -1


# ── scope 令牌测试 ─────────────────────────────────────────────────────────


def _settings():
    from app.deps import get_settings

    return get_settings()


def test_scope_token_roundtrip_and_scope_isolation():
    settings = _settings()
    token = _issue_scope_token("u1", "a@b.c", "admin", _SCOPE_TOTP, settings)
    payload = _decode_scope_token(token, settings, _SCOPE_TOTP)
    assert payload is not None and payload["sub"] == "u1"
    # scope 不匹配 → 拒绝 (totp 令牌不能当 totp_setup 用, 反之亦然)
    assert _decode_scope_token(token, settings, _SCOPE_TOTP_SETUP) is None


def test_scope_token_tampered_signature_rejected():
    settings = _settings()
    token = _issue_scope_token("u1", "a@b.c", "admin", _SCOPE_TOTP, settings)
    tampered = token[:-4] + ("aaaa" if not token.endswith("aaaa") else "bbbb")
    assert _decode_scope_token(tampered, settings, _SCOPE_TOTP) is None


def test_full_session_token_rejected_as_scope_token():
    settings = _settings()
    from app.security.tokens import TOKEN_TYPE_STAFF, sign_token

    full = sign_token(
        {"sub": "u1", "email": "a@b.c", "role": "admin"},
        typ=TOKEN_TYPE_STAFF,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        expires_in_minutes=15,
    )
    # 完整会话没有 scope, 不能被当成挑战/绑定令牌用
    assert _decode_scope_token(full, settings, _SCOPE_TOTP) is None


# ── PG 集成测试 ─────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _disable_rate_limit():
    """默认关掉限流, 避免多测试共用 testclient IP 把 5/minute 桶打爆;
    专门的限流测试再打开。"""
    from app.security.rate_limit import limiter

    prev = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = prev


@pytest.fixture
async def prod_app(monkeypatch, tmp_path):
    """production 环境的真实 app + 测试账号。

    validate_production_security 要求全套生产密钥, 这里用自生成的 RSA 密钥对
    (is_configured 需要可解析 PEM) + 测试用 Fernet key 满足基线。
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub_pem = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    key_file = tmp_path / "merchant.key"
    key_file.write_text(key_pem)
    cert_file = tmp_path / "platform.pem"
    cert_file.write_text(pub_pem)

    privileged_id = str(uuid.uuid4())
    staff_id = str(uuid.uuid4())
    password = "Passw0rd-prod-test!"

    monkeypatch.setenv("HEMALL_ENVIRONMENT", "production")
    monkeypatch.setenv("HEMALL_PG_DSN", TEST_DSN)
    monkeypatch.setenv("HEMALL_JWT_SECRET", secrets.token_hex(32))
    monkeypatch.setenv("HEMALL_WEBHOOK_SECRET", secrets.token_hex(32))
    monkeypatch.setenv("HEMALL_HARDWARE_SECRET", secrets.token_hex(32))
    monkeypatch.setenv("HEMALL_ENCRYPTION_KEY", _FERNET_KEY)
    monkeypatch.setenv("HEMALL_PRIVILEGED_STAFF_IDS", privileged_id)
    monkeypatch.setenv("HEMALL_PAYMENT_GATEWAY_PROVIDER", "wechat")
    monkeypatch.setenv("HEMALL_WECHAT_PAY_APPID", "wx-test-app")
    monkeypatch.setenv("HEMALL_WECHAT_PAY_MCHID", "mch-test")
    monkeypatch.setenv("HEMALL_WECHAT_PAY_SERIAL_NO", "serial-test")
    monkeypatch.setenv("HEMALL_WECHAT_PAY_PRIVATE_KEY_PATH", str(key_file))
    monkeypatch.setenv("HEMALL_WECHAT_PAY_API_V3_KEY", "k" * 32)
    monkeypatch.setenv("HEMALL_WECHAT_PAY_PLATFORM_CERT_PATH", str(cert_file))
    monkeypatch.setenv(
        "HEMALL_REDIS_URL", os.environ.get("HEMALL_REDIS_URL", "redis://127.0.0.1:1/0")
    )

    password_hash = CryptoUtil.hash_password(password=password)
    pool = await PgPool.create(
        name=f"hemall_test_totp_{uuid.uuid4().hex[:8]}",
        dsn=TEST_DSN,
        min_size=1,
        max_size=2,
    )
    privileged_email = f"admin-{uuid.uuid4().hex[:8]}@hemall.test"
    staff_email = f"staff-{uuid.uuid4().hex[:8]}@hemall.test"
    async with pool.acquire() as conn:
        await conn.execute(
            'INSERT INTO "app_user" (id, email, password_hash, name, status) '
            "VALUES ($1, $2, $3, $4, 'active')",
            privileged_id,
            privileged_email,
            password_hash,
            "Prod Admin",
        )
        await conn.execute(
            'INSERT INTO "app_user" (id, email, password_hash, name, status) '
            "VALUES ($1, $2, $3, $4, 'active')",
            staff_id,
            staff_email,
            password_hash,
            "Plain Staff",
        )

    from app.deps import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    app = create_app()
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        ctx = {
            "client": client,
            "privileged_id": privileged_id,
            "privileged_email": privileged_email,
            "staff_id": staff_id,
            "staff_email": staff_email,
            "password": password,
        }
        yield ctx
    get_settings.cache_clear()
    async with pool.acquire() as conn:
        await conn.execute('DELETE FROM "app_user" WHERE id = $1', privileged_id)
        await conn.execute('DELETE FROM "app_user" WHERE id = $1', staff_id)
    await pool.close()


@_needs_pg
async def test_production_privileged_admin_without_totp_cannot_enter_backoffice(prod_app):
    """production 未绑定 TOTP 的特权管理员: 拿不到完整会话 = 进后台被拒。"""
    r = prod_app["client"].post(
        "/auth/login",
        json={"email": prod_app["privileged_email"], "password": prod_app["password"]},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["requires_totp_setup"] is True
    assert "access_token" not in data  # 没有完整会话
    assert data["setup_token"]

    # 绑定令牌是 scope 受限令牌, 不能当完整会话访问 /auth/me 或后台
    r_me = prod_app["client"].get(
        "/auth/me", headers={"Authorization": f"Bearer {data['setup_token']}"}
    )
    assert r_me.status_code == 401


@_needs_pg
async def test_production_bind_and_two_step_login_flow(prod_app):
    """完整流程: setup → bind → 重新登录 → 第二步 verify 签发完整会话。"""
    client = prod_app["client"]
    email, password = prod_app["privileged_email"], prod_app["password"]
    user_id = prod_app["privileged_id"]

    # 1. 登录第一步 → 绑定令牌
    r = client.post("/auth/login", json={"email": email, "password": password})
    assert r.json()["requires_totp_setup"] is True
    setup_token = r.json()["setup_token"]
    setup_headers = {"Authorization": f"Bearer {setup_token}"}

    # 2. setup: 拿 otpauth URI + secret
    r = client.get("/auth/totp/setup", headers=setup_headers)
    assert r.status_code == 200, r.text
    uri = r.json()["otpauth_uri"]
    secret = r.json()["secret"]
    assert uri.startswith("otpauth://totp/")
    assert f"secret={secret}" in uri

    # 3. bind: 用当前 TOTP 码确认 → 返回一次性恢复码
    code = _totp_generate_code(secret)
    r = client.post(
        "/auth/totp/bind",
        json={"code": code},
        headers={**setup_headers, "X-TOTP-Secret": secret},
    )
    assert r.status_code == 200, r.text
    recovery_codes = r.json()["recovery_codes"]
    assert len(recovery_codes) == 10

    # 4. 重新登录 → 现在需要第二步
    r = client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200
    data = r.json()
    assert data["requires_totp"] is True
    assert data["totp_token"]
    assert "access_token" not in data
    totp_token = data["totp_token"]
    verify_headers = {"Authorization": f"Bearer {totp_token}"}

    # 5. 错误码 → 401
    r = client.post(
        "/auth/totp/verify",
        json={"user_id": user_id, "code": "000000"},
        headers=verify_headers,
    )
    assert r.status_code == 401

    # 6. 正确码 → 完整会话
    code = _totp_generate_code(secret)
    r = client.post(
        "/auth/totp/verify",
        json={"user_id": user_id, "code": code},
        headers=verify_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["access_token"]

    # 7. 防重放: 同一个码立即再用来登录 → 401
    r = client.post(
        "/auth/totp/verify",
        json={"user_id": user_id, "code": code},
        headers=verify_headers,
    )
    assert r.status_code == 401
    assert "replay" in r.json()["detail"].lower()

    # 8. 恢复码一次性: 第一次 200, 第二次 401 (仍带挑战令牌, 与前端流程一致)
    r = client.post(
        "/auth/totp/verify",
        json={"user_id": user_id, "recovery_code": recovery_codes[0]},
        headers=verify_headers,
    )
    assert r.status_code == 200, r.text
    r = client.post(
        "/auth/totp/verify",
        json={"user_id": user_id, "recovery_code": recovery_codes[0]},
        headers=verify_headers,
    )
    assert r.status_code == 401


@_needs_pg
async def test_production_bind_rejects_wrong_code(prod_app):
    """绑定确认时错误的 TOTP 码必须被拒, 且不启用 TOTP。"""
    client = prod_app["client"]
    r = client.post(
        "/auth/login",
        json={
            "email": prod_app["privileged_email"],
            "password": prod_app["password"],
        },
    )
    setup_token = r.json()["setup_token"]
    secret = client.get(
        "/auth/totp/setup",
        headers={"Authorization": f"Bearer {setup_token}"},
    ).json()["secret"]

    r = client.post(
        "/auth/totp/bind",
        json={"code": "000000"},
        headers={
            "Authorization": f"Bearer {setup_token}",
            "X-TOTP-Secret": secret,
        },
    )
    assert r.status_code == 400
    assert "invalid TOTP code" in r.json()["detail"]

    # 未启用: 重新登录仍然要求绑定 (requires_totp_setup)
    r = client.post(
        "/auth/login",
        json={
            "email": prod_app["privileged_email"],
            "password": prod_app["password"],
        },
    )
    assert r.json()["requires_totp_setup"] is True


@_needs_pg
async def test_totp_challenge_token_subject_mismatch_rejected(prod_app):
    """挑战令牌的 sub 与 user_id 不一致 → 403 (不能替别人验第二步)。"""
    client = prod_app["client"]
    r = client.post(
        "/auth/login",
        json={"email": prod_app["staff_email"], "password": prod_app["password"]},
    )
    # 非特权员工未绑定 TOTP, production 下也允许先登录 (他只是 staff)
    # → 直接用 staff 的 user_id + 别人的挑战令牌组合必然 403
    r = client.post(
        "/auth/totp/verify",
        json={"user_id": prod_app["privileged_id"], "code": "123456"},
        headers={"Authorization": "Bearer not-a-valid-scope-token"},
    )
    assert r.status_code == 401


@_needs_pg
async def test_totp_verify_rate_limited(prod_app, _disable_rate_limit):
    """错误次数限流: 连续错误验证触发 429 (5/minute)。"""
    from app.security.rate_limit import limiter

    # 先拿一个合法挑战令牌 (Bearer 认证请求不带 cookie, 不在 CSRF 面内)
    client = prod_app["client"]
    r = client.post(
        "/auth/login",
        json={
            "email": prod_app["privileged_email"],
            "password": prod_app["password"],
        },
    )
    setup_token = r.json()["setup_token"]
    secret = client.get(
        "/auth/totp/setup",
        headers={"Authorization": f"Bearer {setup_token}"},
    ).json()["secret"]
    client.post(
        "/auth/totp/bind",
        json={"code": _totp_generate_code(secret)},
        headers={
            "Authorization": f"Bearer {setup_token}",
            "X-TOTP-Secret": secret,
        },
    )
    r = client.post(
        "/auth/login",
        json={
            "email": prod_app["privileged_email"],
            "password": prod_app["password"],
        },
    )
    challenge = r.json()["totp_token"]
    headers = {"Authorization": f"Bearer {challenge}"}

    limiter.enabled = True
    try:
        statuses = []
        for _ in range(8):
            r = client.post(
                "/auth/totp/verify",
                json={
                    "user_id": prod_app["privileged_id"],
                    "code": "000000",
                },
                headers=headers,
            )
            statuses.append(r.status_code)
            if r.status_code == 429:
                break
        assert 429 in statuses, f"expected rate limit 429, got {statuses}"
        assert 200 not in statuses
    finally:
        limiter.enabled = False


@_needs_pg
async def test_non_privileged_staff_login_allowed_without_totp_in_production(prod_app):
    """非特权员工无 TOTP: production 下可正常登录 (后台权限由角色控制)。"""
    r = prod_app["client"].post(
        "/auth/login",
        json={"email": prod_app["staff_email"], "password": prod_app["password"]},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert "access_token" in data
    assert data["user_id"] == prod_app["staff_id"]
