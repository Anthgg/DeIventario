"""Tests del motor de conteo (F005): QR, manuales, undo, lote, concurrencia, blind."""

from __future__ import annotations

import datetime as dt
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy import func, select

from app.db.session import SessionLocal
from app.models import InventoryCountEvent, InventoryCountTotal, InventoryUnknownCode, Product
from app.models.enums import CountEventType, EventSource
from app.services.counting import event_service
from tests.auth_helpers import override_auth
from tests.inventory_helpers import (
    PERMS_ASSIGN,
    PERMS_CREATE,
    PERMS_MONITOR,
    PERMS_READ,
    as_user,
    batch_products,
    cleanup_inventory_test_data,
    client,
    create_operator_user,
    create_standalone_product,
    create_test_source_batch,
)


def _future() -> str:
    return (dt.datetime.now(dt.UTC) + dt.timedelta(days=1)).isoformat()


def _ready_to_count(
    quantities: tuple[tuple[str, str], ...] = (("A", "5"), ("C", "2")),
) -> tuple[str, str, uuid.UUID]:
    """Campana IN_PROGRESS con operario asignado. Devuelve (campaign_id, batch_id, operator_id)."""
    cleanup_inventory_test_data()
    as_user(PERMS_CREATE, roles=("MANAGER",))
    batch_id = str(create_test_source_batch(quantities=quantities))
    campaign = client.post(
        "/api/v1/inventory/campaigns",
        json={
            "name": f"test-count-{uuid.uuid4().hex[:8]}",
            "source_import_batch_id": batch_id,
            "deadline_at": _future(),
        },
    ).json()
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
    return campaign["id"], batch_id, operator_id


def _as_operator(operator_id: uuid.UUID) -> None:
    override_auth({"inventory.count"}, user_id=operator_id)


def _start_session(campaign_id: str) -> dict[str, object]:
    response = client.post(f"/api/v1/inventory/campaigns/{campaign_id}/count-sessions/start")
    assert response.status_code == 200, response.text
    return response.json()


def _qr_item(
    product_id: uuid.UUID, uuid_value: uuid.UUID | None = None
) -> dict[str, object]:
    """Item QR reutilizable (individual o dentro de un lote)."""
    return {
        "client_event_uuid": str(uuid_value or uuid.uuid4()),
        "event_type": "QR_SCAN",
        "product_id": str(product_id),
    }


def _event(
    session_id: str,
    event_type: str,
    *,
    code: str | None = None,
    product_id: uuid.UUID | None = None,
    quantity: object | None = None,
    uuid_value: uuid.UUID | None = None,
    source: str | None = None,
) -> tuple[int, dict[str, Any]]:
    body: dict[str, object] = {
        "client_event_uuid": str(uuid_value or uuid.uuid4()),
        "event_type": event_type,
    }
    if code is not None:
        body["scanned_code"] = code
    if product_id is not None:
        body["product_id"] = str(product_id)
    if quantity is not None:
        body["quantity"] = str(quantity)
    if source is not None:
        body["source"] = source
    response = client.post(f"/api/v1/inventory/count-sessions/{session_id}/events", json=body)
    return response.status_code, (response.json() if response.content else {})


def _quantity(session_id: str, product_id: uuid.UUID) -> str:
    with SessionLocal() as db:
        total = db.execute(
            select(InventoryCountTotal).where(
                InventoryCountTotal.session_id == uuid.UUID(session_id),
                InventoryCountTotal.product_id == product_id,
            )
        ).scalar_one_or_none()
        return str(total.quantity) if total is not None else "0"


# ------------------------------ sesion / tipo --------------------------------


def test_start_session_initial_and_idempotent() -> None:
    campaign_id, _batch, operator_id = _ready_to_count()
    _as_operator(operator_id)
    first = _start_session(campaign_id)
    assert first["session_number"] == 1
    assert first["session_type"] == "INITIAL"
    assert first["status"] == "IN_PROGRESS"
    assert first["already_started"] is False
    second = _start_session(campaign_id)
    assert second["id"] == first["id"]
    assert second["already_started"] is True
    cleanup_inventory_test_data()


