"""013 document engine: branding, historial de documentos y perfiles ERP (F010).

Revision ID: 013_document_engine
Revises: 012_valuation_workflow
Create Date: 2026-09-24

Migracion aditiva de F010:
  - organization_settings: configuracion unica activa (razon social, marca,
    colores, footer, numeracion) con versionado optimista.
  - document_exports: historial inmutable de documentos oficiales generados
    (PDF/XLSX) con hash de fuente, hash de branding y superacion.
  - export_profiles: perfiles configurables de exportacion ERP.
  - NO se modifican tablas F001-F009 (los campos closed_at/closed_by de
    inventory_campaigns ya existen desde 001).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "013_document_engine"
down_revision: str | None = "012_valuation_workflow"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "organization_settings",
        sa.Column("legal_name", sa.String(length=255), nullable=True),
        sa.Column("tax_id", sa.String(length=50), nullable=True),
        sa.Column("tax_id_label", sa.String(length=50), nullable=True),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("phone", sa.String(length=50), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("website", sa.String(length=255), nullable=True),
        sa.Column("logo_path", sa.String(length=500), nullable=True),
        sa.Column("logo_sha256", sa.String(length=64), nullable=True),
        sa.Column("logo_media_type", sa.String(length=100), nullable=True),
        sa.Column("logo_original_name", sa.String(length=255), nullable=True),
        sa.Column("brand_color_primary", sa.String(length=20), nullable=True),
        sa.Column("brand_color_secondary", sa.String(length=20), nullable=True),
        sa.Column("brand_color_accent", sa.String(length=20), nullable=True),
        sa.Column(
            "currency_style",
            sa.String(length=50),
            nullable=False,
            server_default="SYMBOL_BEFORE",
        ),
        sa.Column(
            "date_format", sa.String(length=30), nullable=False, server_default="YYYY-MM-DD"
        ),
        sa.Column("footer_text", sa.Text(), nullable=True),
        sa.Column("footer_left", sa.Text(), nullable=True),
        sa.Column("footer_right", sa.Text(), nullable=True),
        sa.Column(
            "document_number_prefix",
            sa.String(length=30),
            nullable=False,
            server_default="DOC",
        ),
        sa.Column(
            "next_document_number", sa.Integer(), nullable=False, server_default=sa.text("1")
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organization_settings")),
    )
    op.create_check_constraint(
        op.f("ck_organization_settings_currencystyle"),
        "organization_settings",
        "currency_style IN ('SYMBOL_BEFORE', 'SYMBOL_AFTER', 'CODE_SUFFIX')",
    )
    op.create_check_constraint(
        op.f("ck_organization_settings_next_document_number_positive"),
        "organization_settings",
        "next_document_number >= 1",
    )
    op.create_foreign_key(
        op.f("fk_organization_settings_created_by_users"),
        "organization_settings",
        "users",
        ["created_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        op.f("fk_organization_settings_updated_by_users"),
        "organization_settings",
        "users",
        ["updated_by"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "document_exports",
        sa.Column(
            "module", sa.String(length=50), nullable=False, server_default="INVENTORY"
        ),
        sa.Column("document_type", sa.String(length=100), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("document_number", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="GENERATED"),
        sa.Column("format", sa.String(length=50), nullable=False),
        sa.Column("template_version", sa.String(length=30), nullable=False),
        sa.Column("module_version", sa.String(length=30), nullable=False),
        sa.Column("locale", sa.String(length=10), nullable=False, server_default="es"),
        sa.Column("entity_type", sa.String(length=50), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("inventory_campaign_id", sa.Uuid(), nullable=True),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("branding_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("file_path", sa.String(length=500), nullable=False),
        sa.Column("file_sha256", sa.String(length=64), nullable=False),
        sa.Column("file_size", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("generated_by", sa.Uuid(), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_by", sa.Uuid(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["inventory_campaign_id"],
            ["inventory_campaigns.id"],
            name=op.f("fk_document_exports_inventory_campaign_id_inventory_campaigns"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_exports")),
    )
    op.create_check_constraint(
        op.f("ck_document_exports_documentmodule"),
        "document_exports",
        "module IN ('INVENTORY')",
    )
    op.create_check_constraint(
        op.f("ck_document_exports_documentstatus"),
        "document_exports",
        "status IN ('GENERATED', 'SUPERSEDED', 'FAILED')",
    )
    op.create_check_constraint(
        op.f("ck_document_exports_documentformat"),
        "document_exports",
        "format IN ('PDF', 'XLSX')",
    )
    op.create_check_constraint(
        op.f("ck_document_exports_filesize_nonnegative"),
        "document_exports",
        "file_size >= 0",
    )
    op.create_foreign_key(
        op.f("fk_document_exports_generated_by_users"),
        "document_exports",
        "users",
        ["generated_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        op.f("fk_document_exports_superseded_by_document_exports"),
        "document_exports",
        "document_exports",
        ["superseded_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint(
        op.f("uq_document_exports_document_number"),
        "document_exports",
        ["document_number"],
    )
    op.create_index(
        op.f("ix_document_exports_status"), "document_exports", ["status"], unique=False
    )
    op.create_index(
        op.f("ix_document_exports_inventory_campaign_id"),
        "document_exports",
        ["inventory_campaign_id"],
        unique=False,
    )
    op.create_index(
        "ix_document_exports_entity_type_id",
        "document_exports",
        ["entity_type", "entity_id"],
        unique=False,
    )
    op.create_index(
        "uq_document_exports_idempotency",
        "document_exports",
        ["module", "document_type", "entity_id", "source_sha256", "branding_sha256", "template_version"],
        unique=True,
        postgresql_where=sa.text("status = 'GENERATED'"),
    )

    op.create_table(
        "export_profiles",
        sa.Column("code", sa.String(length=60), nullable=False),
        sa.Column("vendor", sa.String(length=60), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("version", sa.String(length=30), nullable=False, server_default="1"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "document_format",
            sa.String(length=50),
            nullable=False,
            server_default="XLSX",
        ),
        sa.Column("column_order", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("column_mapping", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "file_extension", sa.String(length=10), nullable=False, server_default="xlsx"
        ),
        sa.Column("field_separator", sa.String(length=5), nullable=True),
        sa.Column("decimal_separator", sa.String(length=5), nullable=True),
        sa.Column("thousands_separator", sa.String(length=5), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("date_format", sa.String(length=30), nullable=True),
        sa.Column("document_number_format", sa.String(length=100), nullable=True),
        sa.Column("encoding", sa.String(length=30), nullable=True),
        sa.Column(
            "requires_vendor_template",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_export_profiles")),
    )
    op.create_check_constraint(
        op.f("ck_export_profiles_documentformat"),
        "export_profiles",
        "document_format IN ('PDF', 'XLSX')",
    )
    op.create_foreign_key(
        op.f("fk_export_profiles_created_by_users"),
        "export_profiles",
        "users",
        ["created_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint(
        op.f("uq_export_profiles_code"), "export_profiles", ["code"]
    )


def downgrade() -> None:
    op.drop_constraint(op.f("uq_export_profiles_code"), "export_profiles", type_="unique")
    op.drop_constraint(
        op.f("fk_export_profiles_created_by_users"), "export_profiles", type_="foreignkey"
    )
    op.drop_constraint(
        op.f("ck_export_profiles_documentformat"), "export_profiles", type_="check"
    )
    op.drop_table("export_profiles")

    op.drop_index("uq_document_exports_idempotency", table_name="document_exports")
    op.drop_index(
        op.f("ix_document_exports_entity_type_id"), table_name="document_exports"
    )
    op.drop_index(
        op.f("ix_document_exports_inventory_campaign_id"), table_name="document_exports"
    )
    op.drop_index(op.f("ix_document_exports_status"), table_name="document_exports")
    op.drop_constraint(
        op.f("uq_document_exports_document_number"), "document_exports", type_="unique"
    )
    op.drop_constraint(
        op.f("fk_document_exports_superseded_by_document_exports"),
        "document_exports",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("fk_document_exports_generated_by_users"), "document_exports", type_="foreignkey"
    )
    op.drop_constraint(
        op.f("ck_document_exports_filesize_nonnegative"), "document_exports", type_="check"
    )
    op.drop_constraint(
        op.f("ck_document_exports_documentformat"), "document_exports", type_="check"
    )
    op.drop_constraint(
        op.f("ck_document_exports_documentstatus"), "document_exports", type_="check"
    )
    op.drop_constraint(
        op.f("ck_document_exports_documentmodule"), "document_exports", type_="check"
    )
    op.drop_constraint(
        op.f("fk_document_exports_inventory_campaign_id_inventory_campaigns"),
        "document_exports",
        type_="foreignkey",
    )
    op.drop_table("document_exports")

    op.drop_constraint(
        op.f("fk_organization_settings_updated_by_users"),
        "organization_settings",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("fk_organization_settings_created_by_users"),
        "organization_settings",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("ck_organization_settings_next_document_number_positive"),
        "organization_settings",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_organization_settings_currencystyle"), "organization_settings", type_="check"
    )
    op.drop_table("organization_settings")
