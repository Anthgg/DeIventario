"""Tests de excepciones F006: dano, extras, unknowns, evidencia y resumen.

Blind-safe: ningun endpoint administrativo puede filtrar expected ni costos.
"""

from __future__ import annotations

import decimal
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models import (
    AuditEvent,
    InventoryCountEvent,
    InventoryCountSession,
    InventoryCountTotal,
    InventoryDamage,
    InventoryExtraItem,
    InventoryUnknownCode,
    Product,
)
from app.models.enums import CountEventType, EventSource
from app.services.auth import audit_service
from app.services.counting import event_service
from tests.auth_helpers import override_auth
from tests.inventory_helpers import (
    PERMS_DAMAGE_REPORT,
    PERMS_DAMAGE_REVIEW,
    PERMS_RECONCILE,
    as_user,
    batch_products,
    cleanup_inventory_test_data,
    client,
    create_standalone_product,
    create_test_user,
)
from tests.test_count_engine import _as_operator, _quantity, _ready_to_count, _start_session

_PNG = b"\x89PNG\r\n\x1a\n" + b"fakepng-payload"
_JPEG = b"\xff\xd8\xff\xe0" + b"fakejpeg-payload"
_BLIND_FORBIDDEN = (
    "expected_quantity",
    "effective_cost",
    "sale_price",
    "cost_snapshot",
    "snapshot_sha256",
    "difference",
)


def _post_event(
    session_id: str,
    event_type: str,
    *,
    code: str | None = None,
    product_id: uuid.UUID | None = None,
    quantity: object | None = None,
    reason: str | None = None,
    observation: str | None = None,
    uuid_value: uuid.UUID | None = None,
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
    if reason is not None:
        body["reason"] = reason
    if observation is not None:
        body["observation"] = observation
    response = client.post(f"/api/v1/inventory/count-sessions/{session_id}/events", json=body)
    return response.status_code, (response.json() if response.content else {})


def _campaign_of(session_id: str) -> str:
    with SessionLocal() as db:
        session = db.get(InventoryCountSession, uuid.UUID(session_id))
        assert session is not None
        return str(session.inventory_campaign_id)


def _damaged(session_id: str, product_id: uuid.UUID) -> str:
    with SessionLocal() as db:
        total = db.execute(
            select(InventoryCountTotal).where(
                InventoryCountTotal.session_id == uuid.UUID(session_id),
                InventoryCountTotal.product_id == product_id,
            )
        ).scalar_one_or_none()
        return str(total.damaged_quantity) if total is not None else "0"


def _unknown_row(session_id: str, code: str) -> InventoryUnknownCode:
    with SessionLocal() as db:
        unknown = db.execute(
            select(InventoryUnknownCode).where(
                InventoryUnknownCode.session_id == uuid.UUID(session_id),
                InventoryUnknownCode.scanned_code == code,
            )
        ).scalar_one()
        return unknown


def _unknown_quantity(session_id: str, code: str) -> str:
    return str(_unknown_row(session_id, code).quantity)


def _extra_quantity(session_id: str, product_id: uuid.UUID) -> str | None:
    with SessionLocal() as db:
        extra = db.execute(
            select(InventoryExtraItem).where(
                InventoryExtraItem.session_id == uuid.UUID(session_id),
                InventoryExtraItem.product_id == product_id,
            )
        ).scalar_one_or_none()
        return str(extra.quantity) if extra is not None else None


def _damage_count(session_id: str) -> int:
    with SessionLocal() as db:
        return int(
            db.execute(
                select(func.count())
                .select_from(InventoryDamage)
                .where(InventoryDamage.session_id == uuid.UUID(session_id))
            ).scalar_one()
        )


def _audit_count(action: str, entity_id: uuid.UUID) -> int:
    with SessionLocal() as db:
        return int(
            db.execute(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == action, AuditEvent.entity_id == entity_id)
            ).scalar_one()
        )


def _event_by_type(session_id: str, event_type: CountEventType) -> InventoryCountEvent:
    with SessionLocal() as db:
        return db.execute(
            select(InventoryCountEvent)
            .where(
                InventoryCountEvent.session_id == uuid.UUID(session_id),
                InventoryCountEvent.event_type == event_type,
            )
            .order_by(InventoryCountEvent.server_sequence)
        ).scalars().all()[-1]


def _physical_events(session_id: str) -> list[InventoryCountEvent]:
    with SessionLocal() as db:
        return list(
            db.execute(
                select(InventoryCountEvent)
                .where(
                    InventoryCountEvent.session_id == uuid.UUID(session_id),
                    InventoryCountEvent.event_type.notin_(
                        (CountEventType.DAMAGE_ADD, CountEventType.DAMAGE_SUBTRACT)
                    ),
                    InventoryCountEvent.event_type != CountEventType.UNDO,
                )
                .order_by(InventoryCountEvent.server_sequence)
            ).scalars()
        )


def _setup_with_count() -> tuple[str, str, uuid.UUID, uuid.UUID]:
    """Campana lista + producto A contado (fisico 5). Devuelve ids utiles."""
    campaign_id, batch_id, operator_id = _ready_to_count()
    product_id, reference = batch_products(batch_id)[0]
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])
    _post_event(sid, "MANUAL_ADD", product_id=product_id, quantity=5)
    return sid, reference, product_id, operator_id


def _operator_with_evidence_perm(operator_id: uuid.UUID) -> None:
    override_auth(PERMS_DAMAGE_REPORT | {"inventory.count"}, user_id=operator_id)


# --------------------------------- dano basico --------------------------------


def test_damage_add_keeps_physical_and_registers_record() -> None:
    sid, _reference, product_id, _operator = _setup_with_count()
    status, body = _post_event(
        sid,
        "DAMAGE_ADD",
        product_id=product_id,
        quantity=1,
        reason="Estanteria seno",
        observation="Unidad 3",
    )
    assert status == 200, body
    assert body["resulting_quantity"] == "5.0000"  # fisico NO cambia
    assert body["quantity"] == "0.0000"
    assert body["damage_delta_quantity"] == "1.0000"
    assert body["previous_damaged_quantity"] == "0.0000"
    assert body["resulting_damaged_quantity"] == "1.0000"
    assert _quantity(sid, product_id) == "5.0000"
    assert _damaged(sid, product_id) == "1.0000"
    assert _damage_count(sid) == 1
    with SessionLocal() as db:
        record = db.execute(
            select(InventoryDamage).where(InventoryDamage.session_id == uuid.UUID(sid))
        ).scalar_one()
        assert record.action.value == "ADD"
        assert record.quantity == 1
        assert record.reason == "Estanteria seno"
        assert record.observation == "Unidad 3"
        assert record.event_id is not None
        assert record.product_id == product_id
        assert record.scanned_code is None
    cleanup_inventory_test_data()


def test_damage_subtract_restores_damaged() -> None:
    sid, _reference, product_id, _operator = _setup_with_count()
    _post_event(sid, "DAMAGE_ADD", product_id=product_id, quantity=2, reason="Roto")
    status, body = _post_event(
        sid, "DAMAGE_SUBTRACT", product_id=product_id, quantity=1, reason="Reparado"
    )
    assert status == 200, body
    assert body["damage_delta_quantity"] == "-1.0000"
    assert body["resulting_damaged_quantity"] == "1.0000"
    assert _quantity(sid, product_id) == "5.0000"
    assert _damaged(sid, product_id) == "1.0000"
    assert _damage_count(sid) == 2  # historial inmutable: 2 registros
    cleanup_inventory_test_data()


