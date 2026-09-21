"""Ubicaciones fisicas donde pueden desarrollarse inventarios."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Location(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Ubicacion fisica de inventario (se cargaran en fases posteriores)."""

    __tablename__ = "locations"

    code: Mapped[str | None] = mapped_column(sa.String(50))
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    external_ref: Mapped[str | None] = mapped_column(sa.String(50))
    active: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=True, server_default=sa.text("true")
    )