def test_start_requires_assignment_owner() -> None:
    campaign_id, _batch, _operator = _ready_to_count()
    other = create_operator_user()
    _as_operator(other)
    response = client.post(f"/api/v1/inventory/campaigns/{campaign_id}/count-sessions/start")
    assert response.status_code == 403
    cleanup_inventory_test_data()


# ---------------------------------- QR ---------------------------------------


def test_qr_flow_and_manual_operations() -> None:
    campaign_id, batch_id, operator_id = _ready_to_count()
    product_id, reference = batch_products(batch_id)[0]
    _as_operator(operator_id)
    session = _start_session(campaign_id)
    sid = str(session["id"])

    code, body = _event(sid, "QR_SCAN", code=reference)
    assert code == 200 and body["resulting_quantity"] == "1.0000"
    code, body = _event(sid, "QR_SCAN", code=reference)
    assert code == 200 and body["resulting_quantity"] == "2.0000"
    code, body = _event(sid, "MULTI_QR_SCAN", code=reference)
    assert code == 200 and body["resulting_quantity"] == "3.0000"
    code, body = _event(sid, "MANUAL_ADD", product_id=product_id, quantity=1)
    assert code == 200 and body["resulting_quantity"] == "4.0000"
    code, body = _event(sid, "MANUAL_SUBTRACT", product_id=product_id, quantity=1)
    assert code == 200 and body["resulting_quantity"] == "3.0000"
    code, body = _event(sid, "MANUAL_SET", product_id=product_id, quantity=10)
    assert code == 200 and body["resulting_quantity"] == "10.0000"
    assert body["previous_quantity"] == "3.0000"
    assert _quantity(sid, product_id) == "10.0000"
    cleanup_inventory_test_data()


def test_qr_ignores_client_quantity() -> None:
    campaign_id, batch_id, operator_id = _ready_to_count()
    _product_id, reference = batch_products(batch_id)[0]
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])
    code, body = _event(sid, "QR_SCAN", code=reference, quantity=99)
    assert code == 200
    assert body["resulting_quantity"] == "1.0000"
    cleanup_inventory_test_data()


def test_manual_rejects_decimal_quantity() -> None:
    campaign_id, batch_id, operator_id = _ready_to_count()
    product_id, _reference = batch_products(batch_id)[0]
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])
    code, _body = _event(sid, "MANUAL_ADD", product_id=product_id, quantity="1.5")
    assert code == 422
    cleanup_inventory_test_data()


def test_manual_subtract_below_zero_is_rejected() -> None:
    campaign_id, batch_id, operator_id = _ready_to_count()
    product_id, _reference = batch_products(batch_id)[0]
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])
    code, body = _event(sid, "MANUAL_SUBTRACT", product_id=product_id, quantity=1)
    assert code == 409
    assert body["detail"]["error"] == "COUNT_WOULD_BE_NEGATIVE"
    assert _quantity(sid, product_id) == "0"
    cleanup_inventory_test_data()


# ------------------------------- idempotencia --------------------------------


def test_idempotency_same_uuid() -> None:
    campaign_id, batch_id, operator_id = _ready_to_count()
    _product_id, reference = batch_products(batch_id)[0]
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])
    fixed = uuid.uuid4()
    first = _event(sid, "QR_SCAN", code=reference, uuid_value=fixed)
    second = _event(sid, "QR_SCAN", code=reference, uuid_value=fixed)
    assert first[1]["resulting_quantity"] == "1.0000"
    assert second[1]["already_processed"] is True
    assert second[1]["resulting_quantity"] == "1.0000"
    with SessionLocal() as db:
        count = db.execute(
            select(func.count())
            .select_from(InventoryCountEvent)
            .where(InventoryCountEvent.session_id == uuid.UUID(sid))
        ).scalar_one()
    assert count == 1
    cleanup_inventory_test_data()


def test_idempotency_conflict_same_uuid_other_product() -> None:
    campaign_id, batch_id, operator_id = _ready_to_count()
    products = batch_products(batch_id)
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])
    fixed = uuid.uuid4()
    _event(sid, "QR_SCAN", code=products[0][1], uuid_value=fixed)
    code, body = _event(sid, "QR_SCAN", code=products[1][1], uuid_value=fixed)
    assert code == 409
    assert body["detail"]["error"] == "IDEMPOTENCY_CONFLICT"
    cleanup_inventory_test_data()


