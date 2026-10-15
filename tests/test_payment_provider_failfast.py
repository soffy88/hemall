"""支付 SDK 生产分支的 fail-fast（audit tests.md P2-2）。

背景: app/payments/{wechat,alipay}.py 里 create_order / handle_notify /
query_order / refund 的生产分支共 8 处 `NotImplementedError`，而沙箱分支一切
正常。生产配置一旦真的走到这些分支，结果是：

  - 微信回调 `handle_notify` 抛 NotImplementedError → **支付状态永不回写，
    订单永久卡在 pending**；
  - `POST /payments/refund` 必 500。

真正的微信/Stripe 收款走 `app/ext/payment_gateways.py`
（`HEMALL_PAYMENT_GATEWAY_PROVIDER` + 密钥装配），这两个 SDK 文件是遗留骨架。
本文件锁定两件事：

1. 选到"生产模式"（即配了 WECHAT_PAY_MCH_ID / ALIPAY_APP_ID）时**构造即失败**，
   不给"运行到一半才 500"的机会；
2. 8 处 `NotImplementedError` 逐条锁死，防止有人改成 `pass` / `return {}`
   变成假成功（假成功 = 回调被当成验签通过 = 订单被白嫖）。
"""

from __future__ import annotations

import pytest

from app.config import Settings

# ── 1. 启动期 fail-fast：选到生产模式就必须拒绝构造 ───────────────────────


def test_wechat_provider_refuses_to_construct_in_production_mode(monkeypatch):
    from app.payments.wechat import WeChatPayProvider

    monkeypatch.setenv("WECHAT_PAY_MCH_ID", "1900000001")
    with pytest.raises(NotImplementedError) as exc:
        WeChatPayProvider()
    assert "production" in str(exc.value).lower()


def test_alipay_provider_refuses_to_construct_in_production_mode(monkeypatch):
    from app.payments.alipay import AlipayProvider

    monkeypatch.setenv("ALIPAY_APP_ID", "2021000000000000")
    with pytest.raises(NotImplementedError) as exc:
        AlipayProvider()
    assert "production" in str(exc.value).lower()


def test_wechat_provider_still_constructs_in_sandbox(monkeypatch):
    from app.payments.wechat import WeChatPayProvider

    monkeypatch.setenv("WECHAT_PAY_MCH_ID", "")
    provider = WeChatPayProvider()
    assert provider._sandbox is True


def test_alipay_provider_still_constructs_in_sandbox(monkeypatch):
    from app.payments.alipay import AlipayProvider

    monkeypatch.setenv("ALIPAY_APP_ID", "")
    provider = AlipayProvider()
    assert provider._sandbox is True


# ── 2. Settings.validate_production_security 拒绝遗留 SDK 的生产开关 ────────


def _prod_settings(**overrides) -> Settings:
    base = {
        "environment": "production",
        "jwt_secret": "j" * 48,
        "webhook_secret": "w" * 48,
        "hardware_secret": "h" * 48,
        "pg_dsn": "postgresql://hemall:realpw@db:5432/hemall",
        "pg_sslmode": "require",  # 显式设置 require 以通过 sslmode 检查
        "cors_origins": ["https://mall.example.com"],
        "privileged_staff_ids": "u1",
        "payment_gateway_provider": "stripe",
        "stripe_secret_key": "sk_live_placeholder",
        "stripe_webhook_secret": "whsec_placeholder",
        "encryption_key": _fernet_key(),  # 有效的 Fernet key
    }
    base.update(overrides)
    return Settings(**base)


def test_production_rejects_legacy_wechat_sdk_env(monkeypatch):
    """生产下配了 WECHAT_PAY_MCH_ID 却没走真网关 = 选到了未实现的分支。"""
    monkeypatch.setenv("WECHAT_PAY_MCH_ID", "1900000001")
    monkeypatch.setenv("ENCRYPTION_KEY", _fernet_key())
    with pytest.raises(RuntimeError) as exc:
        _prod_settings().validate_production_security()
    assert "WECHAT_PAY_MCH_ID" in str(exc.value)


