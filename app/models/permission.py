"""Permisos RBAC y su asignacion a roles (role_permissions)."""

from __future__ import annotations

import datetime as dt
import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class Permission(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Permiso del sistema (catalogo canonico, sembrado en migracion)."""

    __tablename__ = "permissions"

    code: Mapped[str] = mapped_column(sa.String(100), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text)


class RolePermission(Base):
    """Asignacion rol-permiso (PK compuesta impide duplicados)."""

    __tablename__ = "role_permissions"

    role_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("roles.id", ondelete="RESTRICT"), primary_key=True
    )
    permission_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("permissions.id", ondelete="RESTRICT"), primary_key=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