# ----------------------------------- lote ------------------------------------


def test_batch_order_and_sequences() -> None:
    campaign_id, batch_id, operator_id = _ready_to_count()
    products = batch_products(batch_id)
    first, second = products[0][0], products[1][0]
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])
    payload = {
        "events": [
            _qr_item(first),
            _qr_item(first),
            _qr_item(second),
            {
                "client_event_uuid": str(uuid.uuid4()),
                "event_type": "MANUAL_ADD",
                "product_id": str(first),
                "quantity": "3",
            },
        ]
    }
    response = client.post(f"/api/v1/inventory/count-sessions/{sid}/events/batch", json=payload)
    assert response.status_code == 200, response.text
    sequences = [item["server_sequence"] for item in response.json()["items"]]
    assert sequences == [1, 2, 3, 4]
    assert _quantity(sid, first) == "5.0000"
    assert _quantity(sid, second) == "1.0000"
    cleanup_inventory_test_data()


def test_batch_duplicates_are_not_reapplied() -> None:
    campaign_id, batch_id, operator_id = _ready_to_count()
    first = batch_products(batch_id)[0][0]
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])
    fixed = uuid.uuid4()
    first_item = _qr_item(first, fixed)
    assert client.post(
        f"/api/v1/inventory/count-sessions/{sid}/events", json=first_item
    ).status_code == 200
    payload = {
        "events": [
            first_item,
            _qr_item(first),
        ]
    }
    response = client.post(f"/api/v1/inventory/count-sessions/{sid}/events/batch", json=payload)
    assert response.status_code == 200
    items = response.json()["items"]
    assert items[0]["already_processed"] is True
    assert items[1]["already_processed"] is False
    assert _quantity(sid, first) == "2.0000"
    cleanup_inventory_test_data()


def test_batch_is_atomic_on_error() -> None:
    campaign_id, batch_id, operator_id = _ready_to_count()
    first = batch_products(batch_id)[0][0]
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])
    payload = {
        "events": [
            _qr_item(first),
            {
                "client_event_uuid": str(uuid.uuid4()),
                "event_type": "MANUAL_SUBTRACT",
                "product_id": str(first),
                "quantity": "5",
            },
        ]
    }
    response = client.post(f"/api/v1/inventory/count-sessions/{sid}/events/batch", json=payload)
    assert response.status_code in (409, 400)
    assert _quantity(sid, first) == "0"
    with SessionLocal() as db:
        count = db.execute(
            select(func.count())
            .select_from(InventoryCountEvent)
            .where(InventoryCountEvent.session_id == uuid.UUID(sid))
        ).scalar_one()
    assert count == 0
    cleanup_inventory_test_data()


# --------------------------------- concurrencia ------------------------------


def test_concurrent_qr_scans_do_not_lose_increments() -> None:
    campaign_id, batch_id, operator_id = _ready_to_count()
    product_id, _reference = batch_products(batch_id)[0]
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])
    session_uuid = uuid.UUID(sid)
    total_concurrent = 20

    def worker() -> None:
        with SessionLocal() as db:
            event_service.process_event(
                db,
                session_id=session_uuid,
                actor_id=operator_id,
                payload=event_service.EventInput(
                    client_event_uuid=uuid.uuid4(),
                    event_type=CountEventType.QR_SCAN,
                    product_id=product_id,
                    source=EventSource.CAMERA,
                ),
            )

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(worker) for _ in range(total_concurrent)]
        for future in futures:
            future.result()

    assert _quantity(sid, product_id) == f"{total_concurrent}.0000"
    with SessionLocal() as db:
        sequences = list(
            db.execute(
                select(InventoryCountEvent.server_sequence).where(
                    InventoryCountEvent.session_id == session_uuid
                )
            ).scalars()
        )
    assert len(sequences) == total_concurrent
    assert len(set(sequences)) == total_concurrent
    cleanup_inventory_test_data()


# ----------------------------------- undo ------------------------------------