def test_damage_requires_reason() -> None:
    sid, _reference, product_id, _operator = _setup_with_count()
    missing = _post_event(sid, "DAMAGE_ADD", product_id=product_id, quantity=1)
    assert missing[0] == 422
    empty = _post_event(sid, "DAMAGE_ADD", product_id=product_id, quantity=1, reason="   ")
    assert empty[0] == 422
    too_long = _post_event(sid, "DAMAGE_ADD", product_id=product_id, quantity=1, reason="x" * 256)
    assert too_long[0] == 422
    assert _damage_count(sid) == 0
    assert _damaged(sid, product_id) == "0.0000"
    cleanup_inventory_test_data()


def test_damage_exceeds_physical_is_rejected() -> None:
    sid, _reference, product_id, _operator = _setup_with_count()
    _post_event(sid, "DAMAGE_ADD", product_id=product_id, quantity=5, reason="todo")
    status, body = _post_event(sid, "DAMAGE_ADD", product_id=product_id, quantity=1, reason="mas")
    assert status == 409
    assert body["detail"]["error"] == "DAMAGE_EXCEEDS_PHYSICAL_QUANTITY"
    assert _damaged(sid, product_id) == "5.0000"
    assert _damage_count(sid) == 1
    cleanup_inventory_test_data()


def test_damage_subtract_below_zero_is_rejected() -> None:
    sid, _reference, product_id, _operator = _setup_with_count()
    status, body = _post_event(
        sid, "DAMAGE_SUBTRACT", product_id=product_id, quantity=1, reason="n"
    )
    assert status == 409
    assert body["detail"]["error"] == "DAMAGE_WOULD_BE_NEGATIVE"
    assert _damaged(sid, product_id) == "0.0000"
    cleanup_inventory_test_data()


def test_manual_subtract_cannot_pass_damaged() -> None:
    sid, _reference, product_id, _operator = _setup_with_count()
    _post_event(sid, "DAMAGE_ADD", product_id=product_id, quantity=5, reason="todo danado")
    status, body = _post_event(sid, "MANUAL_SUBTRACT", product_id=product_id, quantity=1)
    assert status == 409
    assert body["detail"]["error"] == "DAMAGED_EXCEEDS_RESULTING_PHYSICAL_QUANTITY"
    assert _quantity(sid, product_id) == "5.0000"
    assert _damaged(sid, product_id) == "5.0000"
    cleanup_inventory_test_data()


def test_manual_physical_reduction_cannot_pass_damaged() -> None:
    """fisico=10, dano=4: SET 3 y SUBTRACT 7 quedarian por debajo del dano."""
    campaign_id, batch_id, operator_id = _ready_to_count()
    product_id, _reference = batch_products(batch_id)[0]
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])
    _post_event(sid, "MANUAL_ADD", product_id=product_id, quantity=10)
    _post_event(sid, "DAMAGE_ADD", product_id=product_id, quantity=4, reason="parcial")

    set_rejected = _post_event(sid, "MANUAL_SET", product_id=product_id, quantity=3)
    assert set_rejected[0] == 409
    assert set_rejected[1]["detail"]["error"] == "DAMAGED_EXCEEDS_RESULTING_PHYSICAL_QUANTITY"

    subtract_rejected = _post_event(sid, "MANUAL_SUBTRACT", product_id=product_id, quantity=7)
    assert subtract_rejected[0] == 409
    assert subtract_rejected[1]["detail"]["error"] == "DAMAGED_EXCEEDS_RESULTING_PHYSICAL_QUANTITY"

    # Ninguna cantidad se modifico.
    assert _quantity(sid, product_id) == "10.0000"
    assert _damaged(sid, product_id) == "4.0000"
    cleanup_inventory_test_data()


def test_manual_set_cannot_pass_damaged_quantity() -> None:
    """fisico=5, dano=3, MANUAL_SET 2 -> 409 sin modificar cantidades."""
    sid, _reference, product_id, _operator = _setup_with_count()
    _post_event(sid, "DAMAGE_ADD", product_id=product_id, quantity=3, reason="parcial")
    status, body = _post_event(sid, "MANUAL_SET", product_id=product_id, quantity=2)
    assert status == 409
    assert body["detail"]["error"] == "DAMAGED_EXCEEDS_RESULTING_PHYSICAL_QUANTITY"
    assert _quantity(sid, product_id) == "5.0000"
    assert _damaged(sid, product_id) == "3.0000"
    cleanup_inventory_test_data()


# ------------------------------ idempotencia dano -----------------------------


def test_damage_idempotency_signature_includes_reason() -> None:
    sid, _reference, product_id, _operator = _setup_with_count()
    fixed = uuid.uuid4()
    first = _post_event(
        sid, "DAMAGE_ADD", product_id=product_id, quantity=1, reason="R1", uuid_value=fixed
    )
    same = _post_event(
        sid, "DAMAGE_ADD", product_id=product_id, quantity=1, reason="R1", uuid_value=fixed
    )
    assert first[0] == 200 and same[0] == 200
    assert same[1]["already_processed"] is True
    conflict = _post_event(
        sid, "DAMAGE_ADD", product_id=product_id, quantity=1, reason="R2", uuid_value=fixed
    )
    assert conflict[0] == 409
    assert conflict[1]["detail"]["error"] == "IDEMPOTENCY_CONFLICT"
    assert _damage_count(sid) == 1
    assert _damaged(sid, product_id) == "1.0000"
    cleanup_inventory_test_data()


# --------------------------------- undo dano ----------------------------------


