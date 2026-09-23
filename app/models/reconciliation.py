"""Conciliacion por producto al cerrar una campana (F008).

Comparacion Odoo (snapshot congelado) vs conteos oficiales. Los campos de
valores (missing_cost_value, damage_cost_value, etc.) existen pero F008 NO
los calcula: quedan NULL (valuacion es F009).
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, varchar_enum
from app.models.enums import ReconciliationStatus, SelectionMode


class InventoryReconciliation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Conciliacion de un producto dentro de una campana. UNIQUE campana+producto."""

    __tablename__ = "inventory_reconciliations"
    __table_args__ = (sa.UniqueConstraint("inventory_campaign_id", "product_id"),)

    inventory_campaign_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        sa.ForeignKey("inventory_campaigns.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("products.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    expected_quantity: Mapped[decimal.Decimal] = mapped_column(sa.Numeric(18, 4), nullable=False)

    approved_physical_quantity: Mapped[decimal.Decimal | None] = mapped_column(sa.Numeric(18, 4))
    difference_quantity: Mapped[decimal.Decimal | None] = mapped_column(sa.Numeric(18, 4))

    damaged_quantity: Mapped[decimal.Decimal] = mapped_column(
        sa.Numeric(18, 4), nullable=False, default=0, server_default=sa.text("0")
    )
    missing_quantity: Mapped[decimal.Decimal] = mapped_column(
        sa.Numeric(18, 4), nullable=False, default=0, server_default=sa.text("0")
    )
    surplus_quantity: Mapped[decimal.Decimal] = mapped_column(
        sa.Numeric(18, 4), nullable=False, default=0, server_default=sa.text("0")
    )

    effective_unit_cost: Mapped[decimal.Decimal | None] = mapped_column(sa.Numeric(18, 4))

    missing_cost_value: Mapped[decimal.Decimal | None] = mapped_column(sa.Numeric(18, 4))
    damage_cost_value: Mapped[decimal.Decimal | None] = mapped_column(sa.Numeric(18, 4))
    surplus_cost_value: Mapped[decimal.Decimal | None] = mapped_column(sa.Numeric(18, 4))
    affected_sale_value: Mapped[decimal.Decimal | None] = mapped_column(sa.Numeric(18, 4))

    status: Mapped[ReconciliationStatus] = mapped_column(
        varchar_enum(ReconciliationStatus), nullable=False, index=True
    )

    reason: Mapped[str | None] = mapped_column(sa.Text)
    observation: Mapped[str | None] = mapped_column(sa.Text)

    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    approved_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))

    selected_session_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid,
        sa.ForeignKey("inventory_count_sessions.id", ondelete="RESTRICT"),
    )
    selection_mode: Mapped[SelectionMode | None] = mapped_column(varchar_enum(SelectionMode))
    selected_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    selected_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    version: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=1, server_default=sa.text("1")
    )
