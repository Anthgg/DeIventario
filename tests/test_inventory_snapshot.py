"""Tests de snapshot: preview, congelado, hash, inmutabilidad y blind mode."""

from __future__ import annotations

import datetime as dt
import decimal
import uuid

from sqlalchemy import func, select

from app.db.session import SessionLocal
from app.models import ImportBatch, InventoryCampaign, InventorySnapshotItem, Product, StockSnapshot
from app.models.enums import ImportBatchStatus, ImportBatchType
from tests.inventory_helpers import (
    PERMS_ASSIGN,
    PERMS_CREATE,
    PERMS_EXPECTED,
    PERMS_READ,
    as_user,
    cleanup_inventory_test_data,
    client,
    create_operator_user,
    create_test_location,
    create_test_source_batch,
)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _future() -> str:
    return (_now() + dt.timedelta(days=1)).isoformat()


def _create_campaign(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {"name": f"test-snap-{uuid.uuid4().hex[:8]}"}
    payload.update(overrides)
    response = client.post("/api/v1/inventory/campaigns", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def _snapshot_item_count(campaign_id: str) -> int:
    with SessionLocal() as db:
        return db.execute(
            select(func.count())
            .select_from(InventorySnapshotItem)
            .where(InventorySnapshotItem.inventory_campaign_id == uuid.UUID(campaign_id))
        ).scalar_one()


def _startable_campaign(
    *, quantities: tuple[tuple[str, str], ...], location_id: str | None = None
) -> tuple[dict[str, object], uuid.UUID, str]:
    as_user(PERMS_CREATE, roles=("MANAGER",))
    batch_id = str(create_test_source_batch(quantities=quantities))
    campaign = _create_campaign(
        source_import_batch_id=batch_id, deadline_at=_future(), location_id=location_id
    )
    operator_id = create_operator_user()
    as_user(PERMS_ASSIGN, roles=("MANAGER",))
    assigned = client.post(
        f"/api/v1/inventory/campaigns/{campaign['id']}/assign",
        json={"user_id": str(operator_id), "expected_version": 1},
    )
    assert assigned.status_code == 200, assigned.text
    return campaign, operator_id, batch_id


def _create_duplicate_source_batch() -> str:
    with SessionLocal() as db:
        batch = ImportBatch(
            import_type=ImportBatchType.PRODUCTS,
            source_filename=f"test-dup-{uuid.uuid4()}.xlsx",
            source_sha256=uuid.uuid4().hex + uuid.uuid4().hex,
            status=ImportBatchStatus.COMPLETED,
            completed_at=_now(),
        )
        db.add(batch)
        db.flush()
        product = Product(
            internal_reference=f"TEST-DUP-{uuid.uuid4().hex[:6]}",
            name="Duplicado",
            currency="PEN",
            active=True,
        )
        db.add(product)
        db.flush()
        for quantity in ("1", "2"):
            db.add(
                StockSnapshot(
                    import_batch_id=batch.id,
                    product_id=product.id,
                    quantity=decimal.Decimal(quantity),
                    source="TEST",
                )
            )
        db.commit()
        return str(batch.id)


def test_preview_does_not_persist() -> None:
    cleanup_inventory_test_data()
    campaign, _operator, _batch = _startable_campaign(quantities=(("A", "5"), ("B", "0")))
    as_user(PERMS_EXPECTED, roles=("MANAGER",))
    response = client.post(f"/api/v1/inventory/campaigns/{campaign['id']}/snapshot/preview")
    assert response.status_code == 200
    body = response.json()
    assert body["source_rows"] == 2
    assert body["candidate_products"] == 1
    assert body["zero_quantity_products"] == 1
    assert body["source_stock_scope"] == "AGGREGATE"
    assert _snapshot_item_count(str(campaign["id"])) == 0
    cleanup_inventory_test_data()


def test_start_freezes_snapshot_and_omits_zero_quantity() -> None:
    cleanup_inventory_test_data()
    campaign, _operator, batch_id = _startable_campaign(
        quantities=(("A", "5"), ("B", "0"), ("C", "3"))
    )
    as_user(PERMS_CREATE, roles=("MANAGER",))
    started = client.post(
        f"/api/v1/inventory/campaigns/{campaign['id']}/start", json={"expected_version": 2}
    )
    assert started.status_code == 200, started.text
    assert started.json()["status"] == "IN_PROGRESS"
    assert started.json()["already_started"] is False
    assert _snapshot_item_count(str(campaign["id"])) == 2  # B (0) omitido

    as_user(PERMS_EXPECTED, roles=("MANAGER",))
    snapshot = client.get(f"/api/v1/inventory/campaigns/{campaign['id']}/snapshot")
    assert snapshot.status_code == 200
    body = snapshot.json()
    assert body["total"] == 2
    assert body["snapshot_sha256"] and len(body["snapshot_sha256"]) == 64
    assert {item["internal_reference"].split("-")[0] for item in body["items"]} == {"TEST"}
    cleanup_inventory_test_data()


def test_negative_quantity_is_preserved_with_warning() -> None:
    cleanup_inventory_test_data()
    campaign, _operator, _batch = _startable_campaign(quantities=(("A", "5"), ("N", "-2")))
    as_user(PERMS_EXPECTED, roles=("MANAGER",))
    preview = client.post(f"/api/v1/inventory/campaigns/{campaign['id']}/snapshot/preview")
    assert preview.status_code == 200
    assert preview.json()["negative_quantity_products"] == 1
    assert "NEGATIVE_EXPECTED_QUANTITY" in preview.json()["warnings"]

    as_user(PERMS_CREATE, roles=("MANAGER",))
    started = client.post(
        f"/api/v1/inventory/campaigns/{campaign['id']}/start", json={"expected_version": 2}
    )
    assert started.status_code == 200
    as_user(PERMS_EXPECTED, roles=("MANAGER",))
    items = client.get(f"/api/v1/inventory/campaigns/{campaign['id']}/snapshot").json()["items"]
    assert any(item["expected_quantity"] == "-2.0000" for item in items)
    cleanup_inventory_test_data()


def test_cost_snapshots_and_effective_cost() -> None:
    cleanup_inventory_test_data()
    campaign, _operator, _batch = _startable_campaign(quantities=(("A", "1"), ("B", "1")))
    as_user(PERMS_CREATE, roles=("MANAGER",))
    client.post(f"/api/v1/inventory/campaigns/{campaign['id']}/start", json={"expected_version": 2})
    as_user(PERMS_EXPECTED, roles=("MANAGER",))
    items = client.get(f"/api/v1/inventory/campaigns/{campaign['id']}/snapshot").json()["items"]
    # El helper crea productos con cost=2 y consignment=1 -> efectivo COST.
    assert all(item["cost"] == "2.0000" for item in items)
    assert all(item["consignment_cost"] == "1.0000" for item in items)
    assert all(item["effective_cost"] == "2.0000" for item in items)
    assert all(item["cost_source"] == "COST" for item in items)
    cleanup_inventory_test_data()


def test_start_is_idempotent() -> None:
    cleanup_inventory_test_data()
    campaign, _operator, _batch = _startable_campaign(quantities=(("A", "5"),))
    as_user(PERMS_CREATE, roles=("MANAGER",))
    first = client.post(
        f"/api/v1/inventory/campaigns/{campaign['id']}/start", json={"expected_version": 2}
    )
    assert first.status_code == 200
    second = client.post(
        f"/api/v1/inventory/campaigns/{campaign['id']}/start", json={"expected_version": 2}
    )
    assert second.status_code == 200
    assert second.json()["already_started"] is True
    assert _snapshot_item_count(str(campaign["id"])) == 1
    cleanup_inventory_test_data()


def test_snapshot_hash_is_deterministic() -> None:
    cleanup_inventory_test_data()
    as_user(PERMS_CREATE, roles=("MANAGER",))
    batch_id = str(create_test_source_batch(quantities=(("A", "5"), ("C", "3"))))
    for _ in range(2):
        campaign = _create_campaign(source_import_batch_id=batch_id, deadline_at=_future())
        operator_id = create_operator_user()
        as_user(PERMS_ASSIGN, roles=("MANAGER",))
        client.post(
            f"/api/v1/inventory/campaigns/{campaign['id']}/assign",
            json={"user_id": str(operator_id), "expected_version": 1},
        )
        as_user(PERMS_CREATE, roles=("MANAGER",))
        started = client.post(
            f"/api/v1/inventory/campaigns/{campaign['id']}/start", json={"expected_version": 2}
        )
        assert started.status_code == 200, started.text
    with SessionLocal() as db:
        hashes = [
            value
            for value in db.execute(
                select(InventoryCampaign.snapshot_sha256).where(
                    InventoryCampaign.name.like("test-snap-%")
                )
            ).scalars()
            if value
        ]
    assert len(hashes) == 2
    assert len(set(hashes)) == 1


def test_snapshot_is_immutable_and_master_changes_do_not_alter_it() -> None:
    cleanup_inventory_test_data()
    campaign, _operator, _batch = _startable_campaign(quantities=(("A", "5"),))
    as_user(PERMS_CREATE, roles=("MANAGER",))
    client.post(f"/api/v1/inventory/campaigns/{campaign['id']}/start", json={"expected_version": 2})

    as_user(PERMS_EXPECTED, roles=("MANAGER",))
    before = client.get(f"/api/v1/inventory/campaigns/{campaign['id']}/snapshot").json()
    price_before = before["items"][0]["sale_price"]

    with SessionLocal() as db:
        product = db.execute(
            select(Product)
            .where(Product.internal_reference.like("TEST-A-%"))
            .limit(1)
        ).scalar_one()
        product.sale_price = decimal.Decimal("99.0000")
        product.name = "Producto modificado"
        db.commit()

    after = client.get(f"/api/v1/inventory/campaigns/{campaign['id']}/snapshot").json()
    assert after["items"][0]["sale_price"] == price_before
    assert after["snapshot_sha256"] == before["snapshot_sha256"]
    assert after["items"][0]["description"] != "Producto modificado"

    # Sin endpoints de mutacion del snapshot.
    assert client.patch(f"/api/v1/inventory/campaigns/{campaign['id']}/snapshot").status_code == 405
    cleanup_inventory_test_data()


def test_duplicate_source_is_blocked() -> None:
    cleanup_inventory_test_data()
    as_user(PERMS_CREATE, roles=("MANAGER",))
    batch_id = _create_duplicate_source_batch()
    campaign = _create_campaign(source_import_batch_id=batch_id, deadline_at=_future())
    operator_id = create_operator_user()
    as_user(PERMS_ASSIGN, roles=("MANAGER",))
    client.post(
        f"/api/v1/inventory/campaigns/{campaign['id']}/assign",
        json={"user_id": str(operator_id), "expected_version": 1},
    )
    as_user(PERMS_EXPECTED, roles=("MANAGER",))
    preview = client.post(f"/api/v1/inventory/campaigns/{campaign['id']}/snapshot/preview")
    assert "INCONSISTENT_SOURCE_SNAPSHOT" in preview.json()["warnings"]

    as_user(PERMS_CREATE, roles=("MANAGER",))
    started = client.post(
        f"/api/v1/inventory/campaigns/{campaign['id']}/start", json={"expected_version": 2}
    )
    assert started.status_code == 409
    assert _snapshot_item_count(str(campaign["id"])) == 0
    cleanup_inventory_test_data()


def test_aggregate_source_with_physical_location_requires_confirmation() -> None:
    cleanup_inventory_test_data()
    location_id = str(create_test_location())
    campaign, _operator, _batch = _startable_campaign(
        quantities=(("A", "5"),), location_id=location_id
    )
    as_user(PERMS_EXPECTED, roles=("MANAGER",))
    preview = client.post(f"/api/v1/inventory/campaigns/{campaign['id']}/snapshot/preview")
    assert preview.status_code == 200
    assert preview.json()["source_stock_scope"] == "AGGREGATE"
    assert "AGGREGATE_SOURCE_WITH_PHYSICAL_LOCATION" in preview.json()["warnings"]

    as_user(PERMS_CREATE, roles=("MANAGER",))
    blocked = client.post(
        f"/api/v1/inventory/campaigns/{campaign['id']}/start", json={"expected_version": 2}
    )
    assert blocked.status_code == 409

    confirmed = client.post(
        f"/api/v1/inventory/campaigns/{campaign['id']}/start",
        json={"expected_version": 2, "confirm_aggregate_source": True},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "IN_PROGRESS"
    cleanup_inventory_test_data()


# --------------------------------- blind mode --------------------------------


def _frozen_campaign() -> dict[str, object]:
    campaign, _operator, _batch = _startable_campaign(
        quantities=(("A", "5"), ("C", "2")), location_id=None
    )
    as_user(PERMS_CREATE, roles=("MANAGER",))
    started = client.post(
        f"/api/v1/inventory/campaigns/{campaign['id']}/start", json={"expected_version": 2}
    )
    assert started.status_code == 200, started.text
    return campaign


def test_operator_cannot_read_snapshot_but_can_read_campaign() -> None:
    cleanup_inventory_test_data()
    campaign = _frozen_campaign()
    as_user(PERMS_READ, roles=("OPERATOR",))
    detail = client.get(f"/api/v1/inventory/campaigns/{campaign['id']}")
    assert detail.status_code == 200
    assert "expected_quantity" not in detail.text
    assert "effective_cost" not in detail.text
    assert client.get(f"/api/v1/inventory/campaigns/{campaign['id']}/snapshot").status_code == 403
    cleanup_inventory_test_data()


def test_supervisor_cannot_read_snapshot() -> None:
    cleanup_inventory_test_data()
    campaign = _frozen_campaign()
    as_user(PERMS_READ, roles=("SUPERVISOR",))
    assert client.get(f"/api/v1/inventory/campaigns/{campaign['id']}").status_code == 200
    assert client.get(f"/api/v1/inventory/campaigns/{campaign['id']}/snapshot").status_code == 403
    cleanup_inventory_test_data()


def test_manager_and_admin_can_read_snapshot() -> None:
    cleanup_inventory_test_data()
    campaign = _frozen_campaign()
    for role in ("MANAGER", "ADMIN"):
        as_user(PERMS_EXPECTED, roles=(role,))
        response = client.get(f"/api/v1/inventory/campaigns/{campaign['id']}/snapshot")
        assert response.status_code == 200
        assert response.json()["total"] == 2
    cleanup_inventory_test_data()


def test_operator_cannot_read_expected_anywhere() -> None:
    cleanup_inventory_test_data()
    campaign = _frozen_campaign()
    as_user(PERMS_READ, roles=("OPERATOR",))
    assert client.post(
        f"/api/v1/inventory/campaigns/{campaign['id']}/snapshot/preview"
    ).status_code == 403
    assert "expected" not in client.get("/api/v1/inventory/my-assignments").text
    cleanup_inventory_test_data()
