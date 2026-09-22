"""Tests del CLI bootstrap_admin (dry-run e idempotencia, Supabase mockeado)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import func, select

from app.cli import bootstrap_admin
from app.db.session import SessionLocal
from app.models import User, UserRole
from app.services.auth import rbac_service
from tests.auth_helpers import cleanup_auth_test_data


def _fake_supabase_user(email: str) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "email": email,
        "user_metadata": {"full_name": "Persona de Prueba"},
    }


def test_dry_run_does_not_mutate(monkeypatch: pytest.MonkeyPatch) -> None:
    cleanup_auth_test_data()
    email = "test-auth-bootstrap@example.invalid"
    monkeypatch.setattr(
        bootstrap_admin, "find_supabase_user", lambda _email: _fake_supabase_user(email)
    )
    assert bootstrap_admin.main(["--email", email]) == 0
    with SessionLocal() as db:
        assert db.execute(select(User).where(User.email == email)).scalar_one_or_none() is None


def test_confirm_creates_admin_and_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    cleanup_auth_test_data()
    email = "test-auth-bootstrap2@example.invalid"
    supabase_user = _fake_supabase_user(email)
    monkeypatch.setattr(bootstrap_admin, "find_supabase_user", lambda _email: supabase_user)

    assert bootstrap_admin.main(["--email", email, "--confirm"]) == 0
    with SessionLocal() as db:
        user = db.execute(select(User).where(User.email == email)).scalar_one()
        assert user.display_name == "Persona de Prueba"
        assert rbac_service.user_roles(db, user.id) == ["ADMIN"]

    # Segunda ejecucion: no duplica la asignacion.
    assert bootstrap_admin.main(["--email", email, "--confirm"]) == 0
    with SessionLocal() as db:
        user = db.execute(select(User).where(User.email == email)).scalar_one()
        assignments = db.execute(
            select(func.count()).select_from(UserRole).where(UserRole.user_id == user.id)
        ).scalar_one()
        assert assignments == 1
        assert rbac_service.user_roles(db, user.id) == ["ADMIN"]
    cleanup_auth_test_data()


def test_unknown_email_exits_with_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap_admin, "find_supabase_user", lambda _email: None)
    assert bootstrap_admin.main(["--email", "nadie@example.invalid"]) == 1
