"""AI 助手模块安全回归（本轮新增审查）。

审计覆盖三条线：
  1. **越权 / 冒用**：会话归属与调用方身份绑定（``user_id`` 客户端自报、
     ``GET/DELETE /ai/sessions`` 不分归属）。
  2. **成本**：LLM 端点逐次烧钱，必须有按主体的配额上限与入参长度上限，
     否则任意员工令牌可把账单打爆。
  3. **密钥 / 错误泄露**：异常原文不得回传给调用方（SDK 异常里可能带
     请求 URL 与鉴权头）。
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.deps import get_settings
from app.main import app
from app.security.tokens import TOKEN_TYPE_STAFF, sign_token

# ── 1. 会话归属 / 冒用 ───────────────────────────────────────────────────


def _staff_token(user_id: str, role: str = "staff") -> str:
    settings = get_settings()
    return sign_token(
        {"sub": user_id, "email": f"{user_id}@x.test", "role": role},
        typ=TOKEN_TYPE_STAFF,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        expires_in_minutes=10,
    )


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


def _service():
    from app.ai_assistant.service import AIAssistantService

    return AIAssistantService.__new__(AIAssistantService)


def _conversation(session_id="s1", user_id="cust-1"):
    from app.ai_assistant.models import Conversation

    return Conversation(session_id=session_id, user_id=user_id)


def test_session_records_the_authenticated_owner_not_the_client_claimed_user():
    svc = _service()
    svc._sessions = {}
    session = svc.get_or_create_session(None, owner_id="staff-alice", claimed_user_id="victim")
    assert session.owner_id == "staff-alice", "会话归属必须来自已认证主体"
    assert session.user_id != "victim" or session.owner_id == "staff-alice"


def test_get_or_create_session_rejects_foreign_session_id():
    svc = _service()
    svc._sessions = {"s1": _conversation("s1", "cust-1")}
    svc._sessions["s1"].owner_id = "staff-alice"
    with pytest.raises(PermissionError):
        svc.get_or_create_session("s1", owner_id="staff-bob")


def test_get_or_create_session_allows_own_session():
    svc = _service()
    svc._sessions = {"s1": _conversation("s1", "cust-1")}
    svc._sessions["s1"].owner_id = "staff-alice"
    session = svc.get_or_create_session("s1", owner_id="staff-alice")
    assert session.session_id == "s1"


def test_list_sessions_only_returns_the_callers_own_sessions():
    svc = _service()
    svc._sessions = {
        "a": _conversation("a", "cust-a"),
        "b": _conversation("b", "cust-b"),
    }
    svc._sessions["a"].owner_id = "staff-alice"
    svc._sessions["b"].owner_id = "staff-bob"
    visible = svc.sessions_for_owner("staff-alice")
    assert [s["session_id"] for s in visible] == ["a"]


def test_delete_session_refuses_foreign_sessions():
    svc = _service()
    svc._sessions = {"b": _conversation("b", "cust-b")}
    svc._sessions["b"].owner_id = "staff-bob"
    with pytest.raises(PermissionError):
        svc.delete_session_owned("b", owner_id="staff-alice")
    assert "b" in svc._sessions, "越权删除必须不生效"


# ── HTTP 层: 会话端点的归属 ──────────────────────────────────────────────


def test_ai_sessions_list_is_scoped_to_the_caller(client):
    r_alice = client.get(
        "/ai/sessions", headers={"Authorization": f"Bearer {_staff_token('staff-alice')}"}
    )
    assert r_alice.status_code != 401
    if r_alice.status_code == 200:
        assert r_alice.json()["data"] == [], "新实例不应凭空有别人的会话"


def test_ai_sessions_delete_rejects_foreign_session(client):
    r = client.delete(
        "/ai/sessions/definitely-not-mine",
        headers={"Authorization": f"Bearer {_staff_token('staff-alice')}"},
    )
    assert r.status_code == 404, f"实得 {r.status_code}"


def test_ai_routes_still_require_staff_token(client):
    """既有基线不能被放松。"""
    for method, path, body in [
        ("POST", "/ai/chat", {"message": "hi"}),
        ("POST", "/ai/product/ask", {"product_id": "p1", "question": "q"}),
        ("POST", "/ai/product/description", {"product_id": "p1"}),
        ("POST", "/ai/sentiment", None),
        ("GET", "/ai/sessions", None),
    ]:
        r = client.request(method, path, json=body)
        assert r.status_code == 401, f"{method} {path} 应 401"


# ── 2. 成本上限 ─────────────────────────────────────────────────────────


def test_quota_guard_blocks_after_the_per_minute_budget():
    from app.ai_assistant.quota import AIQuotaGuard

    guard = AIQuotaGuard(calls_per_minute=3, calls_per_hour=100)
    for _ in range(3):
        assert guard.consume("staff-alice") is True
    with pytest.raises(HTTPException) as exc:
        guard.consume("staff-alice")
    assert exc.value.status_code == 429
    # 另一个主体不受影响
    assert guard.consume("staff-bob") is True


def test_quota_guard_blocks_after_the_per_hour_budget():
    from app.ai_assistant.quota import AIQuotaGuard

    guard = AIQuotaGuard(calls_per_minute=1000, calls_per_hour=4)
    for _ in range(4):
        assert guard.consume("u1") is True
    with pytest.raises(HTTPException) as exc:
        guard.consume("u1")
    assert exc.value.status_code == 429


def test_quota_guard_key_is_per_principal_not_global():
    from app.ai_assistant.quota import AIQuotaGuard

    guard = AIQuotaGuard(calls_per_minute=1, calls_per_hour=10)
    assert guard.consume("a") is True
    assert guard.consume("b") is True, "配额必须按主体隔离，不能一刀切"
    with pytest.raises(HTTPException):
        guard.consume("a")


def test_quota_guard_rejects_empty_principal():
    from app.ai_assistant.quota import AIQuotaGuard

    guard = AIQuotaGuard(calls_per_minute=5, calls_per_hour=5)
    for bad in ("", "   ", None):
        with pytest.raises((HTTPException, ValueError)):
            guard.consume(bad)


def test_quota_limits_come_from_settings_and_have_safe_defaults():
    from app.config import Settings

    settings = Settings()
    assert settings.ai_quota_calls_per_minute > 0
    assert settings.ai_quota_calls_per_hour >= settings.ai_quota_calls_per_minute
    assert settings.ai_max_message_chars > 0


def test_oversized_prompt_is_rejected_before_calling_the_llm():
    from app.ai_assistant.quota import enforce_message_budget

    with pytest.raises(HTTPException) as exc:
        enforce_message_budget("x" * 5000, max_chars=1000)
    assert exc.value.status_code in (413, 422)


def test_message_budget_allows_normal_size():
    from app.ai_assistant.quota import enforce_message_budget

    assert enforce_message_budget("正常的一句话", max_chars=1000) == "正常的一句话"


def test_ai_chat_rejects_oversized_message(client):
    settings = get_settings()
    token = _staff_token("staff-alice")
    r = client.post(
        "/ai/chat",
        json={"message": "x" * (settings.ai_max_message_chars + 100)},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code in (413, 422), f"实得 {r.status_code}: {r.text[:200]}"


# ── 3. 密钥 / 错误泄露 ───────────────────────────────────────────────────


def test_llm_error_text_is_not_echoed_to_the_caller():
    """SDK 异常原文可能带请求 URL / 鉴权头，不许回传给调用方。"""
    from app.ai_assistant.llm_provider import OpenAIProvider

    provider = OpenAIProvider(api_key="sk-live-do-not-leak", model_name="gpt-4")

    async def boom(*a, **k):
        raise RuntimeError(
            "Connection to https://api.example.com/v1 failed; "
            "auth header: Bearer sk-live-do-not-leak"
        )

    provider._client = type("C", (), {"chat": type("Chat", (), {"completions": type("X", (), {"create": staticmethod(boom)})()})()})()
    provider._started = True

    from app.ai_assistant.llm_provider import LLMMessage

    out = asyncio.run(
        provider.chat_with_tools([LLMMessage(role="user", content="hi")], tools=[])
    )
    assert "sk-live-do-not-leak" not in out.content, "API key 泄漏到响应体"
    assert "https://api.example.com" not in out.content, "上游 URL 泄漏到响应体"
    assert out.finish_reason == "error"


def test_ai_api_key_is_never_echoed_in_config_repr():
    from app.ai_assistant.models import AIConfig

    config = AIConfig(api_key="sk-live-do-not-leak")
    rendered = f"{config!r} {config.model_dump()} {getattr(config, '__str__', lambda: '')()}"
    assert "sk-live-do-not-leak" not in rendered, "AIConfig 会把 api_key 打印出来"


def test_provider_repr_hides_the_api_key():
    from app.ai_assistant.llm_provider import OpenAIProvider

    provider = OpenAIProvider(api_key="sk-live-do-not-leak", model_name="gpt-4")
    assert "sk-live-do-not-leak" not in repr(provider)


def test_no_hardcoded_api_keys_in_ai_assistant_sources():
    """源码里不许出现看起来像真 key 的字面量。"""
    import pathlib
    import re

    from app import ai_assistant

    root = pathlib.Path(ai_assistant.__file__).parent
    pattern = re.compile(r"sk-[A-Za-z0-9]{16,}|sk_live_[A-Za-z0-9]{16,}")
    offenders = []
    for path in sorted(root.glob("*.py")):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.name}:{lineno}")
    assert offenders == [], f"AI 助手源码疑似硬编码 API key: {offenders}"


def test_config_default_api_key_is_empty():
    from app.config import Settings

    assert Settings().ai_api_key == "", "ai_api_key 不得有非空默认值"