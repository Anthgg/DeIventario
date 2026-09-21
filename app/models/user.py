"""Usuarios del sistema.

Sin autenticacion todavia: ``auth_user_id`` queda preparado para la futura
integracion con Supabase Auth. Nunca se almacenan contrasenas aqui.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Usuario del sistema (operario/supervisor/jefe/admin en fases futuras)."""

    __tablename__ = "users"

    auth_user_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, unique=True)
    email: Mapped[str | None] = mapped_column(sa.String(320))
    display_name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=True, server_default=sa.text("true")
    )