def test_undo_latest_event_only() -> None:
    campaign_id, batch_id, operator_id = _ready_to_count()
    product_id, _reference = batch_products(batch_id)[0]
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])
    _code, qr = _event(sid, "QR_SCAN", product_id=product_id)
    _code, added = _event(sid, "MANUAL_ADD", product_id=product_id, quantity=4)
    assert _quantity(sid, product_id) == "5.0000"

    # Con un evento posterior efectivo, el antiguo no se puede deshacer.
    blocked = client.post(
        f"/api/v1/inventory/count-sessions/{sid}/events/{qr['event_id']}/undo", json={}
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["error"] == "UNDO_NOT_LATEST_PRODUCT_EVENT"

    undo = client.post(
        f"/api/v1/inventory/count-sessions/{sid}/events/{added['event_id']}/undo", json={}
    )
    assert undo.status_code == 200, undo.text
    assert undo.json()["event_type"] == "UNDO"
    assert undo.json()["resulting_quantity"] == "1.0000"
    assert _quantity(sid, product_id) == "1.0000"

    # El evento original sigue intacto (historial inmutable).
    with SessionLocal() as db:
        original = db.get(InventoryCountEvent, uuid.UUID(str(added["event_id"])))
        assert original is not None and original.delta_quantity == 4
        assert original.resulting_quantity == 5

    # Al deshacerel ultimo, el anterior vuelve a ser el ultimo efectivo.
    second = client.post(
        f"/api/v1/inventory/count-sessions/{sid}/events/{qr['event_id']}/undo", json={}
    )
    assert second.status_code == 200, second.text
    assert second.json()["resulting_quantity"] == "0.0000"
    assert _quantity(sid, product_id) == "0.0000"
    cleanup_inventory_test_data()


def test_undo_is_idempotent() -> None:
    campaign_id, batch_id, operator_id = _ready_to_count()
    product_id, _reference = batch_products(batch_id)[0]
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])
    _code, added = _event(sid, "MANUAL_ADD", product_id=product_id, quantity=4)
    first = client.post(
        f"/api/v1/inventory/count-sessions/{sid}/events/{added['event_id']}/undo", json={}
    )
    second = client.post(
        f"/api/v1/inventory/count-sessions/{sid}/events/{added['event_id']}/undo", json={}
    )
    assert first.status_code == 200 and second.status_code == 200
    assert second.json()["already_processed"] is True
    assert _quantity(sid, product_id) == "0.0000"
    cleanup_inventory_test_data()


def test_manual_set_then_undo_restores_previous() -> None:
    campaign_id, batch_id, operator_id = _ready_to_count()
    product_id, _reference = batch_products(batch_id)[0]
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])
    _event(sid, "MANUAL_SET", product_id=product_id, quantity=3)
    _code, set_event = _event(sid, "MANUAL_SET", product_id=product_id, quantity=12)
    assert set_event["previous_quantity"] == "3.0000"
    assert set_event["resulting_quantity"] == "12.0000"
    undo = client.post(
        f"/api/v1/inventory/count-sessions/{sid}/events/{set_event['event_id']}/undo", json={}
    )
    assert undo.status_code == 200
    assert undo.json()["resulting_quantity"] == "3.0000"
    cleanup_inventory_test_data()


# --------------------------- extra / unknown / blind --------------------------


def test_extra_product_is_accepted() -> None:
    campaign_id, _batch, operator_id = _ready_to_count()
    extra_id = create_standalone_product(f"TEST-EXTRA-{uuid.uuid4().hex[:6]}")
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])
    code, body = _event(sid, "QR_SCAN", product_id=extra_id)
    assert code == 200
    assert body["resulting_quantity"] == "1.0000"
    assert _quantity(sid, extra_id) == "1.0000"
    items = client.get(f"/api/v1/inventory/count-sessions/{sid}/items").json()
    assert any(item["product_id"] == str(extra_id) for item in items)
    cleanup_inventory_test_data()


def test_unknown_code_is_accepted_without_fictitious_product() -> None:
    campaign_id, _batch, operator_id = _ready_to_count()
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])
    code, body = _event(sid, "QR_SCAN", code="ZZZ99999")
    assert code == 200
    assert body["resulting_quantity"] == "1.0000"
    assert body["product_id"] is None
    assert body["scanned_code"] == "ZZZ99999"
    with SessionLocal() as db:
        fake = db.execute(
            select(func.count())
            .select_from(Product)
            .where(Product.internal_reference == "ZZZ99999")
        ).scalar_one()
        unknown = db.execute(
            select(func.count())
            .select_from(InventoryUnknownCode)
            .where(
                InventoryUnknownCode.session_id == uuid.UUID(sid),
                InventoryUnknownCode.scanned_code == "ZZZ99999",
            )
        ).scalar_one()
    assert fake == 0  # no se creo ningun producto ficticio
    assert unknown == 1
    cleanup_inventory_test_data()


