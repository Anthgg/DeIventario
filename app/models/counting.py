"""Dannos, items extra, codigos desconocidos y reconteos.

Regla de negocio: un producto danado sigue siendo existencia fisica
(fisico = buenos + dannados). Ningun modelo resta automaticamente dannos de
la cantidad fisica. Los registros de dano son inmutables: corregir = nuevo
evento DAMAGE_SUBTRACT (nunca PATCH/DELETE del historial).
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin, varchar_enum
from app.models.enums import DamageAction, RecountStatus
from app.models.inventory import InventoryCountSession


class InventoryDamage(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Historial auditable e inmutable de ajustes de dano.

    Exactamente uno de ``product_id`` / ``scanned_code`` identifica el objetivo.
    ``quantity`` es siempre positiva; el signo lo determina ``action``.
    """

    __tablename__ = "inventory_damages"
    __table_args__ = (
        sa.CheckConstraint(
            "(product_id IS NOT NULL AND scanned_code IS NULL) "
            "OR (product_id IS NULL AND scanned_code IS NOT NULL)",
            name="ck_inventory_damages_target_exactly_one",
        ),
        sa.CheckConstraint("quantity > 0", name="ck_inventory_damages_quantity_positive"),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        sa.ForeignKey("inventory_count_sessions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("products.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    scanned_code: Mapped[str | None] = mapped_column(sa.String(255))
    quantity: Mapped[decimal.Decimal] = mapped_column(sa.Numeric(18, 4), nullable=False)
    action: Mapped[DamageAction] = mapped_column(varchar_enum(DamageAction), nullable=False)
    reason: Mapped[str | None] = mapped_column(sa.String(255))
    observation: Mapped[str | None] = mapped_column(sa.Text)
    evidence_path: Mapped[str | None] = mapped_column(sa.String(512))
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid,
        sa.ForeignKey("inventory_count_events.id", ondelete="RESTRICT"),
        unique=True,
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="SET NULL")
    )


class InventoryExtraItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Producto valido del maestro ausente en el snapshot original."""

    __tablename__ = "inventory_extra_items"
    __table_args__ = (
        sa.UniqueConstraint("session_id", "product_id"),
        sa.CheckConstraint(
            "quantity >= 0", name="ck_inventory_extra_items_quantity_non_negative"
        ),
    )

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
    __table_args__ = (
        sa.UniqueConstraint("session_id", "scanned_code"),
        sa.CheckConstraint(
            "quantity >= 0", name="ck_inventory_unknown_codes_quantity_non_negative"
        ),
        sa.CheckConstraint(
            "damaged_quantity >= 0", name="ck_inventory_unknown_codes_damaged_non_negative"
        ),
        sa.CheckConstraint(
            "damaged_quantity <= quantity",
            name="ck_inventory_unknown_codes_damaged_le_quantity",
        ),
    )

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
    damaged_quantity: Mapped[decimal.Decimal] = mapped_column(
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
    """Reconteo ciego: nunca almacenar aqui resultados visibles al operario.

    Maximo UN reconteo abierto (REQUESTED/ASSIGNED/IN_PROGRESS) por campana:
    lo garantiza un indice unico parcial a nivel de base de datos.
    """

    __tablename__ = "inventory_recounts"
    __table_args__ = (
        sa.Index(
            "uq_inventory_recounts_open_per_campaign",
            "inventory_campaign_id",
            unique=True,
            postgresql_where=sa.text(
                "status IN ('REQUESTED', 'ASSIGNED', 'IN_PROGRESS')"
            ),
        ),
    )

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

    status: Mapped[RecountStatus] = mapped_column(
        varchar_enum(RecountStatus), nullable=False, server_default="ASSIGNED"
    )
    # Optimistic locking (F007): el cliente envia expected_version.
    expected_version: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=1, server_default=sa.text("1")
    )
    started_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    cancelled_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    cancelled_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    cancel_reason: Mapped[str | None] = mapped_column(sa.Text)

    reason: Mapped[str | None] = mapped_column(sa.Text)
    completed_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
