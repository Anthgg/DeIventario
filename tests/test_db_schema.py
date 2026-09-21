"""Pruebas de integracion contra PostgreSQL.

Todas las escrituras ocurren dentro de una transaccion que SIEMPRE termina en
ROLLBACK: no queda ninguna fila permanente. No se ejecutan migraciones y no se
borra ni trunca ninguna tabla.
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from app.db.session import engine
from app.models import (
    InventoryCampaign,
    InventoryCountEvent,
    InventoryCountSession,
    InventoryCountTotal,
    InventorySnapshotItem,
    Product,
    User,
)
from app.models.enums import (
    CampaignStatus,
    CostSource,
    CountEventType,
    EventSource,
    SessionStatus,
    SessionType,
)

BASE_DIR = Path(__file__).resolve().parents[1]

EXPECTED_TABLES = (
    "alembic_version",
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
)

EXPECTED_CHECK_CONSTRAINTS = (
    "ck_inventory_assignments_assignmentstatus",
    "ck_inventory_campaigns_campaignstatus",
    "ck_inventory_count_events_counteventtype",
    "ck_inventory_count_events_eventsource",
    "ck_inventory_count_sessions_sessionstatus",
    "ck_inventory_count_sessions_sessiontype",
    "ck_inventory_reconciliations_reconciliationstatus",
    "ck_inventory_snapshot_items_costsource",
)


@pytest.fixture()
def connection() -> Iterator[Connection]:
    """Conexion con transaccion abierta que se revierte al finalizar."""
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            yield conn
        finally:
            transaction.rollback()


def _create_user(connection: Connection, display_name: str = "usuario-test") -> uuid.UUID:
    user_id = uuid.uuid4()
    connection.execute(sa.insert(User).values(id=user_id, display_name=display_name))
    return user_id


def _create_campaign(connection: Connection, code: str) -> uuid.UUID:
    campaign_id = uuid.uuid4()
    connection.execute(
        sa.insert(InventoryCampaign).values(
            id=campaign_id,
            code=code,
            name="Campana de prueba",
            status=CampaignStatus.DRAFT,
        )
    )
    return campaign_id


def _create_product(connection: Connection, internal_reference: str) -> uuid.UUID:
    product_id = uuid.uuid4()
    connection.execute(
        sa.insert(Product).values(
            id=product_id,
            internal_reference=internal_reference,
            name="Producto de prueba",
        )
    )
    return product_id


def _create_session(
    connection: Connection,
    campaign_id: uuid.UUID,
    user_id: uuid.UUID,
    session_number: int = 1,
) -> uuid.UUID:
    session_id = uuid.uuid4()
    connection.execute(
        sa.insert(InventoryCountSession).values(
            id=session_id,
            inventory_campaign_id=campaign_id,
            user_id=user_id,
            session_number=session_number,
            session_type=SessionType.INITIAL,
            status=SessionStatus.PENDING,
        )
    )
    return session_id


def test_public_schema_contains_expected_tables() -> None:
    with engine.connect() as connection:
        found = set(
            connection.execute(
                sa.text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                )
            )
            .scalars()
            .all()
        )
    assert set(EXPECTED_TABLES) == found


def test_alembic_version_matches_script_head() -> None:
    heads = set(ScriptDirectory(str(BASE_DIR / "migrations")).get_heads())
    with engine.connect() as connection:
        current = connection.execute(
            sa.text("SELECT version_num FROM alembic_version")
        ).scalar_one()
    assert current in heads


def test_enum_check_constraints_exist_in_database() -> None:
    with engine.connect() as connection:
        found = set(
            connection.execute(
                sa.text(
                    "SELECT c.conname FROM pg_constraint c "
                    "JOIN pg_class t ON t.oid = c.conrelid "
                    "JOIN pg_namespace n ON n.oid = t.relnamespace "
                    "WHERE c.contype = 'c' AND n.nspname = 'public'"
                )
            )
            .scalars()
            .all()
        )
    assert set(EXPECTED_CHECK_CONSTRAINTS) <= found


def test_client_event_uuid_is_idempotent_at_database_level(connection: Connection) -> None:
    event_uuid = uuid.uuid4()
    now = dt.datetime.now(dt.UTC)

    user_id = _create_user(connection)
    campaign_id = _create_campaign(connection, "TEST-IDEM-001")
    session_id = _create_session(connection, campaign_id, user_id)

    payload = {
        "session_id": session_id,
        "event_type": CountEventType.QR_SCAN,
        "source": EventSource.CAMERA,
        "client_event_uuid": event_uuid,
        "occurred_at": now,
        "received_at": now,
    }
    connection.execute(sa.insert(InventoryCountEvent).values(id=uuid.uuid4(), **payload))
    stored = connection.execute(
        sa.select(sa.func.count())
        .select_from(InventoryCountEvent)
        .where(InventoryCountEvent.client_event_uuid == event_uuid)
    ).scalar_one()
    assert stored == 1

    with pytest.raises(IntegrityError):
        connection.execute(sa.insert(InventoryCountEvent).values(id=uuid.uuid4(), **payload))


def test_count_totals_unique_per_session_and_product(connection: Connection) -> None:
    user_id = _create_user(connection)
    campaign_id = _create_campaign(connection, "TEST-TOTAL-001")
    session_id = _create_session(connection, campaign_id, user_id)
    product_id = _create_product(connection, "REF-TOTAL-001")

    connection.execute(
        sa.insert(InventoryCountTotal).values(
            id=uuid.uuid4(), session_id=session_id, product_id=product_id, quantity=1
        )
    )
    with pytest.raises(IntegrityError):
        connection.execute(
            sa.insert(InventoryCountTotal).values(
                id=uuid.uuid4(), session_id=session_id, product_id=product_id, quantity=2
            )
        )


def test_snapshot_items_unique_when_location_is_null(connection: Connection) -> None:
    campaign_id = _create_campaign(connection, "TEST-SNAP-001")
    product_id = _create_product(connection, "REF-SNAP-001")
    common = {
        "inventory_campaign_id": campaign_id,
        "product_id": product_id,
        "location_id": None,
        "internal_reference_snapshot": "REF-SNAP-001",
        "description_snapshot": "Producto de prueba",
        "expected_quantity": decimal.Decimal("1"),
        "cost_source": CostSource.COST,
        "currency_snapshot": "PEN",
    }
    connection.execute(sa.insert(InventorySnapshotItem).values(id=uuid.uuid4(), **common))
    with pytest.raises(IntegrityError):
        connection.execute(sa.insert(InventorySnapshotItem).values(id=uuid.uuid4(), **common))


def test_invalid_enum_value_is_rejected_by_check_constraint(connection: Connection) -> None:
    with pytest.raises(IntegrityError):
        connection.execute(
            sa.text(
                "INSERT INTO inventory_campaigns (id, code, name, status) "
                "VALUES (:id, :code, :name, :status)"
            ),
            {
                "id": uuid.uuid4(),
                "code": "TEST-CHECK-001",
                "name": "Estado invalido",
                "status": "NO_EXISTE",
            },
        )
