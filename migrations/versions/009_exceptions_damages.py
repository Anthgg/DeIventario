"""009 exceptions: workflow de dannos, extras y codigos desconocidos.

Revision ID: 009_exceptions_damages
Revises: 008_count_engine
Create Date: 2026-09-23

Migracion aditiva de F006:
  - inventory_count_events: damage_delta_quantity, previous_damaged_quantity,
    resulting_damaged_quantity (NULL en eventos normales).
  - inventory_damages: product_id nullable (dano sobre UNKNOWN), action,
    scanned_code, event_id (FK RESTRICT + UNIQUE), checks de target exacto
    y quantity > 0.
  - inventory_unknown_codes: damaged_quantity + checks de no-negatividad y
    damaged <= quantity.
  - inventory_extra_items: check quantity >= 0.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "009_exceptions_damages"
down_revision: str | None = "008_count_engine"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- eventos de conteo: campos equivalentes de dano ---
    op.add_column(
        "inventory_count_events",
        sa.Column("damage_delta_quantity", sa.Numeric(18, 4), nullable=True),
    )
    op.add_column(
        "inventory_count_events",
        sa.Column("previous_damaged_quantity", sa.Numeric(18, 4), nullable=True),
    )
    op.add_column(
        "inventory_count_events",
        sa.Column("resulting_damaged_quantity", sa.Numeric(18, 4), nullable=True),
    )

    # --- inventory_damages: historial auditable de ajustes ---
    op.alter_column(
        "inventory_damages", "product_id", existing_type=sa.Uuid(), nullable=True
    )
    op.add_column(
        "inventory_damages",
        sa.Column("action", sa.String(length=50), nullable=False, server_default="ADD"),
    )
    op.add_column(
        "inventory_damages", sa.Column("scanned_code", sa.String(length=255), nullable=True)
    )
    op.add_column("inventory_damages", sa.Column("event_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_inventory_damages_event_id_inventory_count_events"),
        "inventory_damages",
        "inventory_count_events",
        ["event_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        op.f("uq_inventory_damages_event_id"), "inventory_damages", ["event_id"]
    )
    op.create_check_constraint(
        op.f("ck_inventory_damages_damageaction"),
        "inventory_damages",
        "action IN ('ADD', 'SUBTRACT')",
    )
    op.create_check_constraint(
        op.f("ck_inventory_damages_target_exactly_one"),
        "inventory_damages",
        "(product_id IS NOT NULL AND scanned_code IS NULL) "
        "OR (product_id IS NULL AND scanned_code IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_inventory_damages_quantity_positive"),
        "inventory_damages",
        "quantity > 0",
    )

    # --- inventory_unknown_codes: dano sobre codigos desconocidos ---
    op.add_column(
        "inventory_unknown_codes",
        sa.Column("damaged_quantity", sa.Numeric(18, 4), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        op.f("ck_inventory_unknown_codes_quantity_non_negative"),
        "inventory_unknown_codes",
        "quantity >= 0",
    )
    op.create_check_constraint(
        op.f("ck_inventory_unknown_codes_damaged_non_negative"),
        "inventory_unknown_codes",
        "damaged_quantity >= 0",
    )
    op.create_check_constraint(
        op.f("ck_inventory_unknown_codes_damaged_le_quantity"),
        "inventory_unknown_codes",
        "damaged_quantity <= quantity",
    )

    # --- inventory_extra_items: cantidad extra nunca negativa ---
    op.create_check_constraint(
        op.f("ck_inventory_extra_items_quantity_non_negative"),
        "inventory_extra_items",
        "quantity >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_inventory_extra_items_quantity_non_negative"),
        "inventory_extra_items",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_inventory_unknown_codes_damaged_le_quantity"),
        "inventory_unknown_codes",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_inventory_unknown_codes_damaged_non_negative"),
        "inventory_unknown_codes",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_inventory_unknown_codes_quantity_non_negative"),
        "inventory_unknown_codes",
        type_="check",
    )
    op.drop_column("inventory_unknown_codes", "damaged_quantity")

    op.drop_constraint(
        op.f("ck_inventory_damages_quantity_positive"), "inventory_damages", type_="check"
    )
    op.drop_constraint(
        op.f("ck_inventory_damages_target_exactly_one"), "inventory_damages", type_="check"
    )
    op.drop_constraint(
        op.f("ck_inventory_damages_damageaction"), "inventory_damages", type_="check"
    )
    op.drop_constraint(op.f("uq_inventory_damages_event_id"), "inventory_damages", type_="unique")
    op.drop_constraint(
        op.f("fk_inventory_damages_event_id_inventory_count_events"),
        "inventory_damages",
        type_="foreignkey",
    )
    op.drop_column("inventory_damages", "event_id")
    op.drop_column("inventory_damages", "scanned_code")
    op.drop_column("inventory_damages", "action")
    op.alter_column("inventory_damages", "product_id", existing_type=sa.Uuid(), nullable=False)

    op.drop_column("inventory_count_events", "resulting_damaged_quantity")
    op.drop_column("inventory_count_events", "previous_damaged_quantity")
    op.drop_column("inventory_count_events", "damage_delta_quantity")