def test_finish_check_reports_missing_without_expected() -> None:
    campaign_id, batch_id, operator_id = _ready_to_count(
        quantities=(("A", "5"), ("B", "1"), ("C", "2"))
    )
    products = batch_products(batch_id)
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])
    _event(sid, "MANUAL_ADD", product_id=products[0][0], quantity=10)
    _event(sid, "MANUAL_ADD", product_id=products[1][0], quantity=1)

    check = client.get(f"/api/v1/inventory/count-sessions/{sid}/finish-check")
    assert check.status_code == 200
    body = check.json()
    assert body["has_missing"] is True
    assert len(body["missing_products"]) == 1
    assert body["missing_products"][0]["product_id"] == str(products[2][0])
    for forbidden in ("expected_quantity", "difference", "cost", "sale_price", "expected"):
        assert forbidden not in check.text
    cleanup_inventory_test_data()


def test_blind_mode_session_endpoints() -> None:
    campaign_id, batch_id, operator_id = _ready_to_count()
    product_id, reference = batch_products(batch_id)[0]
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])
    _event(sid, "QR_SCAN", code=reference)
    for path in ("", "/items", "/events"):
        response = client.get(f"/api/v1/inventory/count-sessions/{sid}{path}")
        assert response.status_code == 200
        text = response.text
        for forbidden in (
            "expected_quantity",
            "effective_cost",
            "sale_price",
            "cost_snapshot",
            "snapshot_sha256",
        ):
            assert forbidden not in text
    detail = client.get(f"/api/v1/inventory/count-sessions/{sid}").json()
    assert detail["actual_units_registered"] == "1.0000"
    assert detail["distinct_products_registered"] == 1
    assert detail["event_count"] == 1
    assert _quantity(sid, product_id) == "1.0000"
    cleanup_inventory_test_data()


def test_monitoring_lists_sessions_without_expected() -> None:
    campaign_id, batch_id, operator_id = _ready_to_count()
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])
    as_user(PERMS_MONITOR, roles=("SUPERVISOR",))
    response = client.get(f"/api/v1/inventory/campaigns/{campaign_id}/count-sessions")
    assert response.status_code == 200
    sessions = response.json()
    assert len(sessions) == 1
    assert sessions[0]["id"] == sid
    assert "expected_quantity" not in response.text
    assert "difference" not in response.text
    cleanup_inventory_test_data()


def test_operator_cannot_read_foreign_session() -> None:
    campaign_id, _batch, operator_id = _ready_to_count()
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])
    intruder = create_operator_user()
    _as_operator(intruder)
    assert client.get(f"/api/v1/inventory/count-sessions/{sid}").status_code == 403
    cleanup_inventory_test_data()


# ----------------------------- submit / deadline ------------------------------


