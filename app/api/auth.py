"""Endpoints de autenticacion (login / refresh / logout / me)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, SecretStr
from sqlalchemy.orm import Session

from app.auth.dependencies import Credentials, CurrentUser, Database, get_current_user
from app.auth.supabase_client import SupabaseAuthError
from app.models import User
from app.services.auth import audit_service, rbac_service
from app.services.auth.auth_service import (
    AuthService,
    InvalidCredentialsError,
    get_auth_service,
)

router = APIRouter(prefix="/auth", tags=["auth"])

AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]


class LoginRequest(BaseModel):
    email: str
    password: SecretStr


class RefreshRequest(BaseModel):
    refresh_token: SecretStr


def _session_payload(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "access_token": session.get("access_token"),
        "refresh_token": session.get("refresh_token"),
        "expires_in": session.get("expires_in"),
        "token_type": session.get("token_type", "bearer"),
    }


def _user_payload(db: Session, user: User) -> dict[str, Any]:
    return {
        "id": str(user.id),
        "email": user.email,
        "display_name": user.display_name,
        "is_active": user.is_active,
        "roles": rbac_service.user_roles(db, user.id),
    }


@router.post("/login")
def login(payload: LoginRequest, db: Database, service: AuthServiceDep) -> dict[str, Any]:
    """Intercambia credenciales contra Supabase Auth. Mensaje generico en error."""
    try:
        session, user = service.login(db, payload.email, payload.password.get_secret_value())
    except InvalidCredentialsError:
        audit_service.record(
            db,
            action=audit_service.LOGIN_FAILED,
            entity_type="user",
            metadata={"email": payload.email},
        )
        db.commit()
        raise HTTPException(status_code=401, detail="Credenciales invalidas") from None
    audit_service.record(
        db,
        action=audit_service.LOGIN_SUCCESS,
        actor_user_id=user.id,
        entity_type="user",
        entity_id=user.id,
    )
    db.commit()
    return {**_session_payload(session), "user": _user_payload(db, user)}


@router.post("/refresh")
def refresh(payload: RefreshRequest, db: Database, service: AuthServiceDep) -> dict[str, Any]:
    try:
        session = service.refresh(db, payload.refresh_token.get_secret_value())
    except InvalidCredentialsError:
        raise HTTPException(status_code=401, detail="Sesion invalida") from None
    return _session_payload(session)


@router.post("/logout")
def logout(
    current: CurrentUserDep,
    credentials: Credentials,
    db: Database,
    service: AuthServiceDep,
) -> dict[str, Any]:
    token = credentials.credentials if credentials is not None else ""
    try:
        service.logout(token)
    except SupabaseAuthError as exc:
        raise HTTPException(status_code=502, detail="No se pudo cerrar la sesion") from exc
    audit_service.record(
        db,
        action=audit_service.LOGOUT,
        actor_user_id=current.id,
        entity_type="user",
        entity_id=current.id,
    )
    db.commit()
    return {"status": "ok"}


@router.get("/me")
def me(current: CurrentUserDep, db: Database) -> dict[str, Any]:
    return {
        "id": str(current.user.id),
        "email": current.user.email,
        "display_name": current.user.display_name,
        "is_active": current.user.is_active,
        "roles": rbac_service.user_roles(db, current.id),
        "permissions": sorted(current.permissions),
    }
