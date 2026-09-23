"""Tests de reconteos ciegos (F007): workflow completo, blind y concurrencia."""

from __future__ import annotations

import datetime as dt
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy import func, select

from app.db.session import SessionLocal
from app.models import (
    AuditEvent,
    InventoryAssignment,
    InventoryCampaign,
    InventoryCountEvent,
    InventoryCountSession,
    InventoryCountTotal,
    InventoryRecount,
)
from app.models.enums import AssignmentStatus, RecountStatus, SessionStatus
from tests.auth_helpers import override_auth
from tests.inventory_helpers import (
    PERMS_MONITOR,
    PERMS_READ,
    PERMS_READ_RECOUNT,
    PERMS_REOPEN,
    as_user,
    batch_products,
    cleanup_inventory_test_data,
    client,
    create_operator_user,
    create_standalone_product,
)
from tests.test_count_engine import _event, _ready_to_count, _start_session
from tests.test_exceptions_damage import _post_event


def _as_manager() -> uuid.UUID:
    return as_user(
        PERMS_READ_RECOUNT
        | PERMS_MONITOR
        | PERMS_READ
        | PERMS_REOPEN
        | {"inventory.create", "inventory.assign", "inventory.expected.read"},
        roles=("MANAGER",),
    )


def _as_operator(operator_id: uuid.UUID) -> None:
    override_auth({"inventory.count", "inventory.read"}, user_id=operator_id)


def _submitted_campaign(
    quantities: tuple[tuple[str, str], ...] = (("A", "5"), ("C", "2")),
) -> tuple[str, str, uuid.UUID]:
    """Campana SUBMITTED con una sesion inicial enviada. Devuelve ids."""
    campaign_id, batch_id, operator_id = _ready_to_count(quantities=quantities)
    _as_operator(operator_id)
    session = _start_session(campaign_id)
    response = client.post(
        f"/api/v1/inventory/count-sessions/{session['id']}/submit",
        json={"expected_version": session["version"], "confirm_missing": True},
    )
    assert response.status_code == 200, response.text
    return campaign_id, batch_id, operator_id


def _campaign(campaign_id: str) -> dict[str, Any]:
    response = client.get(f"/api/v1/inventory/campaigns/{campaign_id}")
    assert response.status_code == 200, response.text
    return response.json()


def _request_recount(
    campaign_id: str,
    assigned_user_id: uuid.UUID,
    *,
    reason: str | None = None,
    source_session_id: str | None = None,
    expected_version: int | None = None,
) -> tuple[int, dict[str, Any]]:
    body: dict[str, object] = {"assigned_user_id": str(assigned_user_id)}
    if reason is not None:
        body["reason"] = reason
    if source_session_id is not None:
        body["source_session_id"] = source_session_id
    if expected_version is not None:
        body["expected_version"] = expected_version
    response = client.post(
        f"/api/v1/inventory/campaigns/{campaign_id}/recounts", json=body
    )
    return response.status_code, (response.json() if response.content else {})


def _recount_row(recount_id: str) -> InventoryRecount:
    with SessionLocal() as db:
        row = db.get(InventoryRecount, uuid.UUID(recount_id))
        assert row is not None
        db.refresh(row)
        return row


def _session_row(session_id: str) -> InventoryCountSession:
    with SessionLocal() as db:
        row = db.get(InventoryCountSession, uuid.UUID(session_id))
        assert row is not None
        db.refresh(row)
        return row


def _campaign_row(campaign_id: str) -> InventoryCampaign:
    with SessionLocal() as db:
        row = db.get(InventoryCampaign, uuid.UUID(campaign_id))
        assert row is not None
        db.refresh(row)
        return row


def _audit_count(action: str, entity_id: str) -> int:
    with SessionLocal() as db:
        return int(
            db.execute(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.action == action,
                    AuditEvent.entity_id == uuid.UUID(entity_id),
                )
            ).scalar_one()
        )


def _force_deadline_past(campaign_id: str) -> None:
    with SessionLocal() as db:
        campaign = db.get(InventoryCampaign, uuid.UUID(campaign_id))
        assert campaign is not None
        campaign.deadline_at = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5)
        db.commit()


