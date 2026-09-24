"""012 valuation workflow: valorizacion economica de la conciliacion (F009).

Revision ID: 012_valuation_workflow
Revises: 011_reconciliation_workflow
Create Date: 2026-09-23

Migracion aditiva de F009:
  - inventory_campaigns: valuation_calculated_at, valuation_source_sha256.
Los campos monetarios por producto (effective_unit_cost, missing_cost_value,
damage_cost_value, surplus_cost_value, affected_sale_value) ya existen en
inventory_reconciliations desde 001: NO se recrean aqui.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "012_valuation_workflow"
down_revision: str | None = "011_reconciliation_workflow"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "inventory_campaigns",
        sa.Column("valuation_calculated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "inventory_campaigns",
        sa.Column("valuation_source_sha256", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("inventory_campaigns", "valuation_source_sha256")
    op.drop_column("inventory_campaigns", "valuation_calculated_at")
