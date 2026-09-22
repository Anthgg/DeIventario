"""Orquestacion de login / refresh / logout contra Supabase Auth."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from sqlalchemy.orm import Session

from app.auth.claims import Claims
from app.auth.jwt import InvalidTokenError, JwtVerifier
from app.auth.supabase_client import SupabaseAuthClient, SupabaseAuthError
from app.core.config import Settings, get_settings
from app.models import User
from app.services.auth.user_service import resolve_local_user


class InvalidCredentialsError(Exception):
    """Credenciales/sesion invalidas. El mensaje externo es generico."""


class AuthService:
    def __init__(
        self,
        settings: Settings | None = None,
        client: SupabaseAuthClient | None = None,
        verifier: JwtVerifier | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._client = client or SupabaseAuthClient(self._settings)
        self._verifier = verifier or JwtVerifier(self._settings)

    def login(self, db: Session, email: str, password: str) -> tuple[dict[str, Any], User]:
        try:
            session = self._client.sign_in(email, password)
        except SupabaseAuthError as exc:
            raise InvalidCredentialsError from exc
        claims = self._verify_session(session)
        user, changed = resolve_local_user(db, claims)
        if changed:
            db.commit()
        return session, user

    def refresh(self, db: Session, refresh_token: str) -> dict[str, Any]:
        try:
            session = self._client.refresh(refresh_token)
        except SupabaseAuthError as exc:
            raise InvalidCredentialsError from exc
        claims = self._verify_session(session)
        _user, changed = resolve_local_user(db, claims)
        if changed:
            db.commit()
        return session

    def logout(self, access_token: str) -> None:
        """Invalida la sesion en Supabase. Propaga el fallo si ocurre."""
        self._client.logout(access_token)

    def _verify_session(self, session: dict[str, Any]) -> Claims:
        token = session.get("access_token")
        if not isinstance(token, str):
            raise InvalidCredentialsError
        try:
            return self._verifier.verify(token)
        except InvalidTokenError as exc:
            raise InvalidCredentialsError from exc


@lru_cache(maxsize=1)
def get_auth_service() -> AuthService:
    """Instancia unica (comparte cliente Supabase y cache de JWKS)."""
    return AuthService()