def test_production_rejects_legacy_alipay_sdk_env(monkeypatch):
    monkeypatch.setenv("ALIPAY_APP_ID", "2021000000000000")
    monkeypatch.setenv("ENCRYPTION_KEY", _fernet_key())
    with pytest.raises(RuntimeError) as exc:
        _prod_settings().validate_production_security()
    assert "ALIPAY_APP_ID" in str(exc.value)


def test_production_with_only_the_real_gateway_starts(monkeypatch):
    monkeypatch.delenv("WECHAT_PAY_MCH_ID", raising=False)
    monkeypatch.delenv("ALIPAY_APP_ID", raising=False)
    monkeypatch.setenv("ENCRYPTION_KEY", _fernet_key())
    settings = _prod_settings()
    assert settings.validate_production_security() is None, (
        "只配真网关的生产设置必须通过安全基线校验"
    )
    assert settings.payment_gateway_provider == "stripe"


def test_development_is_unaffected_by_legacy_env(monkeypatch):
    monkeypatch.setenv("WECHAT_PAY_MCH_ID", "1900000001")
    monkeypatch.setenv("ALIPAY_APP_ID", "2021000000000000")
    settings = Settings(environment="development")
    assert settings.validate_production_security() is None, (
        "开发环境不受生产基线的遗留 SDK 开关影响"
    )
    assert settings.environment == "development"


def _fernet_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode("ascii")


# ── 3. 8 处 NotImplementedError 逐条锁死 ──────────────────────────────────


def _forced_production_provider(cls, sandbox_attr="_sandbox"):
    """绕过构造期 fail-fast 造一个"生产模式"实例，用来锁方法级守卫。

    只有这样才能证明：即使有人日后把构造期的检查删掉，方法本身仍然不会
    假装成功。
    """
    provider = object.__new__(cls)
    provider._mch_id = "1900000001"
    provider._app_id = "2021000000000000"
    provider._api_key = "0" * 32
    provider._notify_url = "https://example.invalid/notify"
    provider._orders = {}
    provider._refunds = {}
    setattr(provider, sandbox_attr, False)
    return provider


@pytest.mark.asyncio
async def test_wechat_production_branches_all_raise():
    from app.payments.wechat import WeChatPayOrderRequest, WeChatPayProvider

    p = _forced_production_provider(WeChatPayProvider)
    with pytest.raises(NotImplementedError):
        await p.create_order(
            WeChatPayOrderRequest(out_trade_no="o1", description="d", total_amount=1)
        )
    with pytest.raises(NotImplementedError):
        await p.handle_notify({}, b"{}")
    with pytest.raises(NotImplementedError):
        await p.query_order("o1")
    with pytest.raises(NotImplementedError):
        await p.refund(out_trade_no="o1", out_refund_no="r1", refund_amount=1)


@pytest.mark.asyncio
async def test_alipay_production_branches_all_raise():
    from app.payments.alipay import AlipayOrderRequest, AlipayProvider

    p = _forced_production_provider(AlipayProvider)
    with pytest.raises(NotImplementedError):
        await p.create_order(
            AlipayOrderRequest(out_trade_no="o1", subject="s", total_amount="1.00")
        )
    with pytest.raises(NotImplementedError):
        await p.handle_notify({})
    with pytest.raises(NotImplementedError):
        await p.query_order("o1")
    with pytest.raises(NotImplementedError):
        await p.refund(out_trade_no="o1", refund_amount="1.00")


