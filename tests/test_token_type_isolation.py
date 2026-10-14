"""令牌类型隔离基线测试。

固定三条不变量：
1. 每一种签发的令牌都带 ``typ``，没有裸 payload 的令牌；
2. 校验端一律走白名单——拿 A 类型的令牌去换 B 类型的主体必失败；
3. 能力令牌 (cart / receipt) 用独立派生密钥签发，用主密钥解不出来，
   因此 staff/customer 主密钥泄漏不直接等于能力令牌泄漏。
"""

from __future__ import annotations

import pytest
from obase.crypto.util import CryptoUtil
from obase.exceptions import ObaseAuthError

from app.deps import get_settings
from app.security.tokens import (
    ALL_TOKEN_TYPES,
    DERIVED_KEY_TOKEN_TYPES,
    TOKEN_TYPE_CART,
    TOKEN_TYPE_CUSTOMER,
    TOKEN_TYPE_PICKUP,
    TOKEN_TYPE_RECEIPT,
    TOKEN_TYPE_STAFF,
    TokenTypeError,
    decode_token,
    derive_secret,
    sign_token,
)


def _raw_sign(payload: dict, *, secret: str | None = None) -> str:
    settings = get_settings()
    return CryptoUtil.jwt_sign(
        payload=payload,
        secret=secret or settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


# ── 1. 签发端：每种令牌都带 typ ──────────────────────────────────────────


def test_sign_token_stamps_typ_and_round_trips():
    settings = get_settings()
    token = sign_token(
        {"sub": "u1", "role": "admin"},
        typ=TOKEN_TYPE_STAFF,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        expires_in_minutes=10,
    )
    payload = decode_token(
        token,
        typ=TOKEN_TYPE_STAFF,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    assert payload["typ"] == TOKEN_TYPE_STAFF
    assert payload["sub"] == "u1"
    assert payload["role"] == "admin"


def test_sign_token_refuses_to_overwrite_caller_supplied_typ():
    with pytest.raises(ValueError):
        sign_token(
            {"typ": TOKEN_TYPE_STAFF},
            typ=TOKEN_TYPE_CART,
            secret="x" * 32,
            algorithm="HS256",
            expires_in_minutes=10,
        )


def test_derive_secret_is_purpose_scoped_and_deterministic():
    base = "a" * 64
    assert derive_secret(base, TOKEN_TYPE_CART) == derive_secret(base, TOKEN_TYPE_CART)
    assert derive_secret(base, TOKEN_TYPE_CART) != derive_secret(base, TOKEN_TYPE_RECEIPT)
    assert derive_secret(base, TOKEN_TYPE_CART) != base
    assert len(derive_secret(base, TOKEN_TYPE_CART)) >= 32


# ── 2. 校验端：白名单，不做黑名单 ────────────────────────────────────────


@pytest.mark.parametrize("typ", sorted(ALL_TOKEN_TYPES - {TOKEN_TYPE_STAFF}))
def test_non_staff_typ_cannot_be_decoded_as_staff(typ):
    """拿非 staff 令牌换 staff 主体必失败。

    失败形式有两种，都算过：同密钥类型（customer/pickup）→ typ 不匹配抛
    TokenTypeError；派生密钥类型（cart/receipt）→ 连签名都验不过。
    """
    settings = get_settings()
    token = sign_token(
        {"cart_id": "c1", "order_id": "o1", "customer_id": "k1", "sub": "u1", "node_name": "n"},
        typ=typ,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        expires_in_minutes=10,
    )
    # 密钥族不同时验签就失败，密钥族相同时类型白名单拒绝；两者都是"拒绝"。
    with pytest.raises((TokenTypeError, ObaseAuthError)):
        decode_token(
            token,
            typ=TOKEN_TYPE_STAFF,
            secret=settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
        )


@pytest.mark.parametrize("typ", sorted(ALL_TOKEN_TYPES))
def test_every_token_type_is_accepted_only_for_its_own_typ(typ):
    settings = get_settings()
    token = sign_token(
        {"x": "1"}, typ=typ, secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm, expires_in_minutes=10,
    )
    assert decode_token(
        token, typ=typ, secret=settings.jwt_secret, algorithm=settings.jwt_algorithm
    )["typ"] == typ
    for other in ALL_TOKEN_TYPES - {typ}:
        with pytest.raises((TokenTypeError, ObaseAuthError)):
            decode_token(
                token, typ=other, secret=settings.jwt_secret,
                algorithm=settings.jwt_algorithm,
            )


@pytest.mark.parametrize(
    "issued,requested",
    [
        (TOKEN_TYPE_STAFF, TOKEN_TYPE_CUSTOMER),
        (TOKEN_TYPE_CUSTOMER, TOKEN_TYPE_STAFF),
        (TOKEN_TYPE_PICKUP, TOKEN_TYPE_STAFF),
        (TOKEN_TYPE_STAFF, TOKEN_TYPE_PICKUP),
    ],
)
def test_same_key_type_mismatch_raises_token_type_error(issued, requested):
    """同密钥的几种身份令牌互换时，失败原因明确是类型不符而非验签失败。"""
    settings = get_settings()
    token = sign_token(
        {"sub": "u1"}, typ=issued, secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm, expires_in_minutes=10,
    )
    with pytest.raises(TokenTypeError):
        decode_token(
            token, typ=requested, secret=settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
        )


def test_untyped_token_is_rejected_by_every_whitelist():
    """裸 payload (无 typ) 的令牌——例如历史版本的收据令牌——一律拒绝。"""
    settings = get_settings()
    token = _raw_sign({"order_id": "o1"})
    for typ in ALL_TOKEN_TYPES:
        with pytest.raises((TokenTypeError, ObaseAuthError)):
            decode_token(
                token, typ=typ, secret=settings.jwt_secret,
                algorithm=settings.jwt_algorithm,
            )
    # 对同密钥类型，失败原因必须是"缺 typ"而不是别的。
    with pytest.raises(TokenTypeError):
        decode_token(
            token, typ=TOKEN_TYPE_STAFF, secret=settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
        )


# ── 3. 能力令牌的独立密钥 ───────────────────────────────────────────────


@pytest.mark.parametrize("typ", sorted(DERIVED_KEY_TOKEN_TYPES))
def test_capability_tokens_are_not_signed_with_the_primary_secret(typ):
    settings = get_settings()
    token = sign_token(
        {"cart_id": "c1"} if typ == TOKEN_TYPE_CART else {"order_id": "o1"},
        typ=typ,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        expires_in_minutes=10,
    )
    # 能力令牌用派生密钥签发，主密钥验签必然失败。
    with pytest.raises(ObaseAuthError):
        CryptoUtil.jwt_decode(
            token=token, secret=settings.jwt_secret, algorithm=settings.jwt_algorithm
        )


@pytest.mark.parametrize("typ", sorted(ALL_TOKEN_TYPES - DERIVED_KEY_TOKEN_TYPES))
def test_identity_tokens_still_use_the_primary_secret(typ):
    settings = get_settings()
    token = sign_token(
        {"sub": "u1"}, typ=typ, secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm, expires_in_minutes=10,
    )
    payload = CryptoUtil.jwt_decode(
        token=token, secret=settings.jwt_secret, algorithm=settings.jwt_algorithm
    )
    assert payload["typ"] == typ


def test_storefront_signs_cart_and_receipt_tokens_with_isolated_keys():
    """storefront 的两个能力令牌签发入口必须走派生密钥 + typ。"""
    from app import storefront

    settings = get_settings()
    cart_token = storefront._sign_cart_capability(
        "cart-1", settings.jwt_secret, settings.jwt_algorithm
    )
    receipt_token = storefront._sign_receipt(
        "order-1", settings.jwt_secret, settings.jwt_algorithm
    )
    # 主密钥解不出
    for token in (cart_token, receipt_token):
        with pytest.raises(ObaseAuthError):
            CryptoUtil.jwt_decode(
                token=token, secret=settings.jwt_secret, algorithm=settings.jwt_algorithm
            )
    assert decode_token(
        cart_token, typ=TOKEN_TYPE_CART, secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )["cart_id"] == "cart-1"
    assert decode_token(
        receipt_token, typ=TOKEN_TYPE_RECEIPT, secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )["order_id"] == "order-1"


# ── 4. 端到端：签发出来的能力令牌在各自端点上可用 ──────────────────────


def test_storefront_rejects_a_primary_secret_forged_receipt_token():
    """攻击者拿到主密钥也不能伪造收据令牌——收据用独立派生密钥。

    伪造手法是拿主密钥直接签一条 claims 写着 ``typ=receipt`` 的 JWT。
    查单端用派生密钥验签，验不过必须在碰数据库之前就返回 None。
    """
    forged = _raw_sign({"typ": TOKEN_TYPE_RECEIPT, "order_id": "order-9"})
    assert queries_get_order_by_receipt_token_fails(forged)


def queries_get_order_by_receipt_token_fails(token: str) -> bool:
    """收据查单必须拒绝伪造令牌 (返回 None)，且全程不碰数据库。"""
    import asyncio

    from app import queries

    settings = get_settings()

    class _Boom:
        def acquire(self):
            raise AssertionError("token must be rejected before touching the DB")

    result = asyncio.run(
        queries.get_order_by_receipt_token(_Boom(), token, settings.jwt_secret, settings.jwt_algorithm)
    )
    return result is None


def test_get_order_by_receipt_token_accepts_a_real_receipt_token():
    import asyncio

    from app import queries
    from app.storefront import _sign_receipt

    settings = get_settings()
    token = _sign_receipt("order-9", settings.jwt_secret, settings.jwt_algorithm)

    seen: dict[str, object] = {}

    class _Conn:
        async def fetchrow(self, sql, order_id):
            seen["order_id"] = order_id
            return {"order_id": order_id, "status": "paid"}

    class _Pool:
        def acquire(self):
            class _Ctx:
                async def __aenter__(self):
                    return _Conn()

                async def __aexit__(self, *exc):
                    return False

            return _Ctx()

    order = asyncio.run(
        queries.get_order_by_receipt_token(_Pool(), token, settings.jwt_secret, settings.jwt_algorithm)
    )
    assert order == {"order_id": "order-9", "status": "paid"}
    assert seen["order_id"] == "order-9"


@pytest.mark.parametrize("typ", [TOKEN_TYPE_STAFF, TOKEN_TYPE_CUSTOMER, TOKEN_TYPE_PICKUP])
def test_identity_tokens_cannot_be_used_as_receipt_tokens(typ):
    """staff / customer / 取货码令牌不能拿去查订单。"""
    import asyncio

    from app import queries

    settings = get_settings()
    token = sign_token(
        {"order_id": "victim-order"},
        typ=typ,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        expires_in_minutes=10,
    )

    class _Boom:
        def acquire(self):
            raise AssertionError("token must be rejected before touching the DB")

    assert asyncio.run(
        queries.get_order_by_receipt_token(
            _Boom(), token, settings.jwt_secret, settings.jwt_algorithm
        )
    ) is None