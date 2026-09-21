"""Dannos, items extra, codigos desconocidos y reconteos.

Regla de negocio futura: un producto danado sigue siendo existencia fisica
(fisico = buenos + dannados). Ningun modelo resta automaticamente dannos de
la cantidad fisica.
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.inventory import InventoryCountSession


class InventoryDamage(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Registro de producto danado durante una sesion."""

    __tablename__ = "inventory_damages"

    session_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        sa.ForeignKey("inventory_count_sessions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("products.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    quantity: Mapped[decimal.Decimal] = mapped_column(sa.Numeric(18, 4), nullable=False)
    reason: Mapped[str | None] = mapped_column(sa.String(255))
    observation: Mapped[str | None] = mapped_column(sa.Text)
    evidence_path: Mapped[str | None] = mapped_column(sa.String(512))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="SET NULL")
    )


class InventoryExtraItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Producto valido del maestro ausente en el snapshot original."""

    __tablename__ = "inventory_extra_items"
    __table_args__ = (sa.UniqueConstraint("session_id", "product_id"),)

    session_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        sa.ForeignKey("inventory_count_sessions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("products.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    quantity: Mapped[decimal.Decimal] = mapped_column(
        sa.Numeric(18, 4), nullable=False, default=0, server_default=sa.text("0")
    )
    first_detected_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class InventoryUnknownCode(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """QR/codigo escaneado inexistente en el maestro. No bloquea el conteo."""

    __tablename__ = "inventory_unknown_codes"
    __table_args__ = (sa.UniqueConstraint("session_id", "scanned_code"),)

    session_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        sa.ForeignKey("inventory_count_sessions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    scanned_code: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    quantity: Mapped[decimal.Decimal] = mapped_column(
        sa.Numeric(18, 4), nullable=False, default=0, server_default=sa.text("0")
    )
    resolved_product_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("products.id", ondelete="RESTRICT")
    )
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    resolved_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))


class InventoryRecount(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Reconteo ciego: nunca almacenar aqui resultados visibles al operario."""

    __tablename__ = "inventory_recounts"

    inventory_campaign_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        sa.ForeignKey("inventory_campaigns.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    assigned_user_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    source_session_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("inventory_count_sessions.id", ondelete="RESTRICT")
    )
    resulting_session_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("inventory_count_sessions.id", ondelete="RESTRICT")
    )

    # Dos FK hacia la misma tabla: se declaran explicitamente.
    source_session: Mapped[InventoryCountSession | None] = relationship(
        foreign_keys="InventoryRecount.source_session_id"
    )
    resulting_session: Mapped[InventoryCountSession | None] = relationship(
        foreign_keys="InventoryRecount.resulting_session_id"
    )

    reason: Mapped[str | None] = mapped_column(sa.Text)
    completed_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
