"""004 ensure all enum check constraints

Revision ID: 004_ensure_enum_checks
Revises: 003_enum_check_names
Create Date: 2026-09-21

La migracion 003 elimino por error una de las dos restricciones CHECK en las
tablas que tienen mas de un enum (inventory_count_sessions y
inventory_count_events): al recalcular el conjunto de nombres existentes dentro
de la misma tabla, trataba como obsoleta la restriccion recien creada.

Esta migracion garantiza el conjunto completo de CHECK declarado por los
modelos. Es idempotente (solo crea lo que falta, nunca elimina filas ni
restricciones validas) y deja el esquema convergido en cualquier entorno.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "004_ensure_enum_checks"
down_revision: str | None = "003_enum_check_names"
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

_CONSTRAINT_EXISTS = sa.text(
    "SELECT 1 FROM pg_constraint c "
    "JOIN pg_class t ON t.oid = c.conrelid "
    "JOIN pg_namespace n ON n.oid = t.relnamespace "
    "WHERE c.contype = 'c' AND n.nspname = 'public' "
    "AND t.relname = :table AND c.conname = :name"
)


def upgrade() -> None:
    connection = op.get_bind()
    for table_name, constraint_name, condition in EXPECTED:
        exists = connection.execute(
            _CONSTRAINT_EXISTS, {"table": table_name, "name": constraint_name}
        ).scalar()
        if not exists:
            op.create_check_constraint(op.f(constraint_name), table_name, condition)


def downgrade() -> None:
    for table_name, constraint_name, _condition in reversed(EXPECTED):
        op.drop_constraint(op.f(constraint_name), table_name, type_="check")
