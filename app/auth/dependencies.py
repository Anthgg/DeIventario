"""Dependencias FastAPI de autenticacion y RBAC."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.auth.claims import Claims
from app.auth.jwt import InvalidTokenError, JwtVerifier
from app.core.config import get_settings
from app.db.session import get_db
from app.models import User
from app.services.auth import rbac_service, user_service

_bearer = HTTPBearer(auto_error=False)
_verifier: JwtVerifier | None = None


def get_jwt_verifier() -> JwtVerifier:
    """Verificador unico (comparte la cache de JWKS entre peticiones)."""
    global _verifier
    if _verifier is None:
        _verifier = JwtVerifier(get_settings())
    return _verifier


class CurrentUser:
    """Identidad resuelta en backend (usuario local + permisos efectivos)."""

    def __init__(self, user: User, claims: Claims, permissions: set[str]) -> None:
        self.user = user
        self.claims = claims
        self.permissions = permissions

    @property
    def id(self) -> uuid.UUID:
        return self.user.id

    def has_permission(self, code: str) -> bool:
        return code in self.permissions


Credentials = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]
Database = Annotated[Session, Depends(get_db)]
Verifier = Annotated[JwtVerifier, Depends(get_jwt_verifier)]


def get_current_user(credentials: Credentials, db: Database, verifier: Verifier) -> CurrentUser:
    """Valida el Bearer token, sincroniza el usuario local y resuelve permisos."""
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=401, detail="No autenticado")
    try:
        claims = verifier.verify(credentials.credentials)
    except InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail="No autenticado") from exc

    user, changed = user_service.resolve_local_user(db, claims)
    if changed:
        db.commit()
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Usuario desactivado")
    permissions = rbac_service.user_permissions(db, user.id)
    return CurrentUser(user=user, claims=claims, permissions=permissions)


def require_permission(code: str) -> Callable[[CurrentUser], CurrentUser]:
    """Dependency: exige un permiso concreto."""

    def dependency(current: Annotated[CurrentUser, Depends(get_current_user)]) -> CurrentUser:
        if not current.has_permission(code):
            raise HTTPException(status_code=403, detail="Sin permiso")
        return current

    return dependency


def require_any_permission(*codes: str) -> Callable[[CurrentUser], CurrentUser]:
    """Dependency: exige al menos uno de los permisos indicados."""

    def dependency(current: Annotated[CurrentUser, Depends(get_current_user)]) -> CurrentUser:
        if not any(current.has_permission(code) for code in codes):
            raise HTTPException(status_code=403, detail="Sin permiso")
        return current

    return dependency


def require_role(role_code: str) -> Callable[..., CurrentUser]:
    """Dependency: exige un rol concreto (uso puntual; preferir permisos)."""

    def dependency(
        current: Annotated[CurrentUser, Depends(get_current_user)], db: Database
    ) -> CurrentUser:
        if role_code not in rbac_service.user_roles(db, current.id):
            raise HTTPException(status_code=403, detail="Sin permiso")
        return current

    return dependency
