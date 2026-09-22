"""Tests de sincronizacion del usuario local (first token / reutilizacion)."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.auth.claims import Claims
from app.db.session import SessionLocal
from app.main import app
from app.models import User
from app.services.auth.user_service import resolve_local_user
from tests.auth_helpers import (
    cleanup_auth_test_data,
    generate_keypair,
    make_token,
    override_verifier,
)


def _claims(sub: uuid.UUID, email: str | None, metadata: dict[str, object]) -> Claims:
    return Claims(sub=sub, email=email, role_claim="authenticated", metadata=metadata)


def test_first_token_creates_local_user() -> None:
    sub = uuid.uuid4()
    with SessionLocal() as db:
        user, created = resolve_local_user(
            db, _claims(sub, "test-auth-new@example.invalid", {"full_name": "Nombre Real"})
        )
        assert created is True
        assert user.auth_user_id == sub
        assert user.display_name == "Nombre Real"
        db.rollback()


def test_second_token_reuses_user() -> None:
    sub = uuid.uuid4()
    claims = _claims(sub, "test-auth-reuse@example.invalid", {})
    with SessionLocal() as db:
        first, created_first = resolve_local_user(db, claims)
        assert created_first is True
        second, created_second = resolve_local_user(db, claims)
        assert created_second is False
        assert second.id == first.id
        db.rollback()


def test_email_is_updated() -> None:
    sub = uuid.uuid4()
    with SessionLocal() as db:
        user, _ = resolve_local_user(db, _claims(sub, "test-auth-old@example.invalid", {}))
        updated, changed = resolve_local_user(
            db, _claims(sub, "test-auth-newer@example.invalid", {})
        )
        assert changed is True
        assert updated.email == "test-auth-newer@example.invalid"
        assert updated.id == user.id
        db.rollback()


def test_display_name_falls_back_to_email() -> None:
    with SessionLocal() as db:
        user, _ = resolve_local_user(db, _claims(uuid.uuid4(), "test-auth-fb@example.invalid", {}))
        assert user.display_name == "test-auth-fb@example.invalid"
        db.rollback()


def test_display_name_falls_back_to_uuid() -> None:
    sub = uuid.uuid4()
    with SessionLocal() as db:
        user, _ = resolve_local_user(db, _claims(sub, None, {}))
        assert user.display_name == str(sub)
        db.rollback()


def test_display_name_uses_name_key() -> None:
    with SessionLocal() as db:
        user, _ = resolve_local_user(
            db, _claims(uuid.uuid4(), "test-auth-nm@example.invalid", {"name": "Desde name"})
        )
        assert user.display_name == "Desde name"
        db.rollback()


def test_display_name_prefers_full_name() -> None:
    with SessionLocal() as db:
        user, _ = resolve_local_user(
            db,
            _claims(
                uuid.uuid4(),
                "test-auth-fn@example.invalid",
                {"full_name": "Nombre Completo", "name": "Nombre Corto"},
            ),
        )
        assert user.display_name == "Nombre Completo"
        db.rollback()


def test_inactive_user_gets_403() -> None:
    cleanup_auth_test_data()
    private_key, public_key = generate_keypair()
    sub = uuid.uuid4()
    override_verifier(public_key)
    with SessionLocal() as db:
        db.add(
            User(
                auth_user_id=sub,
                email="test-auth-inactive@example.invalid",
                display_name="Inactivo",
                is_active=False,
            )
        )
        db.commit()
    token = make_token(private_key, sub=str(sub), email="test-auth-inactive@example.invalid")
    response = TestClient(app).get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403
    cleanup_auth_test_data()


def test_active_user_can_read_me() -> None:
    cleanup_auth_test_data()
    private_key, public_key = generate_keypair()
    sub = uuid.uuid4()
    override_verifier(public_key)
    token = make_token(private_key, sub=str(sub), email="test-auth-active-me@example.invalid")
    response = TestClient(app).get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    assert response.json()["email"] == "test-auth-active-me@example.invalid"
    cleanup_auth_test_data()


def test_missing_or_invalid_bearer_gets_401() -> None:
    client = TestClient(app)
    assert client.get("/api/v1/auth/me").status_code == 401
    empty = {"Authorization": "Bearer "}
    assert client.get("/api/v1/auth/me", headers=empty).status_code == 401
    corrupt = {"Authorization": "Bearer no-es-un-jwt"}
    assert client.get("/api/v1/auth/me", headers=corrupt).status_code == 401
