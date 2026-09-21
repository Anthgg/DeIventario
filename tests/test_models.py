"""Pruebas del modelo de datos (metadata de SQLAlchemy).

Verifican estructura y tipos SIN tocar la base de datos.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

import app.models  # noqa: F401  (registra todos los modelos en Base.metadata)
from app.db.base import Base

EXPECTED_TABLES = frozenset(
    {
        "audit_events",
        "contacts",
        "inventory_assignments",
        "inventory_campaigns",
        "inventory_count_events",
        "inventory_count_sessions",
        "inventory_count_totals",
        "inventory_damages",
        "inventory_extra_items",
        "inventory_reconciliations",
        "inventory_recounts",
        "inventory_snapshot_items",
        "inventory_unknown_codes",
        "locations",
        "product_supplier_refs",
        "products",
        "roles",
        "user_roles",
        "users",
    }
)

ENUM_COLUMNS: dict[str, tuple[str, ...]] = {
    "inventory_assignments": ("status",),
    "inventory_campaigns": ("status",),
    "inventory_count_events": ("event_type", "source"),
    "inventory_count_sessions": ("session_type", "status"),
    "inventory_reconciliations": ("status",),
    "inventory_snapshot_items": ("cost_source",),
}

NUMERIC_COLUMNS: dict[str, tuple[str, ...]] = {
    "inventory_count_events": ("delta_quantity", "set_quantity", "resulting_quantity"),
    "inventory_count_totals": ("quantity", "damaged_quantity"),
    "inventory_damages": ("quantity",),
    "inventory_extra_items": ("quantity",),
    "inventory_reconciliations": (
        "expected_quantity",
        "approved_physical_quantity",
        "difference_quantity",
        "damaged_quantity",
        "missing_quantity",
        "surplus_quantity",
        "effective_unit_cost",
        "missing_cost_value",
        "damage_cost_value",
        "surplus_cost_value",
        "affected_sale_value",
    ),
    "inventory_snapshot_items": (
        "expected_quantity",
        "sale_price_snapshot",
        "cost_snapshot",
        "consignment_cost_snapshot",
        "effective_cost_snapshot",
    ),
    "inventory_unknown_codes": ("quantity",),
    "products": ("sale_price", "cost", "consignment_cost"),
}

JSONB_COLUMNS: dict[str, str] = {
    "audit_events": "metadata",
    "inventory_count_events": "metadata",
}

# (tabla, columna) que deben ser TIMESTAMPTZ
TIMEZONE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("users", "created_at"),
    ("users", "updated_at"),
    ("inventory_campaigns", "deadline_at"),
    ("inventory_count_events", "occurred_at"),
    ("inventory_count_events", "received_at"),
    ("inventory_recounts", "completed_at"),
)

EXPECTED_UNIQUE_CONSTRAINTS: dict[str, tuple[str, ...]] = {
    "products": ("internal_reference",),
    "inventory_campaigns": ("code",),
    "inventory_count_events": ("client_event_uuid",),
    "inventory_count_totals": ("session_id", "product_id"),
    "inventory_count_sessions": ("inventory_campaign_id", "session_number"),
    "inventory_extra_items": ("session_id", "product_id"),
    "inventory_unknown_codes": ("session_id", "scanned_code"),
    "inventory_reconciliations": ("inventory_campaign_id", "product_id"),
    "product_supplier_refs": ("product_id", "contact_id"),
}

EXPECTED_INDEXES: tuple[tuple[str, str], ...] = (
    ("products", "name"),
    ("products", "active"),
    ("contacts", "external_ref"),
    ("inventory_campaigns", "status"),
    ("inventory_campaigns", "deadline_at"),
    ("inventory_assignments", "inventory_campaign_id"),
    ("inventory_assignments", "user_id"),
    ("inventory_count_sessions", "inventory_campaign_id"),
    ("inventory_count_sessions", "user_id"),
    ("inventory_count_events", "session_id"),
    ("inventory_count_events", "product_id"),
    ("inventory_count_events", "scanned_code"),
    ("inventory_count_events", "occurred_at"),
    ("audit_events", "inventory_campaign_id"),
    ("audit_events", "entity_type"),
    ("audit_events", "occurred_at"),
)


def test_expected_tables_are_registered() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_primary_keys_are_uuid() -> None:
    for table_name, table in Base.metadata.tables.items():
        primary_key = list(table.primary_key.columns)
        assert primary_key, f"{table_name} no tiene PK"
        for column in primary_key:
            assert isinstance(column.type, sa.Uuid), f"{table_name}.{column.name} no es UUID"


def test_money_and_quantity_columns_are_numeric() -> None:
    for table_name, columns in NUMERIC_COLUMNS.items():
        for column_name in columns:
            column_type = Base.metadata.tables[table_name].columns[column_name].type
            assert isinstance(column_type, sa.Numeric), f"{table_name}.{column_name} no es Numeric"
            assert not isinstance(column_type, sa.Float)
            assert (column_type.precision, column_type.scale) == (18, 4)


def test_jsonb_columns_are_configured() -> None:
    for table_name, column_name in JSONB_COLUMNS.items():
        column_type = Base.metadata.tables[table_name].columns[column_name].type
        assert isinstance(column_type, postgresql.JSONB)


def test_enums_are_varchar_not_native() -> None:
    for table_name, columns in ENUM_COLUMNS.items():
        for column_name in columns:
            column_type = Base.metadata.tables[table_name].columns[column_name].type
            assert isinstance(column_type, sa.Enum)
            assert column_type.native_enum is False
            assert column_type.length == 50
            compiled = str(column_type.compile(dialect=postgresql.dialect()))
            assert compiled.upper().startswith("VARCHAR")


def test_timestamps_are_timezone_aware() -> None:
    for table_name, column_name in TIMEZONE_COLUMNS:
        column_type = Base.metadata.tables[table_name].columns[column_name].type
        assert isinstance(column_type, sa.DateTime)
        assert column_type.timezone is True, f"{table_name}.{column_name} sin timezone"


def test_relationships_are_declared() -> None:
    from app.models import (
        InventoryCountEvent,
        InventoryCountSession,
        InventoryRecount,
        InventorySnapshotItem,
        Product,
        ProductSupplierRef,
        Role,
    )

    assert "users" in Role.__mapper__.relationships
    assert "supplier_refs" in Product.__mapper__.relationships
    assert "product" in ProductSupplierRef.__mapper__.relationships
    assert "product" in InventorySnapshotItem.__mapper__.relationships
    assert "campaign" in InventorySnapshotItem.__mapper__.relationships
    assert "session" in InventoryCountEvent.__mapper__.relationships
    assert "events" in InventoryCountSession.__mapper__.relationships
    assert "source_session" in InventoryRecount.__mapper__.relationships
    assert "resulting_session" in InventoryRecount.__mapper__.relationships


def test_unique_constraints_are_declared() -> None:
    for table_name, columns in EXPECTED_UNIQUE_CONSTRAINTS.items():
        table = Base.metadata.tables[table_name]
        declared = {
            tuple(sorted(constraint.columns.keys()))
            for constraint in table.constraints
            if isinstance(constraint, sa.UniqueConstraint)
        }
        assert tuple(sorted(columns)) in declared, f"{table_name}: falta unique {columns}"


def test_foreign_keys_and_delete_policy() -> None:
    events = Base.metadata.tables["inventory_count_events"]
    session_fk = next(iter(events.columns["session_id"].foreign_keys))
    assert session_fk.target_fullname == "inventory_count_sessions.id"
    assert session_fk.ondelete == "RESTRICT"

    campaigns = Base.metadata.tables["inventory_campaigns"]
    created_by_fk = next(iter(campaigns.columns["created_by"].foreign_keys))
    assert created_by_fk.ondelete == "SET NULL"

    sessions = Base.metadata.tables["inventory_count_sessions"]
    assignment_fk = next(iter(sessions.columns["assignment_id"].foreign_keys))
    assert assignment_fk.target_fullname == "inventory_assignments.id"

    recounts = Base.metadata.tables["inventory_recounts"]
    for column_name in ("source_session_id", "resulting_session_id"):
        target = next(iter(recounts.columns[column_name].foreign_keys)).target_fullname
        assert target == "inventory_count_sessions.id"


def test_indexes_for_frequent_lookups_are_declared() -> None:
    for table_name, column_name in EXPECTED_INDEXES:
        table = Base.metadata.tables[table_name]
        indexed = {tuple(index.columns.keys()) for index in table.indexes}
        assert (column_name,) in indexed, f"falta indice sobre {table_name}.{column_name}"


def test_snapshot_unique_indexes_handle_null_location() -> None:
    table = Base.metadata.tables["inventory_snapshot_items"]
    partial = {
        index.name: index
        for index in table.indexes
        if index.name
        in {
            "uq_inventory_snapshot_items_campaign_product_no_location",
            "uq_inventory_snapshot_items_campaign_product_location",
        }
    }
    assert len(partial) == 2, "faltan los indices unicos parciales del snapshot"
    assert all(index.unique for index in partial.values())

    no_location = partial["uq_inventory_snapshot_items_campaign_product_no_location"]
    with_location = partial["uq_inventory_snapshot_items_campaign_product_location"]
    assert tuple(no_location.columns.keys()) == ("inventory_campaign_id", "product_id")
    assert tuple(with_location.columns.keys()) == (
        "inventory_campaign_id",
        "product_id",
        "location_id",
    )
