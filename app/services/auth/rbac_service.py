"""Consultas RBAC y proteccion del ultimo administrador."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Permission, Role, RolePermission, User, UserRole

ADMIN_ROLE_CODE = "ADMIN"


def user_permissions(db: Session, user_id: uuid.UUID) -> set[str]:
    """Codigos de permiso efectivos del usuario (via sus roles)."""
    return set(
        db.execute(
            select(Permission.code)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .join(UserRole, UserRole.role_id == RolePermission.role_id)
            .where(UserRole.user_id == user_id)
        ).scalars()
    )


def user_roles(db: Session, user_id: uuid.UUID) -> list[str]:
    """Codigos de rol del usuario."""
    return list(
        db.execute(
            select(Role.code)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == user_id)
            .order_by(Role.code)
        ).scalars()
    )


def get_role_by_code(db: Session, code: str) -> Role | None:
    return db.execute(select(Role).where(Role.code == code)).scalar_one_or_none()


def lock_admin_role(db: Session) -> None:
    """Serialize operations that can remove an active ADMIN capability."""
    db.execute(
        select(Role.id).where(Role.code == ADMIN_ROLE_CODE).with_for_update()
    ).scalar_one_or_none()


def _count_active_admins(db: Session, exclude_user_id: uuid.UUID | None = None) -> int:
    query = (
        select(func.count())
        .select_from(User)
        .join(UserRole, UserRole.user_id == User.id)
        .join(Role, Role.id == UserRole.role_id)
        .where(Role.code == ADMIN_ROLE_CODE, User.is_active.is_(True))
    )
    if exclude_user_id is not None:
        query = query.where(User.id != exclude_user_id)
    return db.execute(query).scalar_one()


def is_last_active_admin(db: Session, user_id: uuid.UUID) -> bool:
    """True si ``user_id`` es el unico ADMIN activo restante."""
    if _count_active_admins(db, exclude_user_id=user_id) > 0:
        return False
    own = db.execute(
        select(func.count())
        .select_from(UserRole)
        .join(Role, Role.id == UserRole.role_id)
        .join(User, User.id == UserRole.user_id)
        .where(
            Role.code == ADMIN_ROLE_CODE,
            User.id == user_id,
            User.is_active.is_(True),
        )
    ).scalar_one()
    return own > 0


def assign_role(db: Session, user_id: uuid.UUID, role_code: str) -> bool:
    """Asigna un rol. Devuelve True si se creo la asignacion (idempotente)."""
    role = get_role_by_code(db, role_code)
    if role is None:
        return False
    existing = db.execute(
        select(UserRole).where(UserRole.user_id == user_id, UserRole.role_id == role.id).limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        return False
    db.add(UserRole(user_id=user_id, role_id=role.id))
    db.flush()
    return True


def revoke_role(db: Session, user_id: uuid.UUID, role_code: str) -> bool:
    """Quita un rol. Devuelve True si existia (idempotente)."""
    role = get_role_by_code(db, role_code)
    if role is None:
        return False
    existing = db.execute(
        select(UserRole).where(UserRole.user_id == user_id, UserRole.role_id == role.id).limit(1)
    ).scalar_one_or_none()
    if existing is None:
        return False
    db.delete(existing)
    db.flush()
    return True


def list_users(db: Session) -> list[tuple[User, list[str]]]:
    users = list(db.execute(select(User).order_by(User.display_name)).scalars())
    return [(user, user_roles(db, user.id)) for user in users]