def test_damage_undo_requires_latest_in_damage_domain() -> None:
    sid, _reference, product_id, _operator = _setup_with_count()
    _status, first = _post_event(sid, "DAMAGE_ADD", product_id=product_id, quantity=2, reason="p")
    _status, second = _post_event(sid, "DAMAGE_ADD", product_id=product_id, quantity=1, reason="s")
    assert _damaged(sid, product_id) == "3.0000"

    blocked = client.post(
        f"/api/v1/inventory/count-sessions/{sid}/events/{first['event_id']}/undo", json={}
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["error"] == "UNDO_NOT_LATEST_DAMAGE_EVENT"

    undo = client.post(
        f"/api/v1/inventory/count-sessions/{sid}/events/{second['event_id']}/undo", json={}
    )
    assert undo.status_code == 200, undo.text
    assert undo.json()["event_type"] == "UNDO"
    assert undo.json()["resulting_damaged_quantity"] == "2.0000"
    assert _damaged(sid, product_id) == "2.0000"
    assert _quantity(sid, product_id) == "5.0000"

    # Historial inmutable: los registros de dano siguen existiendo.
    assert _damage_count(sid) == 2
    with SessionLocal() as db:
        original = db.get(InventoryCountEvent, uuid.UUID(str(second["event_id"])))
        assert original is not None and original.event_type is CountEventType.DAMAGE_ADD

    again = client.post(
        f"/api/v1/inventory/count-sessions/{sid}/events/{second['event_id']}/undo", json={}
    )
    assert again.status_code == 200 and again.json()["already_processed"] is True
    cleanup_inventory_test_data()


def test_physical_and_damage_undo_domains_are_independent() -> None:
    campaign_id, batch_id, operator_id = _ready_to_count()
    product_id, _reference = batch_products(batch_id)[0]
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])
    _post_event(sid, "QR_SCAN", product_id=product_id)
    _post_event(sid, "DAMAGE_ADD", product_id=product_id, quantity=1, reason="ok")
    _post_event(sid, "MANUAL_ADD", product_id=product_id, quantity=4)
    assert _quantity(sid, product_id) == "5.0000"
    assert _damaged(sid, product_id) == "1.0000"

    damage_event = _event_by_type(sid, CountEventType.DAMAGE_ADD)
    # El dano es el ultimo de SU dominio aunque haya fisicos posteriores.
    undo_damage = client.post(
        f"/api/v1/inventory/count-sessions/{sid}/events/{damage_event.id}/undo", json={}
    )
    assert undo_damage.status_code == 200, undo_damage.text
    assert _damaged(sid, product_id) == "0.0000"
    assert _quantity(sid, product_id) == "5.0000"

    # El QR antiguo tampoco es el ultimo fisico (lo es MANUAL_ADD).
    qr_event = _event_by_type(sid, CountEventType.QR_SCAN)
    blocked = client.post(
        f"/api/v1/inventory/count-sessions/{sid}/events/{qr_event.id}/undo", json={}
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["error"] == "UNDO_NOT_LATEST_PRODUCT_EVENT"
    cleanup_inventory_test_data()


def test_physical_undo_blocked_while_damaged_exceeds_resulting() -> None:
    campaign_id, batch_id, operator_id = _ready_to_count()
    product_id, _reference = batch_products(batch_id)[0]
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])
    _post_event(sid, "MANUAL_ADD", product_id=product_id, quantity=5)
    _post_event(sid, "DAMAGE_ADD", product_id=product_id, quantity=3, reason="d")

    manual = _event_by_type(sid, CountEventType.MANUAL_ADD)
    undo = client.post(f"/api/v1/inventory/count-sessions/{sid}/events/{manual.id}/undo", json={})
    assert undo.status_code == 409
    assert undo.json()["detail"]["error"] == "DAMAGED_EXCEEDS_RESULTING_PHYSICAL_QUANTITY"
    assert _quantity(sid, product_id) == "5.0000"
    assert _damaged(sid, product_id) == "3.0000"
    cleanup_inventory_test_data()


# --------------------------------- unknowns -----------------------------------


def test_unknown_scan_registers_without_blocking_or_fake_product() -> None:
    campaign_id, _batch, operator_id = _ready_to_count()
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])
    status, body = _post_event(sid, "QR_SCAN", code="UNK-99100")
    assert status == 200, body
    assert body["product_id"] is None
    assert body["scanned_code"] == "UNK-99100"
    items = client.get(f"/api/v1/inventory/count-sessions/{sid}/items").json()
    unknown_items = [item for item in items if item["kind"] == "UNKNOWN"]
    assert len(unknown_items) == 1
    assert unknown_items[0]["internal_reference"] == "UNK-99100"
    assert unknown_items[0]["damaged_quantity"] == "0.0000"
    with SessionLocal() as db:
        fake = db.execute(
            select(func.count())
            .select_from(Product)
            .where(Product.internal_reference == "UNK-99100")
        ).scalar_one()
    assert fake == 0
    cleanup_inventory_test_data()


def test_damage_and_manual_require_previously_scanned_unknown() -> None:
    campaign_id, _batch, operator_id = _ready_to_count()
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])
    damage = _post_event(sid, "DAMAGE_ADD", code="NEVER-SCAN", quantity=1, reason="r")
    assert damage[0] == 409
    assert damage[1]["detail"]["error"] == "UNKNOWN_CODE_NOT_REGISTERED"
    manual = _post_event(sid, "MANUAL_ADD", code="NEVER-SCAN", quantity=1)
    assert manual[0] == 409
    assert manual[1]["detail"]["error"] == "UNKNOWN_CODE_NOT_REGISTERED"
    with SessionLocal() as db:
        rows = db.execute(
            select(func.count())
            .select_from(InventoryUnknownCode)
            .where(InventoryUnknownCode.session_id == uuid.UUID(sid))
        ).scalar_one()
    assert rows == 0  # solo QR/MULTI_QR crean unknowns
    cleanup_inventory_test_data()


def test_damage_on_unknown_tracks_damaged_quantity() -> None:
    campaign_id, _batch, operator_id = _ready_to_count()
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])
    _post_event(sid, "QR_SCAN", code="UNK-DAMAGE")
    _post_event(sid, "QR_SCAN", code="UNK-DAMAGE")
    status, body = _post_event(sid, "DAMAGE_ADD", code="UNK-DAMAGE", quantity=1, reason="caido")
    assert status == 200, body
    assert body["product_id"] is None
    assert body["scanned_code"] == "UNK-DAMAGE"
    assert body["resulting_quantity"] == "2.0000"
    assert body["resulting_damaged_quantity"] == "1.0000"
    with SessionLocal() as db:
        unknown = db.execute(
            select(InventoryUnknownCode).where(
                InventoryUnknownCode.session_id == uuid.UUID(sid),
                InventoryUnknownCode.scanned_code == "UNK-DAMAGE",
            )
        ).scalar_one()
        assert unknown.quantity == 2
        assert unknown.damaged_quantity == 1
        record = db.execute(
            select(InventoryDamage).where(InventoryDamage.session_id == uuid.UUID(sid))
        ).scalar_one()
        assert record.product_id is None
        assert record.scanned_code == "UNK-DAMAGE"
    cleanup_inventory_test_data()


def test_unknown_multi_qr_scan_creates_unknown() -> None:
    campaign_id, _batch, operator_id = _ready_to_count()
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])
    status, body = _post_event(sid, "MULTI_QR_SCAN", code="UNK-MULTI")
    assert status == 200, body
    assert body["product_id"] is None
    assert body["resulting_quantity"] == "1.0000"
    assert _unknown_quantity(sid, "UNK-MULTI") == "1.0000"
    cleanup_inventory_test_data()


def test_unknown_manual_adjustments_after_scan() -> None:
    """Solo QR/MULTI crean el UNKNOWN; despues admite ADD/SUBTRACT/SET."""
    campaign_id, _batch, operator_id = _ready_to_count()
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])
    _post_event(sid, "QR_SCAN", code="UNK-MANUAL")
    assert _unknown_quantity(sid, "UNK-MANUAL") == "1.0000"

    added = _post_event(sid, "MANUAL_ADD", code="UNK-MANUAL", quantity=4)
    assert added[0] == 200 and added[1]["resulting_quantity"] == "5.0000"
    subtracted = _post_event(sid, "MANUAL_SUBTRACT", code="UNK-MANUAL", quantity=2)
    assert subtracted[0] == 200 and subtracted[1]["resulting_quantity"] == "3.0000"
    set_to = _post_event(sid, "MANUAL_SET", code="UNK-MANUAL", quantity=10)
    assert set_to[0] == 200 and set_to[1]["resulting_quantity"] == "10.0000"
    assert _unknown_quantity(sid, "UNK-MANUAL") == "10.0000"

    # Nada de esto crea un producto ficticio.
    with SessionLocal() as db:
        fake = db.execute(
            select(func.count())
            .select_from(Product)
            .where(Product.internal_reference == "UNK-MANUAL")
        ).scalar_one()
    assert fake == 0
    cleanup_inventory_test_data()


