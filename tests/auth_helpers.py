"""Helpers de autenticacion para tests (claves de TEST, nunca secretos reales)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import delete, select

from app.auth.claims import Claims
from app.auth.dependencies import CurrentUser, get_current_user
from app.auth.jwt import JwtVerifier
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.main import app
from app.models import AuditEvent, User, UserRole

_TEST_EMAIL_PREFIX = "test-auth-"


def generate_keypair() -> tuple[ec.EllipticCurvePrivateKey, ec.EllipticCurvePublicKey]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    return private_key, private_key.public_key()


def make_token(
    private_key: ec.EllipticCurvePrivateKey,
    *,
    sub: str,
    audience: str | None = None,
    issuer: str | None = None,
    email: str | None = None,
    metadata: dict[str, Any] | None = None,
    expires_in: int = 3600,
) -> str:
    """Firma un JWT de prueba con ES256 y la clave privada de test."""
    settings = get_settings()
    now = dt.datetime.now(dt.UTC)
    payload: dict[str, Any] = {
        "sub": sub,
        "aud": audience if audience is not None else settings.SUPABASE_JWT_AUDIENCE,
        "iss": issuer if issuer is not None else settings.supabase_jwt_issuer,
        "iat": now,
        "exp": now + dt.timedelta(seconds=expires_in),
    }
    if email is not None:
        payload["email"] = email
    if metadata is not None:
        payload["user_metadata"] = metadata
    return jwt.encode(payload, private_key, algorithm="ES256")


def verifier_for(public_key: Any) -> JwtVerifier:
    """Verificador con clave inyectada (sin red)."""
    return JwtVerifier(get_settings(), key_provider=lambda _token: public_key)


def fake_current_user(
    permissions: set[str], *, is_active: bool = True, user_id: uuid.UUID | None = None
) -> CurrentUser:
    identity = user_id or uuid.uuid4()
    user = User(
        id=identity,
        display_name="tester",
        email=f"{_TEST_EMAIL_PREFIX}tester@example.invalid",
        is_active=is_active,
    )
    claims = Claims(sub=identity, email=user.email, role_claim="authenticated", metadata={})
    return CurrentUser(user=user, claims=claims, permissions=permissions)


def override_auth(
    permissions: set[str], *, is_active: bool = True, user_id: uuid.UUID | None = None
) -> None:
    """Sustituye ``get_current_user`` por una identidad con esos permisos."""
    app.dependency_overrides[get_current_user] = lambda: fake_current_user(
        permissions, is_active=is_active, user_id=user_id
    )


def override_verifier(public_key: Any) -> None:
    """Sustituye el verificador JWT por uno con clave de test."""
    from app.auth.dependencies import get_jwt_verifier

    app.dependency_overrides[get_jwt_verifier] = lambda: verifier_for(public_key)


def create_test_user(
    *,
    auth_user_id: uuid.UUID | None = None,
    email: str | None = None,
    display_name: str = "test user",
    is_active: bool = True,
) -> uuid.UUID:
    """Crea un usuario local de prueba (email con prefijo test-auth-)."""
    with SessionLocal() as db:
        user = User(
            auth_user_id=auth_user_id,
            email=email or f"{_TEST_EMAIL_PREFIX}{uuid.uuid4()}@example.invalid",
            display_name=display_name,
            is_active=is_active,
        )
        db.add(user)
        db.commit()
        return user.id


def cleanup_auth_test_data() -> None:
    """Elimina usuarios/roles/auditoria creados por los tests."""
    with SessionLocal() as db:
        ids = list(
            db.execute(select(User.id).where(User.email.like(f"{_TEST_EMAIL_PREFIX}%"))).scalars()
        )
        if not ids:
            return
        # Las asignaciones de inventario referencian al usuario con RESTRICT.
        from app.models import InventoryAssignment

        db.execute(delete(InventoryAssignment).where(InventoryAssignment.user_id.in_(ids)))
        db.execute(delete(AuditEvent).where(AuditEvent.entity_id.in_(ids)))
        db.execute(delete(UserRole).where(UserRole.user_id.in_(ids)))
        db.execute(delete(User).where(User.id.in_(ids)))
        db.commit()