def test_submit_requires_confirmation_when_missing() -> None:
    campaign_id, batch_id, operator_id = _ready_to_count(quantities=(("A", "5"), ("B", "1")))
    product_id = batch_products(batch_id)[0][0]
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])
    _event(sid, "MANUAL_ADD", product_id=product_id, quantity=1)

    blocked = client.post(
        f"/api/v1/inventory/count-sessions/{sid}/submit",
        json={"expected_version": 1, "confirm_missing": False},
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["error"] == "MISSING_PRODUCTS_CONFIRMATION_REQUIRED"
    assert "expected_quantity" not in blocked.text
    assert len(blocked.json()["detail"]["missing_products"]) == 1

    confirmed = client.post(
        f"/api/v1/inventory/count-sessions/{sid}/submit",
        json={"expected_version": 1, "confirm_missing": True},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "SUBMITTED"
    assert confirmed.json()["already_submitted"] is False

    # No se aceptan nuevos eventos.
    code, _body = _event(sid, "QR_SCAN", product_id=product_id)
    assert code == 409

    # Idempotente.
    again = client.post(
        f"/api/v1/inventory/count-sessions/{sid}/submit",
        json={"expected_version": 99, "confirm_missing": True},
    )
    assert again.status_code == 200
    assert again.json()["already_submitted"] is True

    as_user(PERMS_READ)
    campaign = client.get(f"/api/v1/inventory/campaigns/{campaign_id}").json()
    assert campaign["status"] == "SUBMITTED"
    cleanup_inventory_test_data()


def test_expired_campaign_rejects_events() -> None:
    campaign_id, batch_id, operator_id = _ready_to_count()
    product_id = batch_products(batch_id)[0][0]
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])

    with SessionLocal() as db:
        from app.models import InventoryCampaign

        campaign = db.get(InventoryCampaign, uuid.UUID(campaign_id))
        assert campaign is not None
        campaign.deadline_at = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
        db.commit()

    code, body = _event(sid, "MANUAL_ADD", product_id=product_id, quantity=1)
    assert code == 409
    assert body["detail"]["error"] == "CAMPAIGN_EXPIRED"
    assert _quantity(sid, product_id) == "0"
    with SessionLocal() as db:
        from app.models import InventoryCampaign

        refreshed = db.get(InventoryCampaign, uuid.UUID(campaign_id))
        assert refreshed is not None and refreshed.status.value == "EXPIRED"
    cleanup_inventory_test_data()


def test_event_immutability_endpoints() -> None:
    campaign_id, batch_id, operator_id = _ready_to_count()
    product_id = batch_products(batch_id)[0][0]
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])
    _code, event = _event(sid, "QR_SCAN", product_id=product_id)
    assert (
        client.patch(f"/api/v1/inventory/count-sessions/{sid}/events/{event['event_id']}").status_code
        in (404, 405)
    )
    assert (
        client.delete(f"/api/v1/inventory/count-sessions/{sid}/events/{event['event_id']}").status_code
        in (404, 405)
    )
    cleanup_inventory_test_data()


# ------------------------------- reasignacion --------------------------------


def test_reassignment_resets_session_and_preserves_history() -> None:
    campaign_id, batch_id, operator_id = _ready_to_count()
    product_id, _reference = batch_products(batch_id)[0]
    _as_operator(operator_id)
    first_session = _start_session(campaign_id)
    _event(str(first_session["id"]), "MANUAL_ADD", product_id=product_id, quantity=32)
    assert _quantity(str(first_session["id"]), product_id) == "32.0000"

    # Reasignacion a Pedro (campana en version 3 tras el start).
    pedro = create_operator_user()
    as_user(PERMS_ASSIGN, roles=("MANAGER",))
    reassigned = client.post(
        f"/api/v1/inventory/campaigns/{campaign_id}/assign",
        json={"user_id": str(pedro), "expected_version": 3},
    )
    assert reassigned.status_code == 200, reassigned.text
    assert reassigned.json()["reassigned"] is True

    # Sesion de Juan: cancelada pero con su historia intacta.
    _as_operator(operator_id)
    juan_session = client.get(f"/api/v1/inventory/count-sessions/{first_session['id']}").json()
    assert juan_session["status"] == "CANCELLED"
    assert _quantity(str(first_session["id"]), product_id) == "32.0000"
    with SessionLocal() as db:
        events = db.execute(
            select(func.count())
            .select_from(InventoryCountEvent)
            .where(InventoryCountEvent.session_id == uuid.UUID(str(first_session["id"])))
        ).scalar_one()
    assert events == 1

    # Pedro empieza desde 0 con una sesion nueva.
    _as_operator(pedro)
    pedro_session = _start_session(campaign_id)
    assert pedro_session["session_number"] == 2
    assert pedro_session["session_type"] == "REASSIGNMENT"
    assert pedro_session["id"] != first_session["id"]
    assert pedro_session["actual_units_registered"] == "0.0000"
    assert pedro_session["distinct_products_registered"] == 0
    assert pedro_session["event_count"] == 0
    assert _quantity(str(pedro_session["id"]), product_id) == "0"
    assert client.get(
        f"/api/v1/inventory/count-sessions/{pedro_session['id']}/items"
    ).json() == []
    cleanup_inventory_test_data()