def test_unknown_manual_subtract_cannot_go_negative() -> None:
    campaign_id, _batch, operator_id = _ready_to_count()
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])
    _post_event(sid, "QR_SCAN", code="UNK-NEG")
    status, body = _post_event(sid, "MANUAL_SUBTRACT", code="UNK-NEG", quantity=2)
    assert status == 409
    assert body["detail"]["error"] == "COUNT_WOULD_BE_NEGATIVE"
    assert _unknown_quantity(sid, "UNK-NEG") == "1.0000"
    cleanup_inventory_test_data()


# ------------------------------- resolucion -----------------------------------


def test_resolve_unknown_is_idempotent_and_never_mutates_history() -> None:
    campaign_id, batch_id, operator_id = _ready_to_count()
    _product_counted, product_uncounted = (
        batch_products(batch_id)[0][0],
        batch_products(batch_id)[1][0],
    )
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])
    _post_event(sid, "MANUAL_ADD", product_id=_product_counted, quantity=5)
    _post_event(sid, "QR_SCAN", code="UNRES-1")
    _post_event(sid, "QR_SCAN", code="UNRES-1")

    as_user(PERMS_RECONCILE, roles=("MANAGER",))
    listing = client.get(f"/api/v1/inventory/campaigns/{campaign_id}/unknown-codes")
    assert listing.status_code == 200, listing.text
    row = next(item for item in listing.json() if item["scanned_code"] == "UNRES-1")
    unknown_id = row["id"]
    assert row["quantity"] == "2.0000"
    assert row["resolved_product_id"] is None

    first = client.post(
        f"/api/v1/inventory/unknown-codes/{unknown_id}/resolve",
        json={"product_id": str(product_uncounted)},
    )
    assert first.status_code == 200, first.text
    assert first.json()["already_resolved"] is False

    again = client.post(
        f"/api/v1/inventory/unknown-codes/{unknown_id}/resolve",
        json={"product_id": str(product_uncounted)},
    )
    assert again.status_code == 200
    assert again.json()["already_resolved"] is True

    # Nada del historial ni de los totales cambia con la resolucion.
    with SessionLocal() as db:
        unknown = db.get(InventoryUnknownCode, uuid.UUID(unknown_id))
        assert unknown is not None
        assert unknown.resolved_product_id == product_uncounted
        assert unknown.quantity == 2
        total = db.execute(
            select(InventoryCountTotal).where(
                InventoryCountTotal.session_id == uuid.UUID(sid),
                InventoryCountTotal.product_id == product_uncounted,
            )
        ).scalar_one_or_none()
        assert total is None  # el producto resuelto nunca se conto fisicamente
        events = db.execute(
            select(func.count())
            .select_from(InventoryCountEvent)
            .where(
                InventoryCountEvent.session_id == uuid.UUID(sid),
                InventoryCountEvent.scanned_code == "UNRES-1",
            )
        ).scalar_one()
        assert events == 2  # eventos originales intactos
        resolved_events = db.execute(
            select(func.count())
            .select_from(InventoryCountEvent)
            .where(
                InventoryCountEvent.session_id == uuid.UUID(sid),
                InventoryCountEvent.product_id == product_uncounted,
            )
        ).scalar_one()
        assert resolved_events == 0  # sin eventos retroactivos
    assert _audit_count(audit_service.UNKNOWN_CODE_RESOLVED, uuid.UUID(unknown_id)) == 1
    _as_operator(operator_id)
    cleanup_inventory_test_data()


def test_resolve_unknown_conflicts_and_inactive_product() -> None:
    sid, _reference, product_a, operator_id = _setup_with_count()
    _post_event(sid, "QR_SCAN", code="UNRES-2")
    _post_event(sid, "QR_SCAN", code="UNRES-3")
    other_product = create_standalone_product(f"TEST-OTHER-{uuid.uuid4().hex[:6]}")
    inactive_ref = f"TEST-INACTIVE-{uuid.uuid4().hex[:6]}"
    with SessionLocal() as db:
        inactive = Product(
            internal_reference=inactive_ref, name="Inactivo", currency="PEN", active=False
        )
        db.add(inactive)
        db.commit()
        inactive_id = inactive.id
    campaign_id = _campaign_of(sid)

    as_user(PERMS_RECONCILE, roles=("MANAGER",))
    listing = client.get(f"/api/v1/inventory/campaigns/{campaign_id}/unknown-codes").json()
    row2 = next(item for item in listing if item["scanned_code"] == "UNRES-2")
    row3 = next(item for item in listing if item["scanned_code"] == "UNRES-3")

    ok = client.post(
        f"/api/v1/inventory/unknown-codes/{row2['id']}/resolve",
        json={"product_id": str(product_a)},
    )
    assert ok.status_code == 200

    conflict = client.post(
        f"/api/v1/inventory/unknown-codes/{row2['id']}/resolve",
        json={"product_id": str(other_product)},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error"] == "UNKNOWN_ALREADY_RESOLVED"

    inactive_hit = client.post(
        f"/api/v1/inventory/unknown-codes/{row3['id']}/resolve",
        json={"product_id": str(inactive_id)},
    )
    assert inactive_hit.status_code == 409
    assert inactive_hit.json()["detail"]["error"] == "PRODUCT_INACTIVE"

    missing = client.post(
        f"/api/v1/inventory/unknown-codes/{uuid.uuid4()}/resolve",
        json={"product_id": str(product_a)},
    )
    assert missing.status_code == 404
    _as_operator(operator_id)
    cleanup_inventory_test_data()


def test_resolve_requires_reconcile_permission() -> None:
    sid, _reference, product_id, operator_id = _setup_with_count()
    _post_event(sid, "QR_SCAN", code="UNRES-4")
    campaign_id = _campaign_of(sid)
    as_user(PERMS_RECONCILE, roles=("MANAGER",))
    unknown_id = next(
        item["id"]
        for item in client.get(f"/api/v1/inventory/campaigns/{campaign_id}/unknown-codes").json()
        if item["scanned_code"] == "UNRES-4"
    )
    override_auth({"damage.review"})
    forbidden = client.post(
        f"/api/v1/inventory/unknown-codes/{unknown_id}/resolve",
        json={"product_id": str(product_id)},
    )
    assert forbidden.status_code == 403
    base = f"/api/v1/inventory/campaigns/{campaign_id}"
    assert client.get(f"{base}/unknown-codes").status_code == 403
    assert client.get(f"{base}/extras").status_code == 403
    assert client.get(f"{base}/exceptions").status_code == 403
    _as_operator(operator_id)
    cleanup_inventory_test_data()


# --------------------------------- extras -------------------------------------


def test_extra_admin_endpoint_lists_synced_quantity_blind() -> None:
    campaign_id, batch_id, operator_id = _ready_to_count()
    extra_ref = f"TEST-EXTRAADM-{uuid.uuid4().hex[:6]}"
    extra_id = create_standalone_product(extra_ref)
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])
    _post_event(sid, "QR_SCAN", product_id=extra_id)
    _post_event(sid, "QR_SCAN", product_id=extra_id)
    assert _quantity(sid, extra_id) == "2.0000"

    as_user(PERMS_RECONCILE, roles=("MANAGER",))
    response = client.get(f"/api/v1/inventory/campaigns/{campaign_id}/extras")
    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["product_id"] == str(extra_id)
    assert rows[0]["internal_reference"] == extra_ref
    assert rows[0]["quantity"] == "2.0000"
    assert rows[0]["source"] == "DIRECT_PRODUCT_SCAN"
    for forbidden in _BLIND_FORBIDDEN:
        assert forbidden not in response.text

    # Undo hasta cero: por defecto no se lista; include_zero si lo muestra.
    events = _physical_events(sid)
    assert len(events) == 2
    _as_operator(operator_id)
    for event in reversed(events):
        undo = client.post(
            f"/api/v1/inventory/count-sessions/{sid}/events/{event.id}/undo", json={}
        )
        assert undo.status_code == 200, undo.text
    assert _quantity(sid, extra_id) == "0.0000"

    as_user(PERMS_RECONCILE, roles=("MANAGER",))
    default_rows = client.get(f"/api/v1/inventory/campaigns/{campaign_id}/extras").json()
    with_zero = client.get(
        f"/api/v1/inventory/campaigns/{campaign_id}/extras?include_zero=true"
    ).json()
    assert all(row["product_id"] != str(extra_id) for row in default_rows)
    zero_row = next(row for row in with_zero if row["product_id"] == str(extra_id))
    assert zero_row["quantity"] == "0.0000"
    cleanup_inventory_test_data()


