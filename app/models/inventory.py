"""Nucleo del dominio de inventario.

Campanas, asignaciones, snapshots, sesiones de conteo, log inmutable de
eventos y totales materializados.

Politica de borrado (PASO 23 del plan F001): preservar el historico.
- RESTRICT para datos maestros y historicos.
- SET NULL para referencias de auditoria (quien aprobo/cerro/reabrio).
- CASCADE solo en tablas puramente auxiliares (product_supplier_refs).
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin, varchar_enum
from app.models.enums import (
    AssignmentStatus,
    CampaignStatus,
    CostSource,
    CountEventType,
    EventSource,
    SessionStatus,
    SessionType,
    StockScope,
)
from app.models.location import Location
from app.models.product import Product


class InventoryCampaign(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Campana de inventario."""

    __tablename__ = "inventory_campaigns"

    code: Mapped[str] = mapped_column(sa.String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    status: Mapped[CampaignStatus] = mapped_column(
        varchar_enum(CampaignStatus), nullable=False, index=True
    )
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("locations.id", ondelete="RESTRICT")
    )
    starts_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    deadline_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True), index=True)
    submitted_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    approved_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    closed_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    closed_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    reopened_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    reopened_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    reopen_reason: Mapped[str | None] = mapped_column(sa.Text)

    # F004: fuente de expected stock y control de version.
    source_import_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("import_batches.id", ondelete="RESTRICT")
    )
    source_stock_scope: Mapped[StockScope | None] = mapped_column(varchar_enum(StockScope))
    snapshot_frozen_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    snapshot_sha256: Mapped[str | None] = mapped_column(sa.String(64))
    version: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=1, server_default=sa.text("1")
    )

    location: Mapped[Location | None] = relationship()
    snapshot_items: Mapped[list[InventorySnapshotItem]] = relationship(
        back_populates="campaign"
    )