def test_every_not_implemented_error_in_payments_still_raises():
    """源码级兜底: payments 包里每个 raise NotImplementedError 都不能被改成
    `pass` / `return {}`（那等于"回调验签通过 = 白嫖订单"）。"""
    import ast
    import pathlib

    from app import payments

    root = pathlib.Path(payments.__file__).parent
    count = 0
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
                func = node.exc.func
                name = getattr(func, "id", None) or getattr(func, "attr", None)
                if name == "NotImplementedError":
                    count += 1
                    assert node.exc.args and all(
                        isinstance(a, ast.Constant) and isinstance(a.value, str) and a.value
                        for a in node.exc.args
                    ), f"{path.name}: NotImplementedError 必须带非空说明字符串"
    # 8 处方法级分支 (wechat/alipay 各 4) + 2 处构造期 fail-fast。
    assert count == 10, f"payments 包应有 10 处 NotImplementedError，实际 {count}"


# ── 4. HTTP 层: 生产下遗留回调端点本来就不可达（再确认一次）──────────────


def test_production_legacy_notify_endpoints_are_not_reachable(monkeypatch):
    """生产下 /payments/wechat/notify 与 /payments/alipay/notify 不会走到遗留 SDK。"""
    from fastapi.testclient import TestClient

    from app.deps import get_settings
    from app.main import app

    settings = get_settings()
    monkeypatch.setattr(settings, "environment", "production")
    get_settings.cache_clear()
    monkeypatch.setattr(
        "app.payments.router.get_settings",
        lambda: Settings(environment="production"),
    )
    try:
        with TestClient(app) as client:
            r = client.post("/payments/wechat/notify", content=b"{}")
            assert r.status_code == 403, f"实得 {r.status_code}: {r.text}"
            # 两个遗留回调端点在 production 下都不会走到 SDK 的沙箱分支：
            # wechat 403，alipay 返回字面量 "fail"（ASGI 层 webhook 验签中间件
            # 会先拦掉无签名请求，同样是"拒绝"，不是"处理"）。
            a = client.post("/payments/alipay/notify", data={})
            assert a.status_code == 403 or a.text == "fail", a.text
    finally:
        get_settings.cache_clear()


def test_no_mock_token_is_left_as_a_default_argument_value():
    """MOCK_WECHAT_TOKEN_XXX / MOCK_DY_TOKEN 不能再作为生产函数签名的默认值。

    用 AST 判定而不是全文搜索——它们作为**拒绝名单**出现在源码里是正确的。
    """
    import ast
    import pathlib

    from app.ext import oservi

    offenders: list[str] = []
    for path in sorted(pathlib.Path(oservi.__file__).parent.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = node.args
            for default in [*args.defaults, *[d for d in args.kw_defaults if d]]:
                if isinstance(default, ast.Constant) and isinstance(default.value, str):
                    if "MOCK" in default.value:
                        offenders.append(
                            f"{path.name}:{node.lineno} {node.name} "
                            f"默认 access_token={default.value!r}"
                        )
    assert offenders == [], "生产函数签名里仍带硬编码 MOCK 占位符:\n  - " + "\n  - ".join(offenders)


def test_placeholder_access_token_is_rejected_at_construction():
    """显式传占位符也要被拒——不能只靠"签名没默认值"这一道。"""
    from app.ext.oservi import (
        UNCONFIGURED_TOKEN,
        build_social_broadcast_engine,
    )

    for bad in ("MOCK_WECHAT_TOKEN_XXX", "MOCK_DY_TOKEN", "", "   "):
        with pytest.raises(ValueError):
            build_social_broadcast_engine(None, settings=None, access_token=bad)

    # 显式声明"未接入"是允许的（引擎照常装配，只是播报会失败）。
    engine = build_social_broadcast_engine(None, settings=None, access_token=UNCONFIGURED_TOKEN)
    assert engine is not None


def test_social_broadcast_engine_requires_an_explicit_access_token():
    """漏传 access_token 必须在调用期就 TypeError（签名里没有默认值）。"""
    from app.ext.oservi import build_social_broadcast_engine

    with pytest.raises(TypeError):
        build_social_broadcast_engine(None, settings=None)
