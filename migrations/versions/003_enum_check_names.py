"""003 reconcile enum check constraint names

Revision ID: 003_enum_check_names
Revises: 002_enum_check_constraints
Create Date: 2026-09-21

La migracion 002 se genero sin envolver el nombre en ``op.f()``. Como Alembic
aplica la convencion de nombres del proyecto tambien en las operaciones, el
nombre completo se prefijo otra vez (``ck_<tabla>_ck_<tabla>_...``) y
PostgreSQL trunco varios identificadores.

Esta migracion deja el esquema exactamente como lo declaran los modelos:
elimina los CHECK cuyo nombre no coincide y crea los que falten. Es idempotente
y NO toca filas de datos.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "003_enum_check_names"
down_revision: str | None = "002_enum_check_constraints"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _in(column: str, values: tuple[str, ...]) -> str:
    joined = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({joined})"


EXPECTED: tuple[tuple[str, str, str], ...] = (
    (
        "inventory_campaigns",
        "ck_inventory_campaigns_campaignstatus",
        _in(
            "status",
            (
                "DRAFT",
                "ASSIGNED",
                "IN_PROGRESS",
                "SUBMITTED",
                "RECOUNT",
                "UNDER_REVIEW",
                "APPROVED",
                "CLOSED",
                "EXPIRED",
                "CANCELLED",
            ),
        ),
    ),
    (
        "inventory_assignments",
        "ck_inventory_assignments_assignmentstatus",
        _in("status", ("ACTIVE", "REVOKED", "COMPLETED")),
    ),
    (
        "inventory_snapshot_items",
        "ck_inventory_snapshot_items_costsource",
        _in("cost_source", ("COST", "CONSIGNMENT", "ZERO")),
    ),
    (
        "inventory_count_sessions",
        "ck_inventory_count_sessions_sessiontype",
        _in("session_type", ("INITIAL", "REASSIGNMENT", "RECOUNT", "SUPERVISOR_CHECK")),
    ),
    (
        "inventory_count_sessions",
        "ck_inventory_count_sessions_sessionstatus",
        _in("status", ("PENDING", "IN_PROGRESS", "SUBMITTED", "CANCELLED")),
    ),
    (
        "inventory_count_events",
        "ck_inventory_count_events_counteventtype",
        _in(
            "event_type",
            (
                "QR_SCAN",
                "MULTI_QR_SCAN",
                "MANUAL_ADD",
                "MANUAL_SUBTRACT",
                "MANUAL_SET",
                "UNDO",
                "DAMAGE_ADD",
                "DAMAGE_SUBTRACT",
            ),
        ),
    ),
    (
        "inventory_count_events",
        "ck_inventory_count_events_eventsource",
        _in("source", ("CAMERA", "MANUAL", "OFFLINE_SYNC", "SYSTEM")),
    ),
    (
        "inventory_reconciliations",
        "ck_inventory_reconciliations_reconciliationstatus",
        _in(
            "status",
            ("PENDING", "MATCHED", "DIFFERENCE", "RECOUNT_REQUIRED", "UNDER_REVIEW", "APPROVED"),
        ),
    ),
)

_EXISTING_CHECK_NAMES = sa.text(
    "SELECT c.conname FROM pg_constraint c "
    "JOIN pg_class t ON t.oid = c.conrelid "
    "JOIN pg_namespace n ON n.oid = t.relnamespace "
    "WHERE c.contype = 'c' AND n.nspname = 'public' AND t.relname = :table"
)


def upgrade() -> None:
    connection = op.get_bind()
    for table_name, constraint_name, condition in EXPECTED:
        existing = set(
            connection.execute(_EXISTING_CHECK_NAMES, {"table": table_name}).scalars().all()
        )
        for stale_name in sorted(existing - {constraint_name}):
            op.drop_constraint(op.f(stale_name), table_name, type_="check")
        if constraint_name not in existing:
            op.create_check_constraint(op.f(constraint_name), table_name, condition)


def downgrade() -> None:
    for table_name, constraint_name, _condition in reversed(EXPECTED):
        op.drop_constraint(op.f(constraint_name), table_name, type_="check")
