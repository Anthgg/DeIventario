"""Modelos del pipeline de importacion Odoo (F002).

Tablas: import_batches, import_errors, stock_snapshots, stock_movements.
Sin logica de negocio: solo persistencia de lo que llega en los exports.
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin, varchar_enum
from app.models.enums import ImportBatchStatus, ImportBatchType

# Estados que impiden volver a procesar el mismo archivo (proteccion de duplicados).
_ACTIVE_BATCH_STATUSES = ("PROCESSING", "COMPLETED", "COMPLETED_WITH_WARNINGS")


class ImportBatch(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Lote de importacion: un archivo procesado una vez."""

    __tablename__ = "import_batches"
    __table_args__ = (
        # Proteccion a nivel de base contra procesamiento duplicado/concurrente
        # del mismo archivo (mismo tipo + mismo SHA-256). Un lote en FAILED
        # queda fuera del indice parcial, por lo que puede reintentarse.
        sa.Index(
            "uq_import_batches_type_sha_active",
            "import_type",
            "source_sha256",
            unique=True,
            postgresql_where=sa.text(
                f"status IN ({', '.join(repr(s) for s in _ACTIVE_BATCH_STATUSES)})"
            ),
        ),
    )

    import_type: Mapped[ImportBatchType] = mapped_column(
        varchar_enum(ImportBatchType), nullable=False, index=True
    )
    source_filename: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    source_sha256: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    status: Mapped[ImportBatchStatus] = mapped_column(
        varchar_enum(ImportBatchStatus),
        nullable=False,
        default=ImportBatchStatus.PENDING,
        index=True,
    )
    started_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    completed_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    row_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, server_default=sa.text("0")
    )
    processed_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, server_default=sa.text("0")
    )
    created_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, server_default=sa.text("0")
    )
    updated_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, server_default=sa.text("0")
    )
    skipped_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, server_default=sa.text("0")
    )
    error_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, server_default=sa.text("0")
    )
    metadata_: Mapped[dict[str, object] | None] = mapped_column("metadata", postgresql.JSONB)


class ImportErrorRecord(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Error recuperable asociado a una fila/columna de un lote."""

    __tablename__ = "import_errors"

    import_batch_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        sa.ForeignKey("import_batches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sheet_name: Mapped[str | None] = mapped_column(sa.String(255))
    row_number: Mapped[int | None] = mapped_column(sa.Integer)
    column_name: Mapped[str | None] = mapped_column(sa.String(255))
    error_code: Mapped[str] = mapped_column(sa.String(50), nullable=False)
    message: Mapped[str] = mapped_column(sa.Text, nullable=False)
    raw_value: Mapped[str | None] = mapped_column(sa.String(500))


class StockSnapshot(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Cantidad a la mano congelada en el momento del import (historico)."""

    __tablename__ = "stock_snapshots"

    import_batch_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        sa.ForeignKey("import_batches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("products.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("locations.id", ondelete="SET NULL")
    )
    quantity: Mapped[decimal.Decimal] = mapped_column(sa.Numeric(18, 4), nullable=False)
    captured_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    source: Mapped[str] = mapped_column(
        sa.String(50),
        nullable=False,
        default="ODOO_EXPORT",
        server_default=sa.text("'ODOO_EXPORT'"),
    )


class StockMovement(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Movimiento de stock importado como evidencia (sin recalcular stock)."""

    __tablename__ = "stock_movements"

    import_batch_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        sa.ForeignKey("import_batches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("products.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    external_ref: Mapped[str | None] = mapped_column(sa.String(100))
    movement_description: Mapped[str | None] = mapped_column(sa.Text)
    source_location: Mapped[str | None] = mapped_column(sa.String(255))
    destination_location: Mapped[str | None] = mapped_column(sa.String(255))
    quantity: Mapped[decimal.Decimal] = mapped_column(sa.Numeric(18, 4), nullable=False)
    scheduled_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    effective_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    state: Mapped[str | None] = mapped_column(sa.String(50))
    raw_data: Mapped[dict[str, object] | None] = mapped_column("raw_data", postgresql.JSONB)