def test_extra_manual_set_zero_keeps_historical_row() -> None:
    campaign_id, _batch, operator_id = _ready_to_count()
    extra_id = create_standalone_product(f"TEST-EXTRAZ-{uuid.uuid4().hex[:6]}")
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])
    _post_event(sid, "MANUAL_ADD", product_id=extra_id, quantity=5)
    assert _extra_quantity(sid, extra_id) == "5.0000"

    status, body = _post_event(sid, "MANUAL_SET", product_id=extra_id, quantity=0)
    assert status == 200, body
    assert _quantity(sid, extra_id) == "0.0000"
    assert _extra_quantity(sid, extra_id) == "0.0000"  # fila historica, no borrada

    as_user(PERMS_RECONCILE, roles=("MANAGER",))
    default_rows = client.get(f"/api/v1/inventory/campaigns/{campaign_id}/extras").json()
    with_zero = client.get(
        f"/api/v1/inventory/campaigns/{campaign_id}/extras?include_zero=true"
    ).json()
    assert all(row["product_id"] != str(extra_id) for row in default_rows)
    assert any(row["product_id"] == str(extra_id) for row in with_zero)
    cleanup_inventory_test_data()


def test_extra_quantity_stays_synced_with_official_total() -> None:
    """QR, QR, SET 10, SUBTRACT 3, UNDO -> extra refleja el total final (10)."""
    campaign_id, _batch, operator_id = _ready_to_count()
    extra_id = create_standalone_product(f"TEST-EXTRASYNC-{uuid.uuid4().hex[:6]}")
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])
    _post_event(sid, "QR_SCAN", product_id=extra_id)
    _post_event(sid, "QR_SCAN", product_id=extra_id)
    _post_event(sid, "MANUAL_SET", product_id=extra_id, quantity=10)
    _status, subtracted = _post_event(sid, "MANUAL_SUBTRACT", product_id=extra_id, quantity=3)
    assert _quantity(sid, extra_id) == "7.0000"
    assert _extra_quantity(sid, extra_id) == "7.0000"

    undo = client.post(
        f"/api/v1/inventory/count-sessions/{sid}/events/{subtracted['event_id']}/undo",
        json={},
    )
    assert undo.status_code == 200, undo.text
    assert _quantity(sid, extra_id) == "10.0000"
    assert _extra_quantity(sid, extra_id) == "10.0000"
    cleanup_inventory_test_data()


def test_worker_items_never_reveal_extra_classification() -> None:
    """Obligatorio F006: el operador ve un PRODUCT normal, sin senales de extra."""
    campaign_id, _batch, operator_id = _ready_to_count()
    extra_id = create_standalone_product(f"TEST-EXTRABLI-{uuid.uuid4().hex[:6]}")
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])
    scanned = _post_event(sid, "QR_SCAN", product_id=extra_id)
    assert scanned[0] == 200

    response = client.get(f"/api/v1/inventory/count-sessions/{sid}/items")
    assert response.status_code == 200
    row = next(item for item in response.json() if item["product_id"] == str(extra_id))
    assert row["kind"] == "PRODUCT"
    assert set(row) == {
        "kind",
        "product_id",
        "internal_reference",
        "name",
        "quantity",
        "damaged_quantity",
    }
    for forbidden in (
        "is_extra",
        "extra=true",
        "warning",
        "out_of_snapshot",
        "expected=false",
        "expected_quantity",
    ):
        assert forbidden not in response.text
    cleanup_inventory_test_data()


# ------------------------------ lista de dannos -------------------------------


def test_damage_list_requires_damage_review_and_is_blind() -> None:
    campaign_id, batch_id, operator_id = _ready_to_count()
    product_id, reference = batch_products(batch_id)[0]
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])
    _post_event(sid, "QR_SCAN", code=reference)
    _post_event(
        sid, "DAMAGE_ADD", product_id=product_id, quantity=1, reason="Golpe", observation="obs"
    )

    override_auth({"inventory.count"})
    assert client.get(f"/api/v1/inventory/campaigns/{campaign_id}/damages").status_code == 403

    as_user(PERMS_DAMAGE_REVIEW, roles=("SUPERVISOR",))
    response = client.get(f"/api/v1/inventory/campaigns/{campaign_id}/damages")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    row = body["items"][0]
    assert row["quantity"] == "1.0000"
    assert row["action"] == "ADD"
    assert row["reason"] == "Golpe"
    assert row["observation"] == "obs"
    assert row["has_evidence"] is False
    assert row["internal_reference"] == reference
    assert row["session_number"] == 1
    for forbidden in _BLIND_FORBIDDEN:
        assert forbidden not in response.text
    cleanup_inventory_test_data()


# -------------------------------- evidencia -----------------------------------


