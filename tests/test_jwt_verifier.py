"""Tests del verificador JWT (claves de TEST, nunca secretos reales)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import jwt
import pytest

from app.auth.jwt import InvalidTokenError
from app.core.config import get_settings
from tests.auth_helpers import generate_keypair, make_token, verifier_for


def test_valid_token() -> None:
    private_key, public_key = generate_keypair()
    sub = uuid.uuid4()
    token = make_token(private_key, sub=str(sub), email="user@example.invalid")
    claims = verifier_for(public_key).verify(token)
    assert claims.sub == sub
    assert claims.email == "user@example.invalid"


def test_expired_token() -> None:
    private_key, public_key = generate_keypair()
    token = make_token(private_key, sub=str(uuid.uuid4()), expires_in=-60)
    with pytest.raises(InvalidTokenError):
        verifier_for(public_key).verify(token)


def test_invalid_signature() -> None:
    private_key, _ = generate_keypair()
    _other_private, other_public = generate_keypair()
    token = make_token(private_key, sub=str(uuid.uuid4()))
    with pytest.raises(InvalidTokenError):
        verifier_for(other_public).verify(token)


def test_wrong_audience() -> None:
    private_key, public_key = generate_keypair()
    token = make_token(private_key, sub=str(uuid.uuid4()), audience="otra-audiencia")
    with pytest.raises(InvalidTokenError):
        verifier_for(public_key).verify(token)


def test_wrong_issuer() -> None:
    private_key, public_key = generate_keypair()
    token = make_token(private_key, sub=str(uuid.uuid4()), issuer="https://otro.example/auth/v1")
    with pytest.raises(InvalidTokenError):
        verifier_for(public_key).verify(token)


def test_invalid_sub() -> None:
    private_key, public_key = generate_keypair()
    token = make_token(private_key, sub="no-es-un-uuid")
    with pytest.raises(InvalidTokenError):
        verifier_for(public_key).verify(token)


def test_disallowed_algorithm() -> None:
    settings = get_settings()
    now = dt.datetime.now(dt.UTC)
    payload: dict[str, Any] = {
        "sub": str(uuid.uuid4()),
        "aud": settings.SUPABASE_JWT_AUDIENCE,
        "iss": settings.supabase_jwt_issuer,
        "iat": now,
        "exp": now + dt.timedelta(seconds=60),
    }
    token = jwt.encode(payload, "clave-de-test-hs256", algorithm="HS256")
    with pytest.raises(InvalidTokenError):
        verifier_for(None).verify(token)


def test_malformed_token() -> None:
    with pytest.raises(InvalidTokenError):
        verifier_for(None).verify("esto-no-es-un-jwt")


def test_missing_required_claim() -> None:
    private_key, public_key = generate_keypair()
    settings = get_settings()
    now = dt.datetime.now(dt.UTC)
    payload: dict[str, Any] = {
        "sub": str(uuid.uuid4()),
        "aud": settings.SUPABASE_JWT_AUDIENCE,
        "iss": settings.supabase_jwt_issuer,
        "exp": now + dt.timedelta(seconds=60),
    }  # sin iat
    token = jwt.encode(payload, private_key, algorithm="ES256")
    with pytest.raises(InvalidTokenError):
        verifier_for(public_key).verify(token)
