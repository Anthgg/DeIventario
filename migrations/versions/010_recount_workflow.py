"""010 recount workflow: reconteos ciegos (F007).

Revision ID: 010_recount_workflow
Revises: 009_exceptions_damages
Create Date: 2026-09-23

Migracion aditiva de F007:
  - inventory_recounts: status (VARCHAR + CHECK), expected_version,
    started_at, cancelled_at, cancelled_by (FK users SET NULL), cancel_reason.
  - Indice unico parcial: maximo UN reconteo abierto por campana.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "010_recount_workflow"
down_revision: str | None = "009_exceptions_damages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "inventory_recounts",
        sa.Column("status", sa.String(length=50), nullable=False, server_default="ASSIGNED"),
    )
    op.add_column(
        "inventory_recounts",
        sa.Column("expected_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
    )
    op.add_column(
        "inventory_recounts",
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "inventory_recounts",
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("inventory_recounts", sa.Column("cancelled_by", sa.Uuid(), nullable=True))
    op.add_column("inventory_recounts", sa.Column("cancel_reason", sa.Text(), nullable=True))

    op.create_check_constraint(
        op.f("ck_inventory_recounts_recountstatus"),
        "inventory_recounts",
        "status IN ('REQUESTED', 'ASSIGNED', 'IN_PROGRESS', 'COMPLETED', 'CANCELLED')",
    )
    op.create_foreign_key(
        op.f("fk_inventory_recounts_cancelled_by_users"),
        "inventory_recounts",
        "users",
        ["cancelled_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "uq_inventory_recounts_open_per_campaign",
        "inventory_recounts",
        ["inventory_campaign_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('REQUESTED', 'ASSIGNED', 'IN_PROGRESS')"),
    )


def downgrade() -> None:
    op.drop_index("uq_inventory_recounts_open_per_campaign", table_name="inventory_recounts")
    op.drop_constraint(
        op.f("fk_inventory_recounts_cancelled_by_users"),
        "inventory_recounts",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("ck_inventory_recounts_recountstatus"), "inventory_recounts", type_="check"
    )
    op.drop_column("inventory_recounts", "cancel_reason")
    op.drop_column("inventory_recounts", "cancelled_by")
    op.drop_column("inventory_recounts", "cancelled_at")
    op.drop_column("inventory_recounts", "started_at")
    op.drop_column("inventory_recounts", "expected_version")
    op.drop_column("inventory_recounts", "status")