def test_evidence_upload_download_and_validation() -> None:
    _campaign_id, batch_id, operator_id = _ready_to_count()
    product_id, _reference = batch_products(batch_id)[0]
    _as_operator(operator_id)
    sid = str(_start_session(_campaign_id)["id"])
    _post_event(sid, "MANUAL_ADD", product_id=product_id, quantity=2)
    status, created = _post_event(sid, "DAMAGE_ADD", product_id=product_id, quantity=1, reason="f")
    assert status == 200
    with SessionLocal() as db:
        record = db.execute(
            select(InventoryDamage).where(
                InventoryDamage.event_id == uuid.UUID(str(created["event_id"]))
            )
        ).scalar_one()
        damage_id = str(record.id)

    _operator_with_evidence_perm(operator_id)
    uploaded = client.post(
        f"/api/v1/inventory/damages/{damage_id}/evidence",
        files={"file": ("../../evil.png", _PNG, "image/png")},
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["has_evidence"] is True
    assert uploaded.json()["evidence_path"] == uuid.UUID(damage_id).hex + ".png"
    assert uploaded.json()["content_type"] == "image/png"

    duplicate = client.post(
        f"/api/v1/inventory/damages/{damage_id}/evidence",
        files={"file": ("otro.png", _PNG, "image/png")},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["error"] == "EVIDENCE_ALREADY_EXISTS"

    download = client.get(f"/api/v1/inventory/damages/{damage_id}/evidence")
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("image/png")
    assert download.content == _PNG
    assert _audit_count(audit_service.DAMAGE_EVIDENCE_ADDED, uuid.UUID(damage_id)) == 1

    # Registros sin evidencia: validaciones de formato.
    _post_event(sid, "DAMAGE_ADD", product_id=product_id, quantity=1, reason="otra")
    with SessionLocal() as db:
        other = db.execute(
            select(InventoryDamage).where(
                InventoryDamage.session_id == uuid.UUID(sid),
                InventoryDamage.evidence_path.is_(None),
            )
        ).scalars().all()
        other_id = str(other[0].id)

    mismatch = client.post(
        f"/api/v1/inventory/damages/{other_id}/evidence",
        files={"file": ("x.jpg", _PNG, "image/jpeg")},
    )
    assert mismatch.status_code == 422
    assert mismatch.json()["detail"]["error"] == "EVIDENCE_CONTENT_MISMATCH"

    invalid = client.post(
        f"/api/v1/inventory/damages/{other_id}/evidence",
        files={"file": ("x.png", b"not-an-image", "image/png")},
    )
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["error"] == "EVIDENCE_INVALID_CONTENT"

    unsupported = client.post(
        f"/api/v1/inventory/damages/{other_id}/evidence",
        files={"file": ("x.png", _PNG, "text/plain")},
    )
    assert unsupported.status_code == 415
    assert unsupported.json()["detail"]["error"] == "EVIDENCE_UNSUPPORTED_MEDIA"

    missing = client.post(
        f"/api/v1/inventory/damages/{uuid.uuid4()}/evidence",
        files={"file": ("x.png", _PNG, "image/png")},
    )
    assert missing.status_code == 404

    not_found = client.get(f"/api/v1/inventory/damages/{uuid.uuid4()}/evidence")
    assert not_found.status_code == 404
    cleanup_inventory_test_data()


def test_evidence_file_is_removed_when_database_commit_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign_id, batch_id, operator_id = _ready_to_count()
    product_id, _reference = batch_products(batch_id)[0]
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])
    _post_event(sid, "MANUAL_ADD", product_id=product_id, quantity=2)
    status, created = _post_event(
        sid, "DAMAGE_ADD", product_id=product_id, quantity=1, reason="rollback evidence"
    )
    assert status == 200
    with SessionLocal() as db:
        record = db.execute(
            select(InventoryDamage).where(
                InventoryDamage.event_id == uuid.UUID(str(created["event_id"]))
            )
        ).scalar_one()
        damage_id = record.id

    _operator_with_evidence_perm(operator_id)
    target = Path(get_settings().EVIDENCE_DIR).resolve() / f"{damage_id.hex}.png"

    def fail_commit(_session: Session) -> None:
        raise RuntimeError("simulated database commit failure")

    with monkeypatch.context() as patch:
        patch.setattr(Session, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="simulated database commit failure"):
            client.post(
                f"/api/v1/inventory/damages/{damage_id}/evidence",
                files={"file": ("proof.png", _PNG, "image/png")},
            )

    assert not target.exists()
    with SessionLocal() as db:
        record = db.execute(
            select(InventoryDamage).where(InventoryDamage.id == damage_id)
        ).scalar_one()
        assert record.evidence_path is None
    cleanup_inventory_test_data()


def test_evidence_accepts_webp_and_rejects_oversized_file() -> None:
    from app.core.config import get_settings

    webp = b"RIFF" + (0).to_bytes(4, "little") + b"WEBP" + b"fake-webp-payload"
    campaign_id, batch_id, operator_id = _ready_to_count()
    product_id, _reference = batch_products(batch_id)[0]
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])
    _post_event(sid, "MANUAL_ADD", product_id=product_id, quantity=3)

    _status, first = _post_event(
        sid, "DAMAGE_ADD", product_id=product_id, quantity=1, reason="webp"
    )
    _status, second = _post_event(
        sid, "DAMAGE_ADD", product_id=product_id, quantity=1, reason="grande"
    )
    with SessionLocal() as db:
        by_event = {
            str(row.event_id): str(row.id)
            for row in db.execute(
                select(InventoryDamage).where(InventoryDamage.session_id == uuid.UUID(sid))
            ).scalars()
        }
    webp_id = by_event[str(uuid.UUID(str(first["event_id"])))]
    big_id = by_event[str(uuid.UUID(str(second["event_id"])))]

    _operator_with_evidence_perm(operator_id)
    uploaded = client.post(
        f"/api/v1/inventory/damages/{webp_id}/evidence",
        files={"file": ("fotowebp", webp, "image/webp")},
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["content_type"] == "image/webp"
    download = client.get(f"/api/v1/inventory/damages/{webp_id}/evidence")
    assert download.status_code == 200
    assert download.content == webp

    max_bytes = get_settings().MAX_EVIDENCE_MB * 1024 * 1024
    oversized = b"\x89PNG\r\n\x1a\n" + b"a" * (max_bytes + 1)
    too_big = client.post(
        f"/api/v1/inventory/damages/{big_id}/evidence",
        files={"file": ("grande.png", oversized, "image/png")},
    )
    assert too_big.status_code == 413
    assert too_big.json()["detail"]["error"] == "EVIDENCE_TOO_LARGE"
    with SessionLocal() as db:
        record = db.get(InventoryDamage, uuid.UUID(big_id))
        assert record is not None and record.evidence_path is None
    cleanup_inventory_test_data()


def test_evidence_permission_rules() -> None:
    campaign_id, batch_id, operator_id = _ready_to_count()
    product_id, _reference = batch_products(batch_id)[0]
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])
    _post_event(sid, "MANUAL_ADD", product_id=product_id, quantity=3)
    _post_event(sid, "DAMAGE_ADD", product_id=product_id, quantity=1, reason="r1")
    _post_event(sid, "DAMAGE_ADD", product_id=product_id, quantity=1, reason="r2")
    with SessionLocal() as db:
        records = list(
            db.execute(
                select(InventoryDamage)
                .where(InventoryDamage.session_id == uuid.UUID(sid))
                .order_by(InventoryDamage.created_at)
            ).scalars()
        )
        open_damage_id = str(records[0].id)
        closed_damage_id = str(records[1].id)

    # Dueno de la sesion activa con damage.report: permitido.
    _operator_with_evidence_perm(operator_id)
    owner = client.post(
        f"/api/v1/inventory/damages/{open_damage_id}/evidence",
        files={"file": ("a.png", _PNG, "image/png")},
    )
    assert owner.status_code == 200, owner.text

    # El dueno tambien LEE su evidencia aunque solo tenga inventory.count.
    _as_operator(operator_id)
    owner_read = client.get(f"/api/v1/inventory/damages/{open_damage_id}/evidence")
    assert owner_read.status_code == 200
    assert owner_read.content == _PNG
    _operator_with_evidence_perm(operator_id)

    # Termino la sesion: el dueno ya no puede subir (no esta IN_PROGRESS).
    _as_operator(operator_id)
    submitted = client.post(
        f"/api/v1/inventory/count-sessions/{sid}/submit",
        json={"expected_version": 1, "confirm_missing": True},
    )
    assert submitted.status_code == 200, submitted.text

    _operator_with_evidence_perm(operator_id)
    closed = client.post(
        f"/api/v1/inventory/damages/{closed_damage_id}/evidence",
        files={"file": ("b.png", _PNG, "image/png")},
    )
    assert closed.status_code == 403

    # Un revisor con damage.review puede subir en cualquier estado.
    as_user(PERMS_DAMAGE_REVIEW, roles=("SUPERVISOR",))
    reviewer = client.post(
        f"/api/v1/inventory/damages/{closed_damage_id}/evidence",
        files={"file": ("c.png", _JPEG, "image/jpeg")},
    )
    assert reviewer.status_code == 200, reviewer.text
    assert reviewer.json()["content_type"] == "image/jpeg"

    # Tercero sin permisos relevantes.
    stranger = create_test_user()
    override_auth({"inventory.count"}, user_id=stranger)
    denied = client.post(
        f"/api/v1/inventory/damages/{open_damage_id}/evidence",
        files={"file": ("d.png", _PNG, "image/png")},
    )
    assert denied.status_code == 403
    cleanup_inventory_test_data()