class InventoryAssignment(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Asignacion usuario-campana con PK propia.

    Reasignar crea NUEVA asignacion (nunca se modifica/borra la anterior):
    por eso NO existe unique (campaign_id, user_id) — la historia se conserva.

    Un indice unico parcial garantiza como maximo UNA asignacion ACTIVE por
    campana, incluso con peticiones concurrentes.
    """

    __tablename__ = "inventory_assignments"
    __table_args__ = (
        sa.Index(
            "uq_inventory_assignments_active_per_campaign",
            "inventory_campaign_id",
            unique=True,
            postgresql_where=sa.text("status = 'ACTIVE' AND revoked_at IS NULL"),
        ),
    )

    inventory_campaign_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        sa.ForeignKey("inventory_campaigns.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        sa.ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    assigned_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    assigned_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    revoked_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    status: Mapped[AssignmentStatus] = mapped_column(
        varchar_enum(AssignmentStatus), nullable=False, index=True
    )


class InventorySnapshotItem(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Congelado del ERP/Odoo al crear la campana. Conceptualmente inmutable."""

    __tablename__ = "inventory_snapshot_items"
    __table_args__ = (
        # location_id es nullable y en PostgreSQL NULL != NULL: un UNIQUE normal
        # (campaign, product, location) permitiria duplicados con location NULL.
        # Se cubren ambos casos con dos indices unicos parciales.
        sa.Index(
            "uq_inventory_snapshot_items_campaign_product_no_location",
            "inventory_campaign_id",
            "product_id",
            unique=True,
            postgresql_where=sa.text("location_id IS NULL"),
        ),
        sa.Index(
            "uq_inventory_snapshot_items_campaign_product_location",
            "inventory_campaign_id",
            "product_id",
            "location_id",
            unique=True,
            postgresql_where=sa.text("location_id IS NOT NULL"),
        ),
    )

    inventory_campaign_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        sa.ForeignKey("inventory_campaigns.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("locations.id", ondelete="RESTRICT")
    )

    internal_reference_snapshot: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    description_snapshot: Mapped[str] = mapped_column(sa.Text, nullable=False)

    expected_quantity: Mapped[decimal.Decimal] = mapped_column(sa.Numeric(18, 4), nullable=False)

    sale_price_snapshot: Mapped[decimal.Decimal | None] = mapped_column(sa.Numeric(18, 4))
    cost_snapshot: Mapped[decimal.Decimal | None] = mapped_column(sa.Numeric(18, 4))
    consignment_cost_snapshot: Mapped[decimal.Decimal | None] = mapped_column(sa.Numeric(18, 4))
    effective_cost_snapshot: Mapped[decimal.Decimal | None] = mapped_column(sa.Numeric(18, 4))
    cost_source: Mapped[CostSource] = mapped_column(varchar_enum(CostSource), nullable=False)

    currency_snapshot: Mapped[str] = mapped_column(sa.String(3), nullable=False)

    supplier_reference_snapshot: Mapped[str | None] = mapped_column(sa.String(255))

    product: Mapped[Product] = relationship()
    campaign: Mapped[InventoryCampaign] = relationship(back_populates="snapshot_items")


class InventoryCountSession(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Sesion de conteo/reconteo. Cada sesion es independiente e historica."""

    __tablename__ = "inventory_count_sessions"
    __table_args__ = (sa.UniqueConstraint("inventory_campaign_id", "session_number"),)

    inventory_campaign_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        sa.ForeignKey("inventory_campaigns.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    assignment_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("inventory_assignments.id", ondelete="RESTRICT")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    session_number: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    session_type: Mapped[SessionType] = mapped_column(varchar_enum(SessionType), nullable=False)
    status: Mapped[SessionStatus] = mapped_column(varchar_enum(SessionStatus), nullable=False)
    started_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    submitted_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    device_identifier: Mapped[str | None] = mapped_column(sa.String(255))

    # F005: control de version y secuencia autoritativa de eventos.
    version: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=1, server_default=sa.text("1")
    )
    last_sequence: Mapped[int] = mapped_column(
        sa.BigInteger, nullable=False, default=0, server_default=sa.text("0")
    )
    last_activity_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))

    events: Mapped[list[InventoryCountEvent]] = relationship(back_populates="session")


class InventoryCountEvent(UUIDPrimaryKeyMixin, Base):
    """LOG INMUTABLE de conteo. ``client_event_uuid`` garantiza idempotencia."""

    __tablename__ = "inventory_count_events"
    __table_args__ = (
        sa.Index(
            "uq_inventory_count_events_session_sequence",
            "session_id",
            "server_sequence",
            unique=True,
            postgresql_where=sa.text("server_sequence IS NOT NULL"),
        ),
        sa.Index(
            "ix_inventory_count_events_session_product_sequence",
            "session_id",
            "product_id",
            "server_sequence",
        ),
        sa.Index(
            "uq_inventory_count_events_reverses_event",
            "reverses_event_id",
            unique=True,
            postgresql_where=sa.text("reverses_event_id IS NOT NULL"),
        ),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        sa.ForeignKey("inventory_count_sessions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("products.id", ondelete="RESTRICT"), index=True
    )
    scanned_code: Mapped[str | None] = mapped_column(sa.String(255), index=True)

    event_type: Mapped[CountEventType] = mapped_column(varchar_enum(CountEventType), nullable=False)
    delta_quantity: Mapped[decimal.Decimal | None] = mapped_column(sa.Numeric(18, 4))
    set_quantity: Mapped[decimal.Decimal | None] = mapped_column(sa.Numeric(18, 4))
    resulting_quantity: Mapped[decimal.Decimal | None] = mapped_column(sa.Numeric(18, 4))

    source: Mapped[EventSource] = mapped_column(varchar_enum(EventSource), nullable=False)

    client_event_uuid: Mapped[uuid.UUID] = mapped_column(sa.Uuid, unique=True, nullable=False)

    occurred_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, index=True
    )
    received_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    metadata_: Mapped[dict[str, object] | None] = mapped_column("metadata", postgresql.JSONB)

    # F005: secuencia autoritativa y soporte de undo.
    server_sequence: Mapped[int | None] = mapped_column(sa.BigInteger)
    previous_quantity: Mapped[decimal.Decimal | None] = mapped_column(sa.Numeric(18, 4))
    reverses_event_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("inventory_count_events.id", ondelete="RESTRICT")
    )

    # F006: eventos de dano (NULL en eventos normales; delta fisico es 0 aqui).
    damage_delta_quantity: Mapped[decimal.Decimal | None] = mapped_column(sa.Numeric(18, 4))
    previous_damaged_quantity: Mapped[decimal.Decimal | None] = mapped_column(sa.Numeric(18, 4))
    resulting_damaged_quantity: Mapped[decimal.Decimal | None] = mapped_column(sa.Numeric(18, 4))

    session: Mapped[InventoryCountSession] = relationship(back_populates="events")


class InventoryCountTotal(UUIDPrimaryKeyMixin, Base):
    """Materializacion operativa del estado de conteo (sesion, producto)."""

    __tablename__ = "inventory_count_totals"
    __table_args__ = (
        sa.UniqueConstraint("session_id", "product_id"),
        sa.CheckConstraint(
            "quantity >= 0", name="ck_inventory_count_totals_quantity_non_negative"
        ),
        sa.CheckConstraint(
            "damaged_quantity >= 0", name="ck_inventory_count_totals_damaged_non_negative"
        ),
        sa.CheckConstraint(
            "damaged_quantity <= quantity",
            name="ck_inventory_count_totals_damaged_le_quantity",
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
    damaged_quantity: Mapped[decimal.Decimal] = mapped_column(
        sa.Numeric(18, 4), nullable=False, default=0, server_default=sa.text("0")
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
