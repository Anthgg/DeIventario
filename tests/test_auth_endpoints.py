"""Tests de los endpoints de autenticacion (Supabase mockeado)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth.supabase_client import SupabaseAuthError
from app.db.session import SessionLocal
from app.main import app
from app.models import User
from app.services.auth import rbac_service
from app.services.auth.auth_service import InvalidCredentialsError, get_auth_service
from tests.auth_helpers import (
    cleanup_auth_test_data,
    create_test_user,
    generate_keypair,
    make_token,
    override_auth,
    override_verifier,
)

client = TestClient(app)


class FakeAuthService:
    """Sustituto de AuthService: evita depender de Supabase en los tests."""

    def __init__(
        self,
        *,
        fail_login: bool = False,
        fail_refresh: bool = False,
        fail_logout: bool = False,
    ) -> None:
        self.fail_login = fail_login
        self.fail_refresh = fail_refresh
        self.fail_logout = fail_logout

    @staticmethod
    def _session(suffix: str) -> dict[str, Any]:
        return {
            "access_token": f"access-{suffix}",
            "refresh_token": f"refresh-{suffix}",
            "expires_in": 3600,
            "token_type": "bearer",
        }

    def login(self, db: Any, email: str, password: str) -> tuple[dict[str, Any], User]:
        if self.fail_login:
            raise InvalidCredentialsError
        user = db.execute(
            select(User).where(User.email.like("test-auth-%")).limit(1)
        ).scalar_one()
        return self._session("login"), user

    def refresh(self, db: Any, refresh_token: str) -> dict[str, Any]:
        if self.fail_refresh:
            raise InvalidCredentialsError
        return self._session("refresh")

    def logout(self, access_token: str) -> None:
        if self.fail_logout:
            raise SupabaseAuthError("fallo simulado")


def test_login_success_returns_session_and_user() -> None:
    cleanup_auth_test_data()
    create_test_user(email="test-auth-login@example.invalid", display_name="Login User")
    app.dependency_overrides[get_auth_service] = lambda: FakeAuthService()
    response = client.post(
        "/api/v1/auth/login", json={"email": "a@b.c", "password": "***"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["access_token"] == "access-login"
    assert body["refresh_token"] == "refresh-login"
    assert body["user"]["display_name"] == "Login User"
    assert "password" not in response.text
    cleanup_auth_test_data()


def test_login_invalid_credentials_generic_message() -> None:
    cleanup_auth_test_data()
    app.dependency_overrides[get_auth_service] = lambda: FakeAuthService(fail_login=True)
    response = client.post(
        "/api/v1/auth/login", json={"email": "nadie@example.invalid", "password": "***"}
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Credenciales invalidas"
    cleanup_auth_test_data()


def test_refresh_success_and_invalid() -> None:
    app.dependency_overrides[get_auth_service] = lambda: FakeAuthService()
    ok = client.post("/api/v1/auth/refresh", json={"refresh_token": "***"})
    assert ok.status_code == 200
    assert ok.json()["access_token"] == "access-refresh"

    app.dependency_overrides[get_auth_service] = lambda: FakeAuthService(fail_refresh=True)
    bad = client.post("/api/v1/auth/refresh", json={"refresh_token": "***"})
    assert bad.status_code == 401


def test_logout_success() -> None:
    cleanup_auth_test_data()
    actor_id = create_test_user(email="test-auth-logout@example.invalid")
    override_auth({"auth.self.read"}, user_id=actor_id)
    app.dependency_overrides[get_auth_service] = lambda: FakeAuthService()
    response = client.post("/api/v1/auth/logout", headers={"Authorization": "***"})
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    cleanup_auth_test_data()


def test_logout_reports_failure() -> None:
    cleanup_auth_test_data()
    actor_id = create_test_user(email="test-auth-logout2@example.invalid")
    override_auth({"auth.self.read"}, user_id=actor_id)
    app.dependency_overrides[get_auth_service] = lambda: FakeAuthService(fail_logout=True)
    response = client.post("/api/v1/auth/logout", headers={"Authorization": "***"})
    assert response.status_code == 502
    cleanup_auth_test_data()


def test_me_requires_authentication() -> None:
    assert client.get("/api/v1/auth/me").status_code == 401


def test_me_returns_identity_roles_and_permissions() -> None:
    """Ruta real: token firmado + usuario local + rol OPERATOR."""
    cleanup_auth_test_data()
    private_key, public_key = generate_keypair()
    sub = uuid.uuid4()
    override_verifier(public_key)
    with SessionLocal() as db:
        user = User(
            auth_user_id=sub,
            email="test-auth-me@example.invalid",
            display_name="Yo Mismo",
            is_active=True,
        )
        db.add(user)
        db.flush()
        rbac_service.assign_role(db, user.id, "OPERATOR")
        db.commit()
        user_id = user.id

    token = make_token(private_key, sub=str(sub), email="test-auth-me@example.invalid")
    response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(user_id)
    assert body["display_name"] == "Yo Mismo"
    assert body["roles"] == ["OPERATOR"]
    assert "inventory.count" in body["permissions"]
    assert "imports.execute" not in body["permissions"]
    for forbidden in ("access_token", "refresh_token", "secret"):
        assert forbidden not in response.text
    cleanup_auth_test_data()