# --------------------------- submit y finish-check -----------------------------


def test_submit_not_blocked_by_exceptions_and_records_ready_audit() -> None:
    campaign_id, batch_id, operator_id = _ready_to_count(
        quantities=(("A", "5"), ("B", "1"), ("C", "2"))
    )
    products = batch_products(batch_id)
    extra_id = create_standalone_product(f"TEST-EXTRASUB-{uuid.uuid4().hex[:6]}")
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])
    _post_event(sid, "MANUAL_ADD", product_id=products[0][0], quantity=5)
    _post_event(sid, "QR_SCAN", product_id=extra_id)
    _post_event(sid, "QR_SCAN", code="UNK-SUBMIT")
    _post_event(sid, "DAMAGE_ADD", product_id=products[0][0], quantity=1, reason="r")

    # Excepciones presentes: no bloquean el envio (solo faltantes de snapshot).
    submitted = client.post(
        f"/api/v1/inventory/count-sessions/{sid}/submit",
        json={"expected_version": 1, "confirm_missing": True},
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["status"] == "SUBMITTED"

    with SessionLocal() as db:
        audit = db.execute(
            select(AuditEvent).where(
                AuditEvent.action == audit_service.COUNT_EXCEPTIONS_READY,
                AuditEvent.entity_id == uuid.UUID(sid),
            )
        ).scalar_one()
        assert audit.metadata_ is not None
        assert audit.metadata_["has_damage"] is True
        assert audit.metadata_["has_extras"] is True
        assert audit.metadata_["has_unknowns"] is True
    cleanup_inventory_test_data()


def test_finish_check_presence_satisfied_by_resolved_unknown() -> None:
    campaign_id, batch_id, operator_id = _ready_to_count(
        quantities=(("A", "5"), ("B", "1"), ("C", "2"))
    )
    products = batch_products(batch_id)
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])
    _post_event(sid, "MANUAL_ADD", product_id=products[0][0], quantity=5)
    _post_event(sid, "QR_SCAN", code="UNK-FINISH")
    _post_event(sid, "QR_SCAN", code="UNK-FINISH")

    before = client.get(f"/api/v1/inventory/count-sessions/{sid}/finish-check").json()
    assert before["has_missing"] is True

    as_user(PERMS_RECONCILE, roles=("MANAGER",))
    unknown_id = next(
        item["id"]
        for item in client.get(f"/api/v1/inventory/campaigns/{campaign_id}/unknown-codes").json()
        if item["scanned_code"] == "UNK-FINISH"
    )
    resolved = client.post(
        f"/api/v1/inventory/unknown-codes/{unknown_id}/resolve",
        json={"product_id": str(products[2][0])},  # producto C faltante
    )
    assert resolved.status_code == 200, resolved.text

    _as_operator(operator_id)
    after = client.get(f"/api/v1/inventory/count-sessions/{sid}/finish-check").json()
    missing_ids = {row["product_id"] for row in after["missing_products"]}
    assert str(products[2][0]) not in missing_ids
    assert str(products[1][0]) in missing_ids  # B sigue faltando
    cleanup_inventory_test_data()


# ---------------------------------- lote --------------------------------------


def test_batch_with_damage_is_ordered_and_atomic() -> None:
    campaign_id, batch_id, operator_id = _ready_to_count()
    product_id, _reference = batch_products(batch_id)[0]
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])

    good = {
        "events": [
            {
                "client_event_uuid": str(uuid.uuid4()),
                "event_type": "MANUAL_ADD",
                "product_id": str(product_id),
                "quantity": "5",
            },
            {
                "client_event_uuid": str(uuid.uuid4()),
                "event_type": "DAMAGE_ADD",
                "product_id": str(product_id),
                "quantity": "2",
                "reason": "lote",
            },
        ]
    }
    response = client.post(f"/api/v1/inventory/count-sessions/{sid}/events/batch", json=good)
    assert response.status_code == 200, response.text
    sequences = [item["server_sequence"] for item in response.json()["items"]]
    assert sequences == [1, 2]
    assert _quantity(sid, product_id) == "5.0000"
    assert _damaged(sid, product_id) == "2.0000"
    assert _damage_count(sid) == 1

    bad = {
        "events": [
            {
                "client_event_uuid": str(uuid.uuid4()),
                "event_type": "MANUAL_ADD",
                "product_id": str(product_id),
                "quantity": "1",
            },
            {
                "client_event_uuid": str(uuid.uuid4()),
                "event_type": "DAMAGE_ADD",
                "product_id": str(product_id),
                "quantity": "1",
            },
        ]
    }
    failed = client.post(f"/api/v1/inventory/count-sessions/{sid}/events/batch", json=bad)
    assert failed.status_code == 422  # sin reason
    assert _quantity(sid, product_id) == "5.0000"
    assert _damaged(sid, product_id) == "2.0000"
    assert _damage_count(sid) == 1
    cleanup_inventory_test_data()


def test_batch_mixed_known_unknown_extra_and_damage() -> None:
    """Lote mixto: QR conocido + QR unknown + QR extra + DAMAGE_ADD, orden 1..4."""
    campaign_id, batch_id, operator_id = _ready_to_count()
    product_a = batch_products(batch_id)[0][0]
    extra_b = create_standalone_product(f"TEST-EXTRABAT-{uuid.uuid4().hex[:6]}")
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])

    payload = {
        "events": [
            {
                "client_event_uuid": str(uuid.uuid4()),
                "event_type": "QR_SCAN",
                "product_id": str(product_a),
            },
            {
                "client_event_uuid": str(uuid.uuid4()),
                "event_type": "QR_SCAN",
                "scanned_code": "XYZ001",
            },
            {
                "client_event_uuid": str(uuid.uuid4()),
                "event_type": "QR_SCAN",
                "product_id": str(extra_b),
            },
            {
                "client_event_uuid": str(uuid.uuid4()),
                "event_type": "DAMAGE_ADD",
                "product_id": str(product_a),
                "quantity": "1",
                "reason": "lote mixto",
            },
        ]
    }
    response = client.post(f"/api/v1/inventory/count-sessions/{sid}/events/batch", json=payload)
    assert response.status_code == 200, response.text
    assert [item["server_sequence"] for item in response.json()["items"]] == [1, 2, 3, 4]

    assert _quantity(sid, product_a) == "1.0000"
    assert _damaged(sid, product_a) == "1.0000"
    assert _quantity(sid, extra_b) == "1.0000"
    assert _extra_quantity(sid, extra_b) == "1.0000"
    assert _unknown_quantity(sid, "XYZ001") == "1.0000"
    assert _damage_count(sid) == 1
    cleanup_inventory_test_data()


