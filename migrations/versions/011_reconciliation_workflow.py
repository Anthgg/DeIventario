"""011 reconciliation workflow: conciliacion Odoo vs conteos (F008).

Revision ID: 011_reconciliation_workflow
Revises: 010_recount_workflow
Create Date: 2026-09-23

Migracion aditiva de F008:
  - inventory_reconciliations: selected_session_id (FK sessions RESTRICT),
    selection_mode (VARCHAR + CHECK DEFAULT_SESSION/PRODUCT_OVERRIDE),
    selected_by (FK users SET NULL), selected_at, version.
  - inventory_campaigns: reconciliation_prepared_at, reconciliation_source_sha256.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "011_reconciliation_workflow"
down_revision: str | None = "010_recount_workflow"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "inventory_reconciliations",
        sa.Column("selected_session_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "inventory_reconciliations",
        sa.Column("selection_mode", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "inventory_reconciliations",
        sa.Column("selected_by", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "inventory_reconciliations",
        sa.Column("selected_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "inventory_reconciliations",
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
    )

    op.create_check_constraint(
        op.f("ck_inventory_reconciliations_selectionmode"),
        "inventory_reconciliations",
        "selection_mode IN ('DEFAULT_SESSION', 'PRODUCT_OVERRIDE')",
    )
    op.create_foreign_key(
        op.f("fk_inventory_reconciliations_selected_session_id_inventory_count_sessions"),
        "inventory_reconciliations",
        "inventory_count_sessions",
        ["selected_session_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        op.f("fk_inventory_reconciliations_selected_by_users"),
        "inventory_reconciliations",
        "users",
        ["selected_by"],
        ["id"],
        ondelete="SET NULL",
    )

    op.add_column(
        "inventory_campaigns",
        sa.Column("reconciliation_prepared_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "inventory_campaigns",
        sa.Column("reconciliation_source_sha256", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("inventory_campaigns", "reconciliation_source_sha256")
    op.drop_column("inventory_campaigns", "reconciliation_prepared_at")
    op.drop_constraint(
        op.f("fk_inventory_reconciliations_selected_by_users"),
        "inventory_reconciliations",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("fk_inventory_reconciliations_selected_session_id_inventory_count_sessions"),
        "inventory_reconciliations",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("ck_inventory_reconciliations_selectionmode"),
        "inventory_reconciliations",
        type_="check",
    )
    op.drop_column("inventory_reconciliations", "version")
    op.drop_column("inventory_reconciliations", "selected_at")
    op.drop_column("inventory_reconciliations", "selected_by")
    op.drop_column("inventory_reconciliations", "selection_mode")
    op.drop_column("inventory_reconciliations", "selected_session_id")
