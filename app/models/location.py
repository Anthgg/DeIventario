"""Ubicaciones fisicas donde pueden desarrollarse inventarios."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Location(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Ubicacion fisica de inventario.

    ``code`` es unico cuando no es NULL (indice unico parcial); el nombre NO se
    asume unico.
    """

    __tablename__ = "locations"
    __table_args__ = (
        sa.Index(
            "uq_locations_code_not_null",
            "code",
            unique=True,
            postgresql_where=sa.text("code IS NOT NULL"),
        ),
    )

    code: Mapped[str | None] = mapped_column(sa.String(50))
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    external_ref: Mapped[str | None] = mapped_column(sa.String(50))
    active: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=True, server_default=sa.text("true")
    )
