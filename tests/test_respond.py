"""respond 适配层单元测试 — omodul 返回 → HTTP 映射。"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from app.respond import _jsonable, omodul_to_response


def test_completed_default_200():
    r = omodul_to_response({"status": "completed", "order_id": "o1"})
    assert r.status_code == 200
    assert r.body and b"completed" in r.body


def test_completed_created_201():
    r = omodul_to_response({"status": "completed", "id": "x"}, success_status=201)
    assert r.status_code == 201


def test_failed_422():
    r = omodul_to_response({"status": "failed", "error": {"type": "ValueError", "message": "bad"}})
    assert r.status_code == 422


def test_cancelled_409():
    r = omodul_to_response({"status": "cancelled", "error": {"type": "Cancel", "message": "stop"}})
    assert r.status_code == 409


def test_unknown_status_500():
    r = omodul_to_response({"status": "weird"})
    assert r.status_code == 500


def test_jsonable_converts_non_native_types():
    out = _jsonable({"p": Path("/tmp/x"), "u": UUID(int=1), "n": 5, "lst": [Path("/a")]})
    assert out["p"] == "/tmp/x"
    assert isinstance(out["u"], str)
    assert out["n"] == 5
    assert out["lst"] == ["/a"]


def test_jsonable_passthrough_primitives():
    assert _jsonable(None) is None
    assert _jsonable(True) is True
    assert _jsonable("s") == "s"
