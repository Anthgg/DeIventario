"""Endpoints de administracion de usuarios (RBAC)."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.dependencies import CurrentUser, Database, require_permission
from app.auth.permissions import USERS_MANAGE, USERS_MANAGE_ROLES, USERS_READ
from app.models import User
from app.services.auth import audit_service, rbac_service

router = APIRouter(prefix="/admin/users", tags=["admin"])

RequireUsersRead = Annotated[CurrentUser, Depends(require_permission(USERS_READ))]
RequireUsersManage = Annotated[CurrentUser, Depends(require_permission(USERS_MANAGE))]
RequireUsersManageRoles = Annotated[CurrentUser, Depends(require_permission(USERS_MANAGE_ROLES))]


class ActiveUpdate(BaseModel):
    is_active: bool


def _payload(db: Session, user: User) -> dict[str, Any]:
    return {
        "id": str(user.id),
        "email": user.email,
        "display_name": user.display_name,
        "is_active": user.is_active,
        "roles": rbac_service.user_roles(db, user.id),
    }


def _get_user_or_404(db: Session, user_id: uuid.UUID) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    return user


@router.get("")
def list_users(current: RequireUsersRead, db: Database) -> list[dict[str, Any]]:
    return [_payload(db, user) for user, _roles in rbac_service.list_users(db)]


@router.get("/{user_id}")
def get_user(user_id: uuid.UUID, current: RequireUsersRead, db: Database) -> dict[str, Any]:
    return _payload(db, _get_user_or_404(db, user_id))


@router.patch("/{user_id}/active")
def set_active(
    user_id: uuid.UUID, payload: ActiveUpdate, current: RequireUsersManage, db: Database
) -> dict[str, Any]:
    user = _get_user_or_404(db, user_id)
    if user.is_active and not payload.is_active and rbac_service.is_last_active_admin(db, user_id):
        raise HTTPException(
            status_code=409, detail="No se puede desactivar al ultimo ADMIN activo"
        )
    if user.is_active != payload.is_active:
        user.is_active = payload.is_active
        action = (
            audit_service.USER_ACTIVATED if payload.is_active else audit_service.USER_DEACTIVATED
        )
        audit_service.record(
            db,
            action=action,
            actor_user_id=current.id,
            entity_type="user",
            entity_id=user_id,
        )
        db.commit()
    return _payload(db, user)


@router.post("/{user_id}/roles/{role_code}")
def assign_role(
    user_id: uuid.UUID, role_code: str, current: RequireUsersManageRoles, db: Database
) -> dict[str, Any]:
    user = _get_user_or_404(db, user_id)
    if rbac_service.get_role_by_code(db, role_code) is None:
        raise HTTPException(status_code=404, detail="Rol no encontrado")
    if rbac_service.assign_role(db, user_id, role_code):
        audit_service.record(
            db,
            action=audit_service.ROLE_ASSIGNED,
            actor_user_id=current.id,
            entity_type="user",
            entity_id=user_id,
            metadata={"role": role_code},
        )
        db.commit()
    return _payload(db, user)


@router.delete("/{user_id}/roles/{role_code}")
def revoke_role(
    user_id: uuid.UUID, role_code: str, current: RequireUsersManageRoles, db: Database
) -> dict[str, Any]:
    user = _get_user_or_404(db, user_id)
    if role_code == rbac_service.ADMIN_ROLE_CODE and rbac_service.is_last_active_admin(db, user_id):
        raise HTTPException(
            status_code=409, detail="No se puede quitar ADMIN al ultimo ADMIN activo"
        )
    if rbac_service.revoke_role(db, user_id, role_code):
        audit_service.record(
            db,
            action=audit_service.ROLE_REVOKED,
            actor_user_id=current.id,
            entity_type="user",
            entity_id=user_id,
            metadata={"role": role_code},
        )
        db.commit()
    return _payload(db, user)
