"""002 enum check constraints

Revision ID: 002_enum_check_constraints
Revises: 4d7350a11464
Create Date: 2026-09-21

Los enums del dominio se almacenan como VARCHAR (native_enum=False). Para que
PostgreSQL valide los valores permitidos se anaden las restricciones CHECK
equivalentes a las declaradas por los modelos (``app.db.base.varchar_enum``).

Operacion puramente aditiva: solo ADD CONSTRAINT, sin borrados ni cambios de
tipo. El downgrade elimina unicamente estas restricciones.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "002_enum_check_constraints"
down_revision: str | None = "4d7350a11464"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _in(column: str, values: tuple[str, ...]) -> str:
    joined = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({joined})"


CHECKS: tuple[tuple[str, str, str], ...] = (
    (
        "ck_inventory_campaigns_campaignstatus",
        "inventory_campaigns",
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
        "ck_inventory_assignments_assignmentstatus",
        "inventory_assignments",
        _in("status", ("ACTIVE", "REVOKED", "COMPLETED")),
    ),
    (
        "ck_inventory_snapshot_items_costsource",
        "inventory_snapshot_items",
        _in("cost_source", ("COST", "CONSIGNMENT", "ZERO")),
    ),
    (
        "ck_inventory_count_sessions_sessiontype",
        "inventory_count_sessions",
        _in("session_type", ("INITIAL", "REASSIGNMENT", "RECOUNT", "SUPERVISOR_CHECK")),
    ),
    (
        "ck_inventory_count_sessions_sessionstatus",
        "inventory_count_sessions",
        _in("status", ("PENDING", "IN_PROGRESS", "SUBMITTED", "CANCELLED")),
    ),
    (
        "ck_inventory_count_events_counteventtype",
        "inventory_count_events",
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
        "ck_inventory_count_events_eventsource",
        "inventory_count_events",
        _in("source", ("CAMERA", "MANUAL", "OFFLINE_SYNC", "SYSTEM")),
    ),
    (
        "ck_inventory_reconciliations_reconciliationstatus",
        "inventory_reconciliations",
        _in(
            "status",
            ("PENDING", "MATCHED", "DIFFERENCE", "RECOUNT_REQUIRED", "UNDER_REVIEW", "APPROVED"),
        ),
    ),
)


def upgrade() -> None:
    for name, table_name, condition in CHECKS:
        op.create_check_constraint(name, table_name, condition)


def downgrade() -> None:
    for name, table_name, _condition in reversed(CHECKS):
        op.drop_constraint(name, table_name, type_="check")