def test_batch_with_unknown_is_atomic_on_damage_error() -> None:
    """Rollback total: un DAMAGE sin reason deshace tambien el unknown del lote."""
    campaign_id, _batch, operator_id = _ready_to_count()
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])
    payload = {
        "events": [
            {
                "client_event_uuid": str(uuid.uuid4()),
                "event_type": "QR_SCAN",
                "scanned_code": "XYZ-ATOMIC",
            },
            {
                "client_event_uuid": str(uuid.uuid4()),
                "event_type": "DAMAGE_ADD",
                "scanned_code": "XYZ-ATOMIC",
                "quantity": "1",
            },
        ]
    }
    failed = client.post(f"/api/v1/inventory/count-sessions/{sid}/events/batch", json=payload)
    assert failed.status_code == 422
    with SessionLocal() as db:
        unknowns = db.execute(
            select(func.count())
            .select_from(InventoryUnknownCode)
            .where(InventoryUnknownCode.session_id == uuid.UUID(sid))
        ).scalar_one()
        events = db.execute(
            select(func.count())
            .select_from(InventoryCountEvent)
            .where(InventoryCountEvent.session_id == uuid.UUID(sid))
        ).scalar_one()
    assert unknowns == 0
    assert events == 0
    cleanup_inventory_test_data()


# ------------------------------ concurrencia ----------------------------------


def test_concurrent_damage_adds_serialize_without_losing_rows() -> None:
    campaign_id, batch_id, operator_id = _ready_to_count()
    product_id, _reference = batch_products(batch_id)[0]
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])
    _post_event(sid, "MANUAL_ADD", product_id=product_id, quantity=10)
    session_uuid = uuid.UUID(sid)
    total_concurrent = 10

    def worker() -> None:
        with SessionLocal() as db:
            event_service.process_event(
                db,
                session_id=session_uuid,
                actor_id=operator_id,
                payload=event_service.EventInput(
                    client_event_uuid=uuid.uuid4(),
                    event_type=CountEventType.DAMAGE_ADD,
                    product_id=product_id,
                    quantity=decimal.Decimal("1"),
                    reason="concurrencia",
                ),
            )

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(worker) for _ in range(total_concurrent)]
        for future in futures:
            future.result()

    assert _quantity(sid, product_id) == "10.0000"
    assert _damaged(sid, product_id) == f"{total_concurrent}.0000"
    assert _damage_count(sid) == total_concurrent

    # Un ADD adicional rebasa el fisico: 409, nunca 11.
    status, body = _post_event(
        sid, "DAMAGE_ADD", product_id=product_id, quantity=1, reason="demasiado"
    )
    assert status == 409
    assert body["detail"]["error"] == "DAMAGE_EXCEEDS_PHYSICAL_QUANTITY"
    assert _damaged(sid, product_id) == "10.0000"

    with SessionLocal() as db:
        sequences = list(
            db.execute(
                select(InventoryCountEvent.server_sequence).where(
                    InventoryCountEvent.session_id == session_uuid
                )
            ).scalars()
        )
    assert len(sequences) == 1 + total_concurrent
    assert len(set(sequences)) == len(sequences)
    cleanup_inventory_test_data()


def test_concurrent_unknown_qr_scans_serialize() -> None:
    """Prueba real: 20 QR concurrentes del mismo UNKNOWN -> quantity 20, 0 perdidos."""
    campaign_id, _batch, operator_id = _ready_to_count()
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
                    scanned_code="UNK-CONC",
                    source=EventSource.CAMERA,
                ),
            )

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(worker) for _ in range(total_concurrent)]
        for future in futures:
            future.result()

    assert _unknown_quantity(sid, "UNK-CONC") == f"{total_concurrent}.0000"
    with SessionLocal() as db:
        unknowns = list(
            db.execute(
                select(InventoryUnknownCode).where(
                    InventoryUnknownCode.session_id == session_uuid
                )
            ).scalars()
        )
        sequences = list(
            db.execute(
                select(InventoryCountEvent.server_sequence).where(
                    InventoryCountEvent.session_id == session_uuid
                )
            ).scalars()
        )
    assert len(unknowns) == 1  # unica (session_id, scanned_code)
    assert len(sequences) == total_concurrent
    assert len(set(sequences)) == total_concurrent
    cleanup_inventory_test_data()


def test_concurrent_extra_scans_sync_totals_and_extra_rows() -> None:
    """Prueba real: 20 scans concurrentes del mismo extra -> totals 20, extra 20."""
    campaign_id, _batch, operator_id = _ready_to_count()
    extra_id = create_standalone_product(f"TEST-EXTRACON-{uuid.uuid4().hex[:6]}")
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
                    product_id=extra_id,
                    source=EventSource.CAMERA,
                ),
            )

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(worker) for _ in range(total_concurrent)]
        for future in futures:
            future.result()

    assert _quantity(sid, extra_id) == f"{total_concurrent}.0000"
    assert _extra_quantity(sid, extra_id) == f"{total_concurrent}.0000"
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


# -------------------------------- resumen -------------------------------------


def test_exceptions_summary_counts_without_money() -> None:
    campaign_id, batch_id, operator_id = _ready_to_count()
    product_id, _reference = batch_products(batch_id)[0]
    extra_id = create_standalone_product(f"TEST-EXTRASUM-{uuid.uuid4().hex[:6]}")
    _as_operator(operator_id)
    sid = str(_start_session(campaign_id)["id"])
    _post_event(sid, "MANUAL_ADD", product_id=product_id, quantity=4)
    _post_event(sid, "DAMAGE_ADD", product_id=product_id, quantity=2, reason="p")
    _post_event(sid, "QR_SCAN", product_id=extra_id)
    _post_event(sid, "QR_SCAN", code="UNK-SUM")
    _post_event(sid, "QR_SCAN", code="UNK-SUM")
    _post_event(sid, "DAMAGE_ADD", code="UNK-SUM", quantity=1, reason="u")

    as_user(PERMS_RECONCILE, roles=("MANAGER",))
    response = client.get(f"/api/v1/inventory/campaigns/{campaign_id}/exceptions")
    assert response.status_code == 200, response.text
    summary = response.json()
    assert summary["damaged_records_count"] == 2
    assert summary["damaged_units_current"] == "3.0000"  # 2 producto + 1 unknown
    assert summary["extra_products_count"] == 1
    assert summary["extra_units_current"] == "1.0000"
    assert summary["unknown_codes_count"] == 1
    assert summary["unknown_units_current"] == "2.0000"
    assert summary["resolved_unknown_count"] == 0
    assert summary["unresolved_unknown_count"] == 1
    for forbidden in (*_BLIND_FORBIDDEN, "cost", "valuation", "total_value"):
        assert forbidden not in response.text

    unknown_id = next(
        item["id"]
        for item in client.get(f"/api/v1/inventory/campaigns/{campaign_id}/unknown-codes").json()
        if item["scanned_code"] == "UNK-SUM"
    )
    resolved = client.post(
        f"/api/v1/inventory/unknown-codes/{unknown_id}/resolve",
        json={"product_id": str(product_id)},
    )
    assert resolved.status_code == 200
    after = client.get(f"/api/v1/inventory/campaigns/{campaign_id}/exceptions").json()
    assert after["resolved_unknown_count"] == 1
    assert after["unresolved_unknown_count"] == 0
    assert after["unknown_units_current"] == "2.0000"  # la resolucion no mueve unidades
    cleanup_inventory_test_data()