def _snapshot_sha(campaign_id: str) -> str | None:
    return _campaign_row(campaign_id).snapshot_sha256


def _start_count(campaign_id: str) -> dict[str, Any]:
    response = client.post(f"/api/v1/inventory/campaigns/{campaign_id}/count-sessions/start")
    assert response.status_code == 200, response.text
    return response.json()


def _submit(session: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    response = client.post(
        f"/api/v1/inventory/count-sessions/{session['id']}/submit",
        json={"expected_version": session["version"], "confirm_missing": True},
    )
    return response.status_code, (response.json() if response.content else {})


# ------------------------------ request / lifecycle ---------------------------


def test_request_creates_assigned_recount_and_switches_assignment() -> None:
    campaign_id, _batch, operator_id = _submitted_campaign()
    operator2 = create_operator_user()
    _as_manager()
    before = _campaign(campaign_id)
    assert before["status"] == "SUBMITTED"
    sha_before = _snapshot_sha(campaign_id)

    status, body = _request_recount(
        campaign_id,
        operator2,
        reason="diferencia detectada",
        expected_version=before["version"],
    )
    assert status == 200, body
    assert body["status"] == "ASSIGNED"
    assert body["assigned_user_id"] == str(operator2)
    assert body["expected_version"] == 1
    assert body["campaign_version"] == before["version"] + 1
    assert body["source_session_id"] is not None  # default: ultima SUBMITTED

    campaign = _campaign(campaign_id)
    assert campaign["status"] == "RECOUNT"
    assert campaign["version"] == before["version"] + 1
    assert _snapshot_sha(campaign_id) == sha_before

    history = client.get(f"/api/v1/inventory/campaigns/{campaign_id}/assignments")
    assert history.status_code == 200
    rows = history.json()
    statuses = [(row["user_id"], row["status"]) for row in rows]
    # El submit original dejo la assignment del primer operario COMPLETED;
    # el reconteo crea la assignment ACTIVE del nuevo responsable.
    assert (str(operator_id), "COMPLETED") in statuses
    assert (str(operator2), "ACTIVE") in statuses
    active_now = [row for row in rows if row["status"] == "ACTIVE"]
    assert len(active_now) == 1
    assert active_now[0]["user_id"] == str(operator2)

    assert _audit_count("RECOUNT_REQUESTED", body["id"]) == 1
    assert _audit_count("RECOUNT_ASSIGNED", body["id"]) == 1
    cleanup_inventory_test_data()


def test_request_rejects_non_requestable_status() -> None:
    campaign_id, _batch, operator_id = _ready_to_count()  # IN_PROGRESS, sin submit
    _as_manager()
    status, body = _request_recount(campaign_id, operator_id)
    assert status == 409
    assert "no admite reconteos" in body["detail"]["message"]
    cleanup_inventory_test_data()


def test_request_requires_recount_permission() -> None:
    campaign_id, _batch, operator_id = _submitted_campaign()
    as_user(PERMS_READ)
    status, _body = _request_recount(campaign_id, operator_id)
    assert status == 403
    cleanup_inventory_test_data()


# --------------------------- start: sesion vacia ciego ------------------------


def test_recount_session_starts_empty_and_is_independent() -> None:
    campaign_id, _batch, _operator = _submitted_campaign()
    operator2 = create_operator_user()
    _as_manager()
    status, recount = _request_recount(campaign_id, operator2)
    assert status == 200, recount

    _as_operator(operator2)
    session = _start_count(campaign_id)
    assert session["session_type"] == "RECOUNT"
    assert session["session_number"] == 2  # sesion anterior + 1
    assert session["status"] == "IN_PROGRESS"

    # Sin copiar nada de la sesion de origen: eventos/totales en cero.
    with SessionLocal() as db:
        totals = db.execute(
            select(func.count())
            .select_from(InventoryCountTotal)
            .where(InventoryCountTotal.session_id == uuid.UUID(str(session["id"])))
        ).scalar_one()
        events = db.execute(
            select(func.count())
            .select_from(InventoryCountEvent)
            .where(InventoryCountEvent.session_id == uuid.UUID(str(session["id"])))
        ).scalar_one()
    assert totals == 0
    assert events == 0

    items = client.get(f"/api/v1/inventory/count-sessions/{session['id']}/items")
    assert items.status_code == 200
    assert items.json() == []

    row = _recount_row(recount["id"])
    assert row.status is RecountStatus.IN_PROGRESS
    assert row.resulting_session_id == uuid.UUID(str(session["id"]))
    assert row.started_at is not None
    assert row.expected_version == 2
    assert _audit_count("RECOUNT_STARTED", recount["id"]) == 1

    again = _start_count(campaign_id)
    assert again["id"] == session["id"]
    assert again["already_started"] is True
    cleanup_inventory_test_data()


def test_recount_start_requires_being_assignee() -> None:
    campaign_id, _batch, _operator = _submitted_campaign()
    operator2 = create_operator_user()
    other = create_operator_user()
    _as_manager()
    _request_recount(campaign_id, operator2)

    _as_operator(other)
    response = client.post(f"/api/v1/inventory/campaigns/{campaign_id}/count-sessions/start")
    assert response.status_code == 403  # no es el responsable activo
    cleanup_inventory_test_data()


# ----------------------------------- submit -----------------------------------


def test_submit_recount_completes_recount_and_is_idempotent() -> None:
    campaign_id, batch_id, operator_id = _submitted_campaign()
    operator2 = create_operator_user()
    _as_manager()
    _request_recount(campaign_id, operator2)
    sha_before = _snapshot_sha(campaign_id)

    _as_operator(operator2)
    session = _start_count(campaign_id)
    _product_id, reference = batch_products(batch_id)[0]
    code, body = _event(str(session["id"]), "QR_SCAN", code=reference)
    assert code == 200, body

    status, submitted = _submit(session)
    assert status == 200, submitted
    assert submitted["status"] == "SUBMITTED"
    assert submitted["already_submitted"] is False

    row = _recount_row(str(_recount_id_for_campaign(campaign_id)))
    assert row.status is RecountStatus.COMPLETED
    assert row.completed_at is not None
    assert row.expected_version == 3

    campaign = _campaign(campaign_id)
    assert campaign["status"] == "SUBMITTED"
    assert _snapshot_sha(campaign_id) == sha_before
    assert _audit_count("RECOUNT_COMPLETED", str(row.id)) == 1
    assert _audit_count("COUNT_SESSION_SUBMITTED", str(session["id"])) == 1

    # Reenvio idempotente: no cambia estado ni duplica auditoria.
    again = client.post(
        f"/api/v1/inventory/count-sessions/{session['id']}/submit",
        json={"expected_version": submitted["version"], "confirm_missing": True},
    )
    assert again.status_code == 200
    assert again.json()["already_submitted"] is True
    assert _recount_row(str(row.id)).status is RecountStatus.COMPLETED
    assert _audit_count("RECOUNT_COMPLETED", str(row.id)) == 1
    cleanup_inventory_test_data()


def _recount_id_for_campaign(campaign_id: str) -> uuid.UUID:
    with SessionLocal() as db:
        row = db.execute(
            select(InventoryRecount.id)
            .where(InventoryRecount.inventory_campaign_id == uuid.UUID(campaign_id))
            .order_by(InventoryRecount.created_at.desc())
            .limit(1)
        ).scalar_one()
        return row


# --------------------------- multiples reconteos ------------------------------


def test_sequential_recounts_and_frozen_snapshot() -> None:
    campaign_id, _batch, _operator = _submitted_campaign()
    operator2 = create_operator_user()
    sha_before = _snapshot_sha(campaign_id)

    _as_manager()
    status1, recount1 = _request_recount(campaign_id, operator2, reason="primer reconteo")
    assert status1 == 200, recount1
    _as_operator(operator2)
    session1 = _start_count(campaign_id)
    status, _body = _submit(session1)
    assert status == 200

    _as_manager()
    campaign = _campaign(campaign_id)
    assert campaign["status"] == "SUBMITTED"
    status2, recount2 = _request_recount(campaign_id, operator2, reason="segundo reconteo")
    assert status2 == 200, recount2
    assert recount2["id"] != recount1["id"]
    # Regla por defecto de source: la ultima SUBMITTED (la del reconteo 1).
    assert recount2["source_session_id"] == str(session1["id"])

    _as_operator(operator2)
    session2 = _start_count(campaign_id)
    assert session2["session_number"] == 3
    assert session2["session_type"] == "RECOUNT"
    status, _body = _submit(session2)
    assert status == 200

    assert _recount_row(recount1["id"]).status is RecountStatus.COMPLETED
    assert _recount_row(recount2["id"]).status is RecountStatus.COMPLETED
    assert _snapshot_sha(campaign_id) == sha_before
    cleanup_inventory_test_data()


def test_explicit_source_session_and_invalid_source() -> None:
    campaign_id, _batch, _operator = _submitted_campaign()
    operator2 = create_operator_user()
    _as_manager()
    # La sesion inicial enviada (number 1) como fuente explicita.
    with SessionLocal() as db:
        source = db.execute(
            select(InventoryCountSession.id)
            .where(
                InventoryCountSession.inventory_campaign_id == uuid.UUID(campaign_id),
                InventoryCountSession.session_number == 1,
            )
        ).scalar_one()
    status, body = _request_recount(
        campaign_id, operator2, source_session_id=str(source)
    )
    assert status == 200, body
    assert body["source_session_id"] == str(source)

    # Fuente invalida: id inexistente.
    cleanup_inventory_test_data()
    campaign_id, _batch, _op = _submitted_campaign()
    operator3 = create_operator_user()
    _as_manager()
    status, body = _request_recount(campaign_id, operator3, source_session_id=str(uuid.uuid4()))
    assert status == 404
    cleanup_inventory_test_data()


# --------------------------- un solo reconteo abierto -------------------------


def test_only_one_open_recount_per_campaign() -> None:
    campaign_id, _batch, _operator = _submitted_campaign()
    operator2 = create_operator_user()
    operator3 = create_operator_user()
    _as_manager()
    status, first = _request_recount(campaign_id, operator2)
    assert status == 200, first

    status, body = _request_recount(campaign_id, operator3)
    assert status == 409
    assert body["detail"]["error"] == "RECOUNT_ALREADY_OPEN"

    # Tras cancelar, se puede abrir uno nuevo.
    cancelled = client.post(
        f"/api/v1/inventory/recounts/{first['id']}/cancel",
        json={"reason": "no hace falta", "expected_version": 1},
    )
    assert cancelled.status_code == 200, cancelled.text
    status, second = _request_recount(campaign_id, operator3)
    assert status == 200, second
    assert second["id"] != first["id"]
    cleanup_inventory_test_data()


# --------------------------------- reassign -----------------------------------


def test_reassign_before_start_swaps_assignment() -> None:
    campaign_id, _batch, _operator = _submitted_campaign()
    operator2 = create_operator_user()
    operator3 = create_operator_user()
    _as_manager()
    status, recount = _request_recount(campaign_id, operator2)
    assert status == 200, recount

    response = client.post(
        f"/api/v1/inventory/recounts/{recount['id']}/reassign",
        json={"user_id": str(operator3), "expected_version": 1},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ASSIGNED"
    assert body["assigned_user_id"] == str(operator3)
    assert body["expected_version"] == 2

    row = _recount_row(recount["id"])
    assert row.assigned_user_id == operator3
    assert row.resulting_session_id is None
    assert _audit_count("RECOUNT_REASSIGNED", recount["id"]) == 1

    with SessionLocal() as db:
        active = list(
            db.execute(
                select(InventoryAssignment).where(
                    InventoryAssignment.inventory_campaign_id == uuid.UUID(campaign_id),
                    InventoryAssignment.status == AssignmentStatus.ACTIVE,
                )
            ).scalars()
        )
    assert len(active) == 1
    assert active[0].user_id == operator3

    # El usuario anterior ya no es responsable activo.
    _as_operator(operator2)
    response = client.post(f"/api/v1/inventory/campaigns/{campaign_id}/count-sessions/start")
    assert response.status_code == 403
    cleanup_inventory_test_data()


def test_reassign_after_start_cancels_session_and_resets() -> None:
    campaign_id, _batch, _operator = _submitted_campaign()
    operator2 = create_operator_user()
    operator3 = create_operator_user()
    _as_manager()
    status, recount = _request_recount(campaign_id, operator2)
    assert status == 200, recount

    _as_operator(operator2)
    session = _start_count(campaign_id)

    _as_manager()
    response = client.post(
        f"/api/v1/inventory/recounts/{recount['id']}/reassign",
        json={"user_id": str(operator3), "expected_version": 2},
    )
    assert response.status_code == 200, response.text
    assert response.json()["expected_version"] == 3

    old = _session_row(str(session["id"]))
    assert old.status is SessionStatus.CANCELLED
    row = _recount_row(recount["id"])
    assert row.status is RecountStatus.ASSIGNED
    assert row.resulting_session_id is None
    assert row.assigned_user_id == operator3

    _as_operator(operator3)
    fresh = _start_count(campaign_id)
    assert fresh["id"] != session["id"]
    assert fresh["session_number"] == 3
    assert fresh["session_type"] == "RECOUNT"
    cleanup_inventory_test_data()


# ---------------------------------- cancel ------------------------------------


def test_cancel_before_start_restores_campaign_and_assignment() -> None:
    campaign_id, _batch, _operator = _submitted_campaign()
    operator2 = create_operator_user()
    _as_manager()
    status, recount = _request_recount(campaign_id, operator2)
    assert status == 200, recount

    response = client.post(
        f"/api/v1/inventory/recounts/{recount['id']}/cancel",
        json={"reason": "anulado por gerencia", "expected_version": 1},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "CANCELLED"
    assert body["cancel_reason"] == "anulado por gerencia"
    assert body["cancelled_by"] is not None

    row = _recount_row(recount["id"])
    assert row.status is RecountStatus.CANCELLED
    assert row.cancelled_at is not None

    campaign = _campaign(campaign_id)
    assert campaign["status"] == "SUBMITTED"  # existia sesion SUBMITTED previa

    with SessionLocal() as db:
        active = list(
            db.execute(
                select(InventoryAssignment).where(
                    InventoryAssignment.inventory_campaign_id == uuid.UUID(campaign_id),
                    InventoryAssignment.status == AssignmentStatus.ACTIVE,
                )
            ).scalars()
        )
    assert active == []

    # El asignado cancelado ya no puede iniciar sesion.
    _as_operator(operator2)
    response = client.post(f"/api/v1/inventory/campaigns/{campaign_id}/count-sessions/start")
    assert response.status_code == 409
    cleanup_inventory_test_data()


def test_cancel_after_start_cancels_session_and_campaign_back_to_submitted() -> None:
    campaign_id, _batch, _operator = _submitted_campaign()
    operator2 = create_operator_user()
    _as_manager()
    _request_recount(campaign_id, operator2)

    _as_operator(operator2)
    session = _start_count(campaign_id)

    _as_manager()
    with SessionLocal() as db:
        recount_id = db.execute(
            select(InventoryRecount.id).where(
                InventoryRecount.inventory_campaign_id == uuid.UUID(campaign_id)
            )
        ).scalar_one()
    response = client.post(
        f"/api/v1/inventory/recounts/{recount_id}/cancel",
        json={"reason": "cancelado en curso", "expected_version": 2},
    )
    assert response.status_code == 200, response.text

    assert _session_row(str(session["id"])).status is SessionStatus.CANCELLED
    assert _recount_row(str(recount_id)).status is RecountStatus.CANCELLED
    assert _campaign(campaign_id)["status"] == "SUBMITTED"
    assert _audit_count("RECOUNT_CANCELLED", str(recount_id)) == 1

    # La sesion cancelada ya no acepta eventos.
    _as_operator(operator2)
    code, _body = _event(str(session["id"]), "MANUAL_ADD", quantity=1)
    assert code == 409
    cleanup_inventory_test_data()


def test_cancel_requires_reason_and_valid_version() -> None:
    campaign_id, _batch, _operator = _submitted_campaign()
    operator2 = create_operator_user()
    _as_manager()
    _status, recount = _request_recount(campaign_id, operator2)

    missing = client.post(
        f"/api/v1/inventory/recounts/{recount['id']}/cancel",
        json={"reason": "   ", "expected_version": 1},
    )
    assert missing.status_code == 422

    conflict = client.post(
        f"/api/v1/inventory/recounts/{recount['id']}/cancel",
        json={"reason": "ok", "expected_version": 99},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error"] == "VERSION_CONFLICT"

    reassign_conflict = client.post(
        f"/api/v1/inventory/recounts/{recount['id']}/reassign",
        json={"user_id": str(create_operator_user()), "expected_version": 42},
    )
    assert reassign_conflict.status_code == 409
    assert reassign_conflict.json()["detail"]["error"] == "VERSION_CONFLICT"

    closed = client.post(
        f"/api/v1/inventory/recounts/{recount['id']}/cancel",
        json={"reason": "ok", "expected_version": 1},
    )
    assert closed.status_code == 200
    again = client.post(
        f"/api/v1/inventory/recounts/{recount['id']}/cancel",
        json={"reason": "otro", "expected_version": 2},
    )
    assert again.status_code == 409
    assert again.json()["detail"]["error"] == "RECOUNT_NOT_OPEN"
    cleanup_inventory_test_data()


# ------------------------- submit con excepciones (F006) ----------------------


def test_recount_submit_with_exceptions_does_not_block() -> None:
    campaign_id, batch_id, _operator = _submitted_campaign()
    operator2 = create_operator_user()
    _as_manager()
    _request_recount(campaign_id, operator2)

    _as_operator(operator2)
    session = _start_count(campaign_id)
    sid = str(session["id"])
    product_id, reference = batch_products(batch_id)[0]
    extra_id = create_standalone_product(f"TEST-RC-{uuid.uuid4().hex[:6]}")

    assert _event(sid, "MANUAL_ADD", product_id=product_id, quantity=5)[0] == 200
    assert _event(sid, "QR_SCAN", product_id=extra_id)[0] == 200  # EXTRA
    assert _event(sid, "QR_SCAN", code="UNK-RC-1")[0] == 200  # UNKNOWN
    damage_status, damage_body = _post_event(
        sid, "DAMAGE_ADD", product_id=product_id, quantity=1, reason="dano en reconteo"
    )
    assert damage_status == 200, damage_body

    status, body = _submit(session)
    assert status == 200, body
    row = _recount_row(str(_recount_id_for_campaign(campaign_id)))
    assert row.status is RecountStatus.COMPLETED
    cleanup_inventory_test_data()


# ------------------------------- version / deadline ---------------------------


def test_request_campaign_version_conflict() -> None:
    campaign_id, _batch, operator_id = _submitted_campaign()
    _as_manager()
    current = _campaign(campaign_id)
    status, body = _request_recount(
        campaign_id, operator_id, expected_version=current["version"] + 5
    )
    assert status == 409
    assert "Conflicto de version" in body["detail"]["message"]
    cleanup_inventory_test_data()


def test_expired_deadline_rejects_request_and_allows_reopen_flow() -> None:
    campaign_id, _batch, operator_id = _submitted_campaign()
    _force_deadline_past(campaign_id)
    operator2 = create_operator_user()

    _as_manager()
    status, body = _request_recount(campaign_id, operator2)
    assert status == 409
    assert body["detail"]["error"] == "CAMPAIGN_EXPIRED"
    assert _campaign(campaign_id)["status"] == "EXPIRED"

    # Reopen F004 habilita el flujo administrativo de nuevo.
    campaign = _campaign(campaign_id)
    reopen = client.post(
        f"/api/v1/inventory/campaigns/{campaign_id}/reopen",
        json={
            "reason": "reabrir para reconteo",
            "new_deadline_at": (dt.datetime.now(dt.UTC) + dt.timedelta(days=1)).isoformat(),
            "expected_version": campaign["version"],
        },
    )
    assert reopen.status_code == 200, reopen.text
    assert reopen.json()["status"] == "IN_PROGRESS"

    # El submit original dejo la assignment COMPLETED: F004 requiere un nuevo
    # assign antes de continuar contando.
    assigned = client.post(
        f"/api/v1/inventory/campaigns/{campaign_id}/assign",
        json={
            "user_id": str(operator_id),
            "expected_version": reopen.json()["version"],
        },
    )
    assert assigned.status_code == 200, assigned.text

    _as_operator(operator_id)
    session = _start_count(campaign_id)
    status, _body = _submit(session)
    assert status == 200

    _as_manager()
    status, body = _request_recount(campaign_id, operator2)
    assert status == 200, body
    cleanup_inventory_test_data()


def test_expired_during_recount_blocks_events_and_reopen_invalidates_session() -> None:
    """§45/§46: expira durante reconteo; el reopen NO revive la sesion vieja."""
    campaign_id, _batch, _operator = _submitted_campaign()
    operator2 = create_operator_user()
    _as_manager()
    _status, recount = _request_recount(campaign_id, operator2)

    _as_operator(operator2)
    session = _start_count(campaign_id)
    sid = str(session["id"])

    _force_deadline_past(campaign_id)

    # Evente nuevo durante reconteo vencido: 409 CAMPAIGN_EXPIRED.
    code, body = _event(sid, "MANUAL_ADD", quantity=1)
    assert code == 409, body
    assert body["detail"]["error"] == "CAMPAIGN_EXPIRED"
    assert _campaign_row(campaign_id).status.value == "EXPIRED"
    # El recount NO se marca COMPLETED artificialmente.
    assert _recount_row(recount["id"]).status is RecountStatus.IN_PROGRESS

    # Reopen administrativo.
    _as_manager()
    campaign = _campaign(campaign_id)
    reopen = client.post(
        f"/api/v1/inventory/campaigns/{campaign_id}/reopen",
        json={
            "reason": "reabrir tras vencimiento",
            "new_deadline_at": (dt.datetime.now(dt.UTC) + dt.timedelta(days=1)).isoformat(),
            "expected_version": campaign["version"],
        },
    )
    assert reopen.status_code == 200, reopen.text
    assert reopen.json()["status"] == "RECOUNT"  # reconteo sigue abierto

    # La sesion vieja quedo CANCELLED y NO acepta eventos tras el reopen.
    assert _session_row(sid).status is SessionStatus.CANCELLED
    row = _recount_row(recount["id"])
    assert row.status is RecountStatus.ASSIGNED
    assert row.resulting_session_id is None
    _as_operator(operator2)
    code, body = _event(sid, "MANUAL_ADD", quantity=1)
    assert code == 409
    assert body["detail"]["error"] == "SESSION_NOT_OPEN"

    # Flujo explicito: nueva sesion de reconteo arranca desde cero.
    fresh = _start_count(campaign_id)
    assert fresh["id"] != sid
    assert fresh["session_type"] == "RECOUNT"
    assert fresh["session_number"] == 3
    cleanup_inventory_test_data()


# ----------------------------------- blind ------------------------------------


def test_blind_payloads_hide_source_and_admin_requires_monitor() -> None:
    campaign_id, _batch, _operator = _submitted_campaign()
    operator2 = create_operator_user()
    _as_manager()
    _status, recount = _request_recount(campaign_id, operator2, reason="cego")

    # Owner: my-recounts y detail blind-safe (sin source_session_id ni expected).
    _as_operator(operator2)
    mine = client.get("/api/v1/inventory/my-recounts")
    assert mine.status_code == 200
    rows = mine.json()
    assert len(rows) == 1
    owner_row = rows[0]
    assert "source_session_id" not in owner_row
    assert "source_session_id" not in str(owner_row)
    dumped = str(owner_row).lower()
    for forbidden in ("expected", "cost", "price", "snapshot", "difference"):
        assert forbidden not in dumped, forbidden

    detail = client.get(f"/api/v1/inventory/recounts/{recount['id']}")
    assert detail.status_code == 200
    detail_body = detail.json()
    assert "source_session_id" not in detail_body
    assert detail_body["assignment_id"] is not None

    # Owner SIN monitor no ve el listado administrativo de la campana.
    admin_list = client.get(f"/api/v1/inventory/campaigns/{campaign_id}/recounts")
    assert admin_list.status_code == 403

    # Tercero sin relacion: 403 en el detail.
    outsider = create_operator_user()
    _as_operator(outsider)
    assert client.get(f"/api/v1/inventory/recounts/{recount['id']}").status_code == 403

    # Monitor: si ve source_session_id.
    _as_manager()
    admin = client.get(f"/api/v1/inventory/campaigns/{campaign_id}/recounts")
    assert admin.status_code == 200
    admin_rows = admin.json()
    assert len(admin_rows) == 1
    assert admin_rows[0]["source_session_id"] is not None
    assert admin_rows[0]["status"] == "ASSIGNED"

    # my-recounts requiere inventory.read.
    override_auth({"inventory.count"}, user_id=operator2)
    assert client.get("/api/v1/inventory/my-recounts").status_code == 403
    cleanup_inventory_test_data()


def test_admin_session_history_lists_recount_sessions_blind() -> None:
    campaign_id, _batch, _operator = _submitted_campaign()
    operator2 = create_operator_user()
    _as_manager()
    _request_recount(campaign_id, operator2)
    _as_operator(operator2)
    _start_count(campaign_id)

    _as_manager()
    history = client.get(f"/api/v1/inventory/campaigns/{campaign_id}/count-sessions")
    assert history.status_code == 200
    types = [row["session_type"] for row in history.json()]
    assert "RECOUNT" in types
    for row in history.json():
        assert "expected" not in row
        assert "difference" not in row
    cleanup_inventory_test_data()


# ------------------------------- concurrencia ---------------------------------


def test_concurrent_requests_only_one_wins() -> None:
    campaign_id, _batch, _operator = _submitted_campaign()
    operator2 = create_operator_user()
    _as_manager()

    def _try(_index: int) -> int:
        response = client.post(
            f"/api/v1/inventory/campaigns/{campaign_id}/recounts",
            json={"assigned_user_id": str(operator2)},
        )
        return int(response.status_code)

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(_try, [0, 1]))

    assert sorted(codes) == [200, 409], codes
    with SessionLocal() as db:
        open_rows = db.execute(
            select(func.count())
            .select_from(InventoryRecount)
            .where(
                InventoryRecount.inventory_campaign_id == uuid.UUID(campaign_id),
                InventoryRecount.status.in_(
                    (RecountStatus.REQUESTED, RecountStatus.ASSIGNED, RecountStatus.IN_PROGRESS)
                ),
            )
        ).scalar_one()
    assert open_rows == 1
    cleanup_inventory_test_data()


def test_recount_request_does_not_copy_events_or_totals() -> None:
    """La sesion origen conserva sus datos; la de reconteo nace vacia."""
    campaign_id, batch_id, _operator = _submitted_campaign()
    # La sesion inicial quedo SUBMITTED vacia; contamos algo en el reconteo.
    operator2 = create_operator_user()
    _as_manager()
    _status, recount = _request_recount(campaign_id, operator2)
    _as_operator(operator2)
    session = _start_count(campaign_id)
    product_id, _reference = batch_products(batch_id)[0]
    _event(str(session["id"]), "MANUAL_ADD", product_id=product_id, quantity=7)

    with SessionLocal() as db:
        source_id = _recount_row(recount["id"]).source_session_id
        assert source_id is not None
        source_totals = db.execute(
            select(func.count())
            .select_from(InventoryCountTotal)
            .where(InventoryCountTotal.session_id == source_id)
        ).scalar_one()
        recount_totals = db.execute(
            select(func.count())
            .select_from(InventoryCountTotal)
            .where(InventoryCountTotal.session_id == uuid.UUID(str(session["id"])))
        ).scalar_one()
    assert source_totals == 0  # la origen no hereda lo nuevo del reconteo
    assert recount_totals == 1  # solo lo que registro el operario del reconteo
    cleanup_inventory_test_data()
