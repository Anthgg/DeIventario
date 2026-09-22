"""007 campaign lifecycle: campos de campana, indices unicos y permiso expected.read.

Revision ID: 007_campaign_lifecycle
Revises: 006_auth_rbac
Create Date: 2026-09-21

Migracion aditiva:
  - inventory_campaigns: source_import_batch_id, source_stock_scope,
    snapshot_frozen_at, snapshot_sha256, version.
  - inventory_assignments: maximo UNA asignacion ACTIVE por campana.
  - locations: code unico cuando no es NULL.
  - permiso inventory.expected.read asignado solo a MANAGER y ADMIN.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "007_campaign_lifecycle"
down_revision: str | None = "006_auth_rbac"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EXPECTED_READ_ROLES = ("MANAGER", "ADMIN")
_EXPECTED_READ_CODE = "inventory.expected.read"


def upgrade() -> None:
    op.add_column(
        "inventory_campaigns", sa.Column("source_import_batch_id", sa.Uuid(), nullable=True)
    )
    op.add_column(
        "inventory_campaigns",
        sa.Column("source_stock_scope", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "inventory_campaigns",
        sa.Column("snapshot_frozen_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "inventory_campaigns", sa.Column("snapshot_sha256", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "inventory_campaigns",
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
    )
    op.create_foreign_key(
        op.f("fk_inventory_campaigns_source_import_batch_id_import_batches"),
        "inventory_campaigns",
        "import_batches",
        ["source_import_batch_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f("ck_inventory_campaigns_stockscope"),
        "inventory_campaigns",
        "source_stock_scope IN ('AGGREGATE', 'LOCATION')",
    )

    # Maximo UNA asignacion ACTIVE por campana (proteccion a nivel de base).
    op.create_index(
        "uq_inventory_assignments_active_per_campaign",
        "inventory_assignments",
        ["inventory_campaign_id"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE' AND revoked_at IS NULL"),
    )
    # code unico solo cuando no es NULL.
    op.create_index(
        "uq_locations_code_not_null",
        "locations",
        ["code"],
        unique=True,
        postgresql_where=sa.text("code IS NOT NULL"),
    )

    op.execute(
        "INSERT INTO permissions (id, code, name, created_at) "
        f"VALUES (gen_random_uuid(), '{_EXPECTED_READ_CODE}', 'Leer stock esperado', now()) "
        "ON CONFLICT (code) DO NOTHING"
    )
    for role_code in _EXPECTED_READ_ROLES:
        op.execute(
            "INSERT INTO role_permissions (role_id, permission_id, created_at) "
            "SELECT r.id, p.id, now() FROM roles r JOIN permissions p "
            f"ON p.code = '{_EXPECTED_READ_CODE}' "
            f"WHERE r.code = '{role_code}' "
            "ON CONFLICT (role_id, permission_id) DO NOTHING"
        )


def downgrade() -> None:
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        f"(SELECT id FROM permissions WHERE code = '{_EXPECTED_READ_CODE}')"
    )
    op.execute(f"DELETE FROM permissions WHERE code = '{_EXPECTED_READ_CODE}'")
    op.drop_index("uq_locations_code_not_null", table_name="locations")
    op.drop_index(
        "uq_inventory_assignments_active_per_campaign", table_name="inventory_assignments"
    )
    op.drop_constraint(
        op.f("ck_inventory_campaigns_stockscope"), "inventory_campaigns", type_="check"
    )
    op.drop_constraint(
        op.f("fk_inventory_campaigns_source_import_batch_id_import_batches"),
        "inventory_campaigns",
        type_="foreignkey",
    )
    op.drop_column("inventory_campaigns", "version")
    op.drop_column("inventory_campaigns", "snapshot_sha256")
    op.drop_column("inventory_campaigns", "snapshot_frozen_at")
    op.drop_column("inventory_campaigns", "source_stock_scope")
    op.drop_column("inventory_campaigns", "source_import_batch_id")
