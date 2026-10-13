from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from app.auth import _staff_role_for_user
from app.config import Settings
from app.deps import require_staff_role


def test_staff_role_is_admin_only_for_explicitly_configured_user_ids():
    settings = Settings(privileged_staff_ids=" user-1, user-2 ")
    assert _staff_role_for_user("user-1", settings) == "admin"
    assert _staff_role_for_user("user-2", settings) == "admin"
    assert _staff_role_for_user("user-3", settings) == "staff"


def test_privileged_role_guard_rejects_staff_and_accepts_admin():
    guard = require_staff_role("admin")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(guard(principal={"claims": {"typ": "staff", "role": "staff"}}))
    assert exc.value.status_code == 403
    principal = {"claims": {"typ": "staff", "role": "admin"}}
    assert asyncio.run(guard(principal=principal)) == principal
