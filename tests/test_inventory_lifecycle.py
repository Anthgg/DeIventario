"""Tests del ciclo de vida de inventario (locations, campanas, asignaciones)."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.session import SessionLocal
from app.models import InventoryAssignment
from app.models.enums import AssignmentStatus, ImportBatchType
from tests.inventory_helpers import (
    PERMS_ASSIGN,
    PERMS_CREATE,
    PERMS_MONITOR,
    PERMS_READ,
    PERMS_REOPEN,
    as_user,
    cleanup_inventory_test_data,
    client,
    create_inactive_user,
    create_operator_user,
    create_test_source_batch,
    create_test_user,
)


def _create_campaign(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {"name": f"test-camp-{uuid.uuid4().hex[:8]}"}
    payload.update(overrides)
    response = client.post("/api/v1/inventory/campaigns", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def _setup_assigned_campaign() -> tuple[dict[str, object], uuid.UUID]:
    as_user(PERMS_CREATE, roles=("MANAGER",))
    campaign = _create_campaign()
    operator_id = create_operator_user()
    as_user(PERMS_ASSIGN, roles=("MANAGER",))
    response = client.post(
        f"/api/v1/inventory/campaigns/{campaign['id']}/assign",
        json={"user_id": str(operator_id), "expected_version": campaign["version"]},
    )
    assert response.status_code == 200, response.text
    return campaign, operator_id


# --------------------------------- locations ---------------------------------


def test_location_crud_and_code_uniqueness() -> None:
    cleanup_inventory_test_data()
    as_user(PERMS_CREATE, roles=("MANAGER",))
    first = client.post("/api/v1/inventory/locations", json={"name": "test-loc-A", "code": "T-A"})
    assert first.status_code == 200
    assert (
        client.post("/api/v1/inventory/locations", json={"name": "test-loc-B", "code": "T-A"})
    ).status_code == 409
    # El nombre NO se asume unico.
    assert (
        client.post("/api/v1/inventory/locations", json={"name": "test-loc-A"}).status_code == 200
    )

    as_user(PERMS_READ)
    listed = client.get("/api/v1/inventory/locations")
    assert listed.status_code == 200
    assert any(item["code"] == "T-A" for item in listed.json())

    location_id = first.json()["id"]
    as_user(PERMS_CREATE, roles=("MANAGER",))
    patched = client.patch(
        f"/api/v1/inventory/locations/{location_id}",
        json={"active": False, "name": "test-loc-A2"},
    )
    assert patched.status_code == 200
    assert patched.json()["active"] is False
    cleanup_inventory_test_data()


def test_location_requires_create_permission() -> None:
    as_user(PERMS_READ)
    assert client.post("/api/v1/inventory/locations", json={"name": "x"}).status_code == 403


# --------------------------------- campaigns ---------------------------------


def test_create_campaign_generates_code_and_draft_status() -> None:
    cleanup_inventory_test_data()
    as_user(PERMS_CREATE, roles=("MANAGER",))
    body = _create_campaign()
    assert body["status"] == "DRAFT"
    assert body["version"] == 1
    assert str(body["code"]).startswith("INV-")
    cleanup_inventory_test_data()


def test_generated_codes_are_unique() -> None:
    cleanup_inventory_test_data()
    as_user(PERMS_CREATE, roles=("MANAGER",))
    codes = {_create_campaign()["code"] for _ in range(3)}
    assert len(codes) == 3
    cleanup_inventory_test_data()


def test_naive_deadline_is_rejected() -> None:
    cleanup_inventory_test_data()
    as_user(PERMS_CREATE, roles=("MANAGER",))
    response = client.post(
        "/api/v1/inventory/campaigns",
        json={"name": "test-naive", "deadline_at": "2030-01-01T00:00:00"},
    )
    assert response.status_code == 422
    cleanup_inventory_test_data()


def test_contacts_batch_is_rejected_as_source() -> None:
    cleanup_inventory_test_data()
    batch_id = create_test_source_batch(import_type=ImportBatchType.CONTACTS)
    as_user(PERMS_CREATE, roles=("MANAGER",))
    response = client.post(
        "/api/v1/inventory/campaigns",
        json={"name": "test-bad-source", "source_import_batch_id": str(batch_id)},
    )
    assert response.status_code == 409
    cleanup_inventory_test_data()


def test_source_without_stock_is_rejected() -> None:
    cleanup_inventory_test_data()
    batch_id = create_test_source_batch(with_stock=False)
    as_user(PERMS_CREATE, roles=("MANAGER",))
    response = client.post(
        "/api/v1/inventory/campaigns",
        json={"name": "test-no-stock", "source_import_batch_id": str(batch_id)},
    )
    assert response.status_code == 409
    cleanup_inventory_test_data()


def test_update_draft_and_version_conflict() -> None:
    cleanup_inventory_test_data()
    as_user(PERMS_CREATE, roles=("MANAGER",))
    campaign = _create_campaign()
    ok = client.patch(
        f"/api/v1/inventory/campaigns/{campaign['id']}",
        json={"expected_version": 1, "name": "test-renamed"},
    )
    assert ok.status_code == 200
    assert ok.json()["version"] == 2
    assert ok.json()["name"] == "test-renamed"

    stale = client.patch(
        f"/api/v1/inventory/campaigns/{campaign['id']}",
        json={"expected_version": 1, "name": "test-otro"},
    )
    assert stale.status_code == 409
    cleanup_inventory_test_data()


def test_list_pagination_and_filters() -> None:
    cleanup_inventory_test_data()
    as_user(PERMS_CREATE, roles=("MANAGER",))
    for _ in range(3):
        _create_campaign()
    as_user(PERMS_READ)
    page = client.get("/api/v1/inventory/campaigns", params={"limit": 2, "offset": 0})
    assert page.status_code == 200
    body = page.json()
    assert body["limit"] == 2 and body["offset"] == 0
    assert len(body["items"]) <= 2
    filtered = client.get("/api/v1/inventory/campaigns", params={"status": "DRAFT"})
    assert filtered.status_code == 200
    cleanup_inventory_test_data()


def test_list_and_detail_are_blind_safe() -> None:
    cleanup_inventory_test_data()
    as_user(PERMS_CREATE, roles=("MANAGER",))
    campaign = _create_campaign(source_import_batch_id=str(create_test_source_batch()))

    as_user(PERMS_READ)
    detail = client.get(f"/api/v1/inventory/campaigns/{campaign['id']}")
    assert detail.status_code == 200
    listed = client.get("/api/v1/inventory/campaigns")
    forbidden = {
        "expected_quantity",
        "total_expected_units",
        "sale_price_snapshot",
        "cost_snapshot",
        "effective_cost_snapshot",
        "snapshot_sha256",
        "snapshot_frozen_at",
        "expected_product_count",
    }
    assert forbidden.isdisjoint(detail.json().keys())
    assert forbidden.isdisjoint(listed.json()["items"][0].keys())
    cleanup_inventory_test_data()


def test_detail_requires_read_permission() -> None:
    cleanup_inventory_test_data()
    as_user(PERMS_CREATE, roles=("MANAGER",))
    campaign = _create_campaign()
    as_user(PERMS_MONITOR, roles=("SUPERVISOR",))
    assert client.get(f"/api/v1/inventory/campaigns/{campaign['id']}").status_code == 403
    cleanup_inventory_test_data()


# -------------------------------- assignments --------------------------------


def test_assign_sets_assigned_status() -> None:
    cleanup_inventory_test_data()
    campaign, operator_id = _setup_assigned_campaign()
    as_user(PERMS_READ)
    detail = client.get(f"/api/v1/inventory/campaigns/{campaign['id']}")
    assert detail.json()["status"] == "ASSIGNED"
    assert detail.json()["version"] == 2
    cleanup_inventory_test_data()


def test_assign_inactive_user_rejected() -> None:
    cleanup_inventory_test_data()
    as_user(PERMS_CREATE, roles=("MANAGER",))
    campaign = _create_campaign()
    inactive_id = create_inactive_user()
    as_user(PERMS_ASSIGN, roles=("MANAGER",))
    response = client.post(
        f"/api/v1/inventory/campaigns/{campaign['id']}/assign",
        json={"user_id": str(inactive_id), "expected_version": campaign["version"]},
    )
    assert response.status_code == 400
    cleanup_inventory_test_data()


def test_assign_user_without_inventory_count_rejected() -> None:
    cleanup_inventory_test_data()
    as_user(PERMS_CREATE, roles=("MANAGER",))
    campaign = _create_campaign()
    no_role_id = create_test_user()
    as_user(PERMS_ASSIGN, roles=("MANAGER",))
    response = client.post(
        f"/api/v1/inventory/campaigns/{campaign['id']}/assign",
        json={"user_id": str(no_role_id), "expected_version": campaign["version"]},
    )
    assert response.status_code == 400
    cleanup_inventory_test_data()


def test_same_user_assign_is_idempotent() -> None:
    cleanup_inventory_test_data()
    campaign, operator_id = _setup_assigned_campaign()
    as_user(PERMS_ASSIGN, roles=("MANAGER",))
    second = client.post(
        f"/api/v1/inventory/campaigns/{campaign['id']}/assign",
        json={"user_id": str(operator_id), "expected_version": 2},
    )
    assert second.status_code == 200
    assert second.json()["created"] is False
    with SessionLocal() as db:
        rows = list(
            db.execute(
                select(InventoryAssignment).where(
                    InventoryAssignment.inventory_campaign_id == uuid.UUID(str(campaign["id"]))
                )
            ).scalars()
        )
    assert len(rows) == 1
    cleanup_inventory_test_data()


def test_reassign_creates_new_row_and_revokes_previous() -> None:
    cleanup_inventory_test_data()
    campaign, first_operator = _setup_assigned_campaign()
    second_operator = create_operator_user()
    as_user(PERMS_ASSIGN, roles=("MANAGER",))
    response = client.post(
        f"/api/v1/inventory/campaigns/{campaign['id']}/assign",
        json={"user_id": str(second_operator), "expected_version": 2},
    )
    assert response.status_code == 200
    assert response.json()["reassigned"] is True

    as_user(PERMS_MONITOR, roles=("SUPERVISOR",))
    history = client.get(f"/api/v1/inventory/campaigns/{campaign['id']}/assignments")
    assert history.status_code == 200
    rows = history.json()
    assert len(rows) == 2
    assert rows[0]["status"] == "REVOKED" and rows[0]["user_id"] == str(first_operator)
    assert rows[1]["status"] == "ACTIVE" and rows[1]["user_id"] == str(second_operator)
    assert "expected_quantity" not in str(rows)
    cleanup_inventory_test_data()


def test_one_active_assignment_enforced_by_database() -> None:
    cleanup_inventory_test_data()
    campaign, _operator = _setup_assigned_campaign()
    other = create_operator_user()
    with SessionLocal() as db:
        db.add(
            InventoryAssignment(
                inventory_campaign_id=uuid.UUID(str(campaign["id"])),
                user_id=other,
                status=AssignmentStatus.ACTIVE,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    cleanup_inventory_test_data()


def test_unassign_before_start_returns_to_draft() -> None:
    cleanup_inventory_test_data()
    campaign, _operator = _setup_assigned_campaign()
    as_user(PERMS_ASSIGN, roles=("MANAGER",))
    assert (
        client.post(
            f"/api/v1/inventory/campaigns/{campaign['id']}/unassign",
            json={"expected_version": 2},
        ).status_code
        == 200
    )
    as_user(PERMS_READ)
    detail = client.get(f"/api/v1/inventory/campaigns/{campaign['id']}")
    assert detail.json()["status"] == "DRAFT"
    cleanup_inventory_test_data()


def test_assignment_history_requires_monitor() -> None:
    cleanup_inventory_test_data()
    campaign, _operator = _setup_assigned_campaign()
    as_user(PERMS_READ)
    assert (
        client.get(f"/api/v1/inventory/campaigns/{campaign['id']}/assignments").status_code == 403
    )
    cleanup_inventory_test_data()


def test_my_assignments_only_returns_own() -> None:
    cleanup_inventory_test_data()
    campaign, operator_id = _setup_assigned_campaign()
    # Otro usuario autenticado no ve la asignacion ajena.
    as_user(PERMS_READ)
    assert client.get("/api/v1/inventory/my-assignments").json() == []
    # El operario asignado si la ve.
    as_user(PERMS_READ, roles=("OPERATOR",))
    from tests.auth_helpers import override_auth

    override_auth({*PERMS_READ}, user_id=operator_id)
    mine = client.get("/api/v1/inventory/my-assignments").json()
    assert len(mine) == 1
    assert mine[0]["id"] == campaign["id"]
    assert "expected_quantity" not in str(mine)
    cleanup_inventory_test_data()


def test_snapshot_sources_lists_eligible_batches() -> None:
    cleanup_inventory_test_data()
    as_user(PERMS_CREATE, roles=("MANAGER",))
    response = client.get("/api/v1/inventory/snapshot-sources")
    assert response.status_code == 200
    payload = response.json()
    assert payload, "deben existir lotes elegibles (productos F002)"
    first = payload[0]
    expected_keys = {
        "id",
        "import_type",
        "source_filename",
        "completed_at",
        "stock_snapshot_count",
        "stock_scope",
    }
    assert expected_keys <= set(first)
    assert "raw_data" not in str(first)
    assert "sha256" not in str(first)
    cleanup_inventory_test_data()


# --------------------------------- deadline ----------------------------------


def test_campaign_expires_on_read_after_deadline() -> None:
    cleanup_inventory_test_data()
    as_user(PERMS_CREATE, roles=("MANAGER",))
    past = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)).isoformat()
    campaign = _create_campaign(
        source_import_batch_id=str(create_test_source_batch()), deadline_at=past
    )
    operator_id = create_operator_user()
    as_user(PERMS_ASSIGN, roles=("MANAGER",))
    client.post(
        f"/api/v1/inventory/campaigns/{campaign['id']}/assign",
        json={"user_id": str(operator_id), "expected_version": 1},
    )
    as_user(PERMS_READ)
    detail = client.get(f"/api/v1/inventory/campaigns/{campaign['id']}")
    assert detail.json()["status"] == "EXPIRED"
    cleanup_inventory_test_data()


def test_reopen_requires_permission_and_reason() -> None:
    cleanup_inventory_test_data()
    as_user(PERMS_CREATE, roles=("MANAGER",))
    past = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)).isoformat()
    campaign = _create_campaign(
        source_import_batch_id=str(create_test_source_batch()), deadline_at=past
    )
    operator_id = create_operator_user()
    as_user(PERMS_ASSIGN, roles=("MANAGER",))
    client.post(
        f"/api/v1/inventory/campaigns/{campaign['id']}/assign",
        json={"user_id": str(operator_id), "expected_version": 1},
    )
    as_user(PERMS_READ)
    client.get(f"/api/v1/inventory/campaigns/{campaign['id']}")  # dispara EXPIRED

    future = (dt.datetime.now(dt.UTC) + dt.timedelta(days=1)).isoformat()
    as_user(PERMS_CREATE, roles=("MANAGER",))
    assert (
        client.post(
            f"/api/v1/inventory/campaigns/{campaign['id']}/reopen",
            json={"reason": "x", "new_deadline_at": future, "expected_version": 3},
        ).status_code
        == 403
    )

    as_user(PERMS_REOPEN, roles=("ADMIN",))
    assert (
        client.post(
            f"/api/v1/inventory/campaigns/{campaign['id']}/reopen",
            json={"reason": "", "new_deadline_at": future, "expected_version": 3},
        ).status_code
        == 422
    )
    reopened = client.post(
        f"/api/v1/inventory/campaigns/{campaign['id']}/reopen",
        json={"reason": "prorroga de prueba", "new_deadline_at": future, "expected_version": 3},
    )
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "IN_PROGRESS"
    cleanup_inventory_test_data()
