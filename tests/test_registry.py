"""omodul 目录加载器测试 — 对抗命名偏差与登记完整性。"""

from __future__ import annotations

from app.registry import DOMAINS, all_endpoint_specs, load_endpoint


def test_all_registered_endpoints_load():
    """登记的全部 omodul 都能解析出 fn / Config / Input。"""
    specs = all_endpoint_specs()
    total = sum(len(v) for v in DOMAINS.values())
    assert len(specs) == total
    for s in specs:
        assert callable(s.fn)
        assert isinstance(s.config_cls, type)
        assert isinstance(s.input_cls, type)
        assert s.path == f"/{s.domain}/{s.name}"


def test_naming_override_line_items():
    """3 个购物车行元素的类名偏差被正确硬编码。"""
    s = load_endpoint("cart", "add_line_item_to_cart")
    assert s.config_cls.__name__ == "AddLineItemConfig"
    assert s.input_cls.__name__ == "AddLineItemInput"
    s2 = load_endpoint("cart", "delete_line_item_from_cart")
    assert s2.config_cls.__name__ == "DeleteLineItemConfig"


def test_camelcase_derivation_regular():
    """常规 omodul 按 CamelCase 约定派生类名。"""
    s = load_endpoint("settings", "create_region")
    assert s.config_cls.__name__ == "CreateRegionConfig"
    assert s.input_cls.__name__ == "CreateRegionInput"


def test_create_ops_return_201():
    assert load_endpoint("settings", "create_region").success_status == 201
    assert load_endpoint("cart", "update_cart").success_status == 200
    assert load_endpoint("checkout", "complete_checkout").success_status == 200


def test_public_ops():
    """create_customer 公开 (买家自助注册); 其余需鉴权。"""
    assert load_endpoint("customers", "create_customer").require_auth is False
    assert load_endpoint("settings", "create_region").require_auth is True
