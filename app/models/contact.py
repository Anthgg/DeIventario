"""Contactos/proveedores (importados posteriormente desde Odoo)."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Contact(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Contacto/proveedor. ``external_ref`` admite valores como ACA6, THE6, KIA6."""

    __tablename__ = "contacts"

    external_ref: Mapped[str | None] = mapped_column(sa.String(50), index=True)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    raw_external_id: Mapped[str | None] = mapped_column(sa.String(255))
    active: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=True, server_default=sa.text("true")
    )
