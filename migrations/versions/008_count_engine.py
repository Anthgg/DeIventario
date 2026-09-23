"""008 count engine: secuencias de eventos, undo y checks de totales.

Revision ID: 008_count_engine
Revises: 007_campaign_lifecycle
Create Date: 2026-09-22

Migracion aditiva del motor de conteo:
  - inventory_count_sessions: version, last_sequence, last_activity_at.
  - inventory_count_events: server_sequence, previous_quantity, reverses_event_id.
  - Unicidad de (session_id, server_sequence) y de reverses_event_id (undo unico).
  - Indice (session_id, product_id, server_sequence).
  - CHECKs de no-negatividad en inventory_count_totals.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "008_count_engine"
down_revision: str | None = "007_campaign_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "inventory_count_sessions",
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
    )
    op.add_column(
        "inventory_count_sessions",
        sa.Column("last_sequence", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "inventory_count_sessions",
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.add_column(
        "inventory_count_events", sa.Column("server_sequence", sa.BigInteger(), nullable=True)
    )
    op.add_column(
        "inventory_count_events",
        sa.Column("previous_quantity", sa.Numeric(18, 4), nullable=True),
    )
    op.add_column(
        "inventory_count_events", sa.Column("reverses_event_id", sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        op.f("fk_inventory_count_events_reverses_event_id_inventory_count_events"),
        "inventory_count_events",
        "inventory_count_events",
        ["reverses_event_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_inventory_count_events_session_sequence",
        "inventory_count_events",
        ["session_id", "server_sequence"],
        unique=True,
        postgresql_where=sa.text("server_sequence IS NOT NULL"),
    )
    op.create_index(
        "ix_inventory_count_events_session_product_sequence",
        "inventory_count_events",
        ["session_id", "product_id", "server_sequence"],
        unique=False,
    )
    op.create_index(
        "uq_inventory_count_events_reverses_event",
        "inventory_count_events",
        ["reverses_event_id"],
        unique=True,
        postgresql_where=sa.text("reverses_event_id IS NOT NULL"),
    )

    op.create_check_constraint(
        op.f("ck_inventory_count_totals_quantity_non_negative"),
        "inventory_count_totals",
        "quantity >= 0",
    )
    op.create_check_constraint(
        op.f("ck_inventory_count_totals_damaged_non_negative"),
        "inventory_count_totals",
        "damaged_quantity >= 0",
    )
    op.create_check_constraint(
        op.f("ck_inventory_count_totals_damaged_le_quantity"),
        "inventory_count_totals",
        "damaged_quantity <= quantity",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_inventory_count_totals_damaged_le_quantity"),
        "inventory_count_totals",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_inventory_count_totals_damaged_non_negative"),
        "inventory_count_totals",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_inventory_count_totals_quantity_non_negative"),
        "inventory_count_totals",
        type_="check",
    )
    op.drop_index("uq_inventory_count_events_reverses_event", table_name="inventory_count_events")
    op.drop_index(
        "ix_inventory_count_events_session_product_sequence", table_name="inventory_count_events"
    )
    op.drop_index(
        "uq_inventory_count_events_session_sequence", table_name="inventory_count_events"
    )
    op.drop_constraint(
        op.f("fk_inventory_count_events_reverses_event_id_inventory_count_events"),
        "inventory_count_events",
        type_="foreignkey",
    )
    op.drop_column("inventory_count_events", "reverses_event_id")
    op.drop_column("inventory_count_events", "previous_quantity")
    op.drop_column("inventory_count_events", "server_sequence")
    op.drop_column("inventory_count_sessions", "last_activity_at")
    op.drop_column("inventory_count_sessions", "last_sequence")
    op.drop_column("inventory_count_sessions", "version")
