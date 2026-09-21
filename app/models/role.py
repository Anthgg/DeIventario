"""Roles y su asociacion con usuarios (muchos a muchos)."""

from __future__ import annotations

import datetime as dt
import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin
from app.models.user import User


class Role(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Rol del sistema (OPERATOR, SUPERVISOR, MANAGER, ADMIN en fases futuras)."""

    __tablename__ = "roles"

    code: Mapped[str] = mapped_column(sa.String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)

    users: Mapped[list[User]] = relationship(
        secondary="user_roles",
        primaryjoin="Role.id == UserRole.role_id",
        secondaryjoin="User.id == UserRole.user_id",
    )


class UserRole(Base):
    """Asignacion usuario-rol. PK compuesta impide roles duplicados por usuario."""

    __tablename__ = "user_roles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        sa.ForeignKey("users.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        sa.ForeignKey("roles.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    assigned_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    assigned_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
