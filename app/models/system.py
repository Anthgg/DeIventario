"""Configuracion organizacional y motor de documentos oficiales (F010).

- ``OrganizationSettings``: configuracion unica activa (razon social, marca,
  colores, footer, numeracion). Singleton servido por la capa de servicio.
- ``DocumentExport``: historial inmutable de cada documento generado
  (PDF/XLSX) con hash de fuente, hash de branding y estado de superacion.
- ``ExportProfile``: perfiles configurables de exportacion ERP (columnas,
  orden, separador, moneda, fecha, formato, numero de documento).
"""

from __future__ import annotations

import datetime as dt
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin, varchar_enum
from app.models.enums import (
    CurrencyStyle,
    DocumentFormat,
    DocumentModule,
    DocumentStatus,
)


class OrganizationSettings(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Configuracion unica de identidad y branding de la organizacion.

    La tabla solo debe contener una fila activa: ``get_settings`` crea la
    fila vacia bajo lock la primera vez que se consulta. ``version`` habilita
    actualizaciones optimistas (PATCH con ``expected_version``).
    """

    __tablename__ = "organization_settings"
    __table_args__ = (
        sa.CheckConstraint(
            "next_document_number >= 1",
            name="ck_organization_settings_next_document_number_positive",
        ),
    )

    legal_name: Mapped[str | None] = mapped_column(sa.String(255))
    tax_id: Mapped[str | None] = mapped_column(sa.String(50))
    tax_id_label: Mapped[str | None] = mapped_column(sa.String(50))
    address: Mapped[str | None] = mapped_column(sa.Text)
    phone: Mapped[str | None] = mapped_column(sa.String(50))
    email: Mapped[str | None] = mapped_column(sa.String(255))
    website: Mapped[str | None] = mapped_column(sa.String(255))

    logo_path: Mapped[str | None] = mapped_column(sa.String(500))
    logo_sha256: Mapped[str | None] = mapped_column(sa.String(64))
    logo_media_type: Mapped[str | None] = mapped_column(sa.String(100))
    logo_original_name: Mapped[str | None] = mapped_column(sa.String(255))

    brand_color_primary: Mapped[str | None] = mapped_column(sa.String(20))
    brand_color_secondary: Mapped[str | None] = mapped_column(sa.String(20))
    brand_color_accent: Mapped[str | None] = mapped_column(sa.String(20))
    currency_style: Mapped[CurrencyStyle] = mapped_column(
        varchar_enum(CurrencyStyle), nullable=False, server_default="SYMBOL_BEFORE"
    )
    date_format: Mapped[str] = mapped_column(
        sa.String(30), nullable=False, server_default="YYYY-MM-DD"
    )
    footer_text: Mapped[str | None] = mapped_column(sa.Text)
    footer_left: Mapped[str | None] = mapped_column(sa.Text)
    footer_right: Mapped[str | None] = mapped_column(sa.Text)

    document_number_prefix: Mapped[str] = mapped_column(
        sa.String(30), nullable=False, server_default="DOC"
    )
    next_document_number: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="1"
    )

    version: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=1, server_default=sa.text("1")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="SET NULL")
    )


class DocumentExport(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Registro historico de un documento oficial generado (F010)."""

    __tablename__ = "document_exports"
    __table_args__ = (
        sa.Index(
            "uq_document_exports_idempotency",
            "module",
            "document_type",
            "entity_id",
            "source_sha256",
            "branding_sha256",
            "template_version",
            unique=True,
            postgresql_where=sa.text("status = 'GENERATED'"),
        ),
        sa.Index(
            "ix_document_exports_entity_type_id",
            "entity_type",
            "entity_id",
        ),
        sa.CheckConstraint("file_size >= 0", name="ck_document_exports_filesize_nonnegative"),
    )

    module: Mapped[DocumentModule] = mapped_column(varchar_enum(DocumentModule), nullable=False)
    document_type: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    title: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    document_number: Mapped[str] = mapped_column(sa.String(50), nullable=False, unique=True)
    status: Mapped[DocumentStatus] = mapped_column(
        varchar_enum(DocumentStatus), nullable=False, index=True
    )
    format: Mapped[DocumentFormat] = mapped_column(
        varchar_enum(DocumentFormat), nullable=False
    )
    template_version: Mapped[str] = mapped_column(sa.String(30), nullable=False)
    module_version: Mapped[str] = mapped_column(sa.String(30), nullable=False)
    locale: Mapped[str] = mapped_column(sa.String(10), nullable=False, server_default="es")

    entity_type: Mapped[str] = mapped_column(sa.String(50), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False)
    inventory_campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("inventory_campaigns.id", ondelete="RESTRICT"), index=True
    )

    source_sha256: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    branding_sha256: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    source_snapshot: Mapped[dict[str, object] | None] = mapped_column(postgresql.JSONB)

    file_path: Mapped[str] = mapped_column(sa.String(500), nullable=False)
    file_sha256: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    file_size: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    content_type: Mapped[str] = mapped_column(sa.String(100), nullable=False)

    generated_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    generated_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    superseded_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("document_exports.id", ondelete="SET NULL")
    )
    error_message: Mapped[str | None] = mapped_column(sa.Text)
    metadata_: Mapped[dict[str, object] | None] = mapped_column("metadata", postgresql.JSONB)


class ExportProfile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Perfil configurable de exportacion ERP (F010E)."""

    __tablename__ = "export_profiles"

    code: Mapped[str] = mapped_column(sa.String(60), unique=True, nullable=False)
    vendor: Mapped[str | None] = mapped_column(sa.String(60))
    description: Mapped[str | None] = mapped_column(sa.Text)
    version: Mapped[str] = mapped_column(sa.String(30), nullable=False, server_default="1")
    active: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=True, server_default=sa.text("true")
    )
    document_format: Mapped[DocumentFormat] = mapped_column(
        varchar_enum(DocumentFormat), nullable=False, server_default="XLSX"
    )
    column_order: Mapped[list[str] | None] = mapped_column(postgresql.JSONB)
    column_mapping: Mapped[dict[str, str] | None] = mapped_column(postgresql.JSONB)
    file_extension: Mapped[str] = mapped_column(
        sa.String(10), nullable=False, server_default="xlsx"
    )
    field_separator: Mapped[str | None] = mapped_column(sa.String(5))
    decimal_separator: Mapped[str | None] = mapped_column(sa.String(5))
    thousands_separator: Mapped[str | None] = mapped_column(sa.String(5))
    currency: Mapped[str | None] = mapped_column(sa.String(3))
    date_format: Mapped[str | None] = mapped_column(sa.String(30))
    document_number_format: Mapped[str | None] = mapped_column(sa.String(100))
    encoding: Mapped[str | None] = mapped_column(sa.String(30))
    requires_vendor_template: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.text("false")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="SET NULL")
    )


__all__ = [
    "DocumentExport",
    "ExportProfile",
    "OrganizationSettings",
]
