"""Tests de conciliacion F008: preview, seleccion, aprobacion, blind y concurrencia."""

from __future__ import annotations

import decimal
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy import func, select

from app.db.session import SessionLocal
from app.models import (
    AuditEvent,
    InventoryCampaign,
    InventoryCountEvent,
    InventoryCountSession,
    InventoryCountTotal,
    InventoryReconciliation,
    InventoryRecount,
    InventorySnapshotItem,
    InventoryUnknownCode,
)
from app.models.enums import (
    CampaignStatus,
    ReconciliationStatus,
    SelectionMode,
    SessionStatus,
)
from tests.auth_helpers import override_auth
from tests.inventory_helpers import (
    PERMS_MONITOR,
    PERMS_READ,
    as_user,
    batch_products,
    cleanup_inventory_test_data,
    client,
    create_operator_user,
    create_standalone_product,
)
from tests.test_count_engine import _event, _ready_to_count, _start_session
from tests.test_exceptions_damage import _post_event, _unknown_row


def _d(value: object) -> decimal.Decimal:
    return decimal.Decimal(str(value))


def _as_manager() -> uuid.UUID:
    return as_user(
        {
            "inventory.read",
            "inventory.monitor",
            "inventory.assign",
            "inventory.create",
            "inventory.recount",
            "inventory.reconcile",
            "inventory.approve",
            "inventory.expected.read",
            "inventory.reopen",
        },
        roles=("MANAGER",),
    )


def _as_operator(operator_id: uuid.UUID) -> None:
    override_auth({"inventory.count", "inventory.read"}, user_id=operator_id)


def _campaign(campaign_id: str) -> dict[str, Any]:
    response = client.get(f"/api/v1/inventory/campaigns/{campaign_id}")
    assert response.status_code == 200, response.text
    return response.json()


def _product_map(batch_id: str) -> dict[str, uuid.UUID]:
    return {ref.split("-")[1]: pid for pid, ref in batch_products(batch_id)}


def _submit_ok(session: dict[str, Any]) -> dict[str, Any]:
    response = client.post(
        f"/api/v1/inventory/count-sessions/{session['id']}/submit",
        json={"expected_version": session["version"], "confirm_missing": True},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _request_recount(campaign_id: str, assigned_user_id: uuid.UUID) -> tuple[int, dict[str, Any]]:
    response = client.post(
        f"/api/v1/inventory/campaigns/{campaign_id}/recounts",
        json={"assigned_user_id": str(assigned_user_id)},
    )
    return response.status_code, (response.json() if response.content else {})


def _preview(campaign_id: str) -> tuple[int, dict[str, Any]]:
    response = client.get(f"/api/v1/inventory/campaigns/{campaign_id}/reconciliation/preview")
    return response.status_code, (response.json() if response.content else {})


def _prepare(
    campaign_id: str, *, version: int, refresh: bool = False
) -> tuple[int, dict[str, Any]]:
    response = client.post(
        f"/api/v1/inventory/campaigns/{campaign_id}/reconciliation/prepare",
        json={"expected_campaign_version": version, "refresh": refresh},
    )
    return response.status_code, (response.json() if response.content else {})


def _list_recon(campaign_id: str) -> tuple[int, dict[str, Any]]:
    response = client.get(f"/api/v1/inventory/campaigns/{campaign_id}/reconciliation")
    return response.status_code, (response.json() if response.content else {})


def _sessions_endpoint(campaign_id: str) -> tuple[int, list[dict[str, Any]]]:
    response = client.get(f"/api/v1/inventory/campaigns/{campaign_id}/reconciliation/sessions")
    return response.status_code, (response.json() if response.content else [])


def _select(
    campaign_id: str, session_id: str, version: int
) -> tuple[int, dict[str, Any]]:
    response = client.post(
        f"/api/v1/inventory/campaigns/{campaign_id}/reconciliation/select-session",
        json={"session_id": session_id, "expected_campaign_version": version},
    )
    return response.status_code, (response.json() if response.content else {})


def _override(
    campaign_id: str,
    product_id: uuid.UUID,
    session_id: str,
    *,
    version: int,
    reason: str | None = None,
    observation: str | None = None,
) -> tuple[int, dict[str, Any]]:
    body: dict[str, object] = {
        "selected_session_id": session_id,
        "expected_version": version,
    }
    if reason is not None:
        body["reason"] = reason
    if observation is not None:
        body["observation"] = observation
    response = client.patch(
        f"/api/v1/inventory/campaigns/{campaign_id}/reconciliation/products/{product_id}",
        json=body,
    )
    return response.status_code, (response.json() if response.content else {})


def _approve(campaign_id: str, version: int) -> tuple[int, dict[str, Any]]:
    response = client.post(
        f"/api/v1/inventory/campaigns/{campaign_id}/reconciliation/approve",
        json={"expected_campaign_version": version},
    )
    return response.status_code, (response.json() if response.content else {})


def _recon_rows(campaign_id: str) -> list[InventoryReconciliation]:
    with SessionLocal() as db:
        rows = list(
            db.execute(
                select(InventoryReconciliation)
                .where(InventoryReconciliation.inventory_campaign_id == uuid.UUID(campaign_id))
                .order_by(InventoryReconciliation.product_id)
            ).scalars()
        )
        for row in rows:
            db.refresh(row)
        return rows


def _row_for(campaign_id: str, product_id: uuid.UUID) -> InventoryReconciliation:
    with SessionLocal() as db:
        row = db.execute(
            select(InventoryReconciliation).where(
                InventoryReconciliation.inventory_campaign_id == uuid.UUID(campaign_id),
                InventoryReconciliation.product_id == product_id,
            )
        ).scalar_one()
        db.refresh(row)
        return row


def _campaign_row(campaign_id: str) -> InventoryCampaign:
    with SessionLocal() as db:
        row = db.get(InventoryCampaign, uuid.UUID(campaign_id))
        assert row is not None
        db.refresh(row)
        return row


def _session_rows(campaign_id: str) -> list[InventoryCountSession]:
    with SessionLocal() as db:
        return list(
            db.execute(
                select(InventoryCountSession)
                .where(InventoryCountSession.inventory_campaign_id == uuid.UUID(campaign_id))
                .order_by(InventoryCountSession.session_number)
            ).scalars()
        )


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


def _monetary_non_null(campaign_id: str) -> int:
    columns = (
        InventoryReconciliation.effective_unit_cost,
        InventoryReconciliation.missing_cost_value,
        InventoryReconciliation.damage_cost_value,
        InventoryReconciliation.surplus_cost_value,
        InventoryReconciliation.affected_sale_value,
    )
    total = 0
    with SessionLocal() as db:
        for column in columns:
            total += int(
                db.execute(
                    select(func.count())
                    .select_from(InventoryReconciliation)
                    .where(
                        InventoryReconciliation.inventory_campaign_id == uuid.UUID(campaign_id),
                        column.is_not(None),
                    )
                ).scalar_one()
            )
    return total


def _session_facts(campaign_id: str) -> dict[str, tuple[int, int, str, int]]:
    with SessionLocal() as db:
        facts: dict[str, tuple[int, int, str, int]] = {}
        for session in _session_rows(campaign_id):
            events = int(
                db.execute(
                    select(func.count())
                    .select_from(InventoryCountEvent)
                    .where(InventoryCountEvent.session_id == session.id)
                ).scalar_one()
            )
            totals = int(
                db.execute(
                    select(func.count())
                    .select_from(InventoryCountTotal)
                    .where(InventoryCountTotal.session_id == session.id)
                ).scalar_one()
            )
            facts[str(session.id)] = (
                events,
                totals,
                session.status.value,
                session.version,
            )
        return facts


def _snapshot_facts(campaign_id: str) -> tuple[str | None, int]:
    campaign = _campaign_row(campaign_id)
    with SessionLocal() as db:
        items = int(
            db.execute(
                select(func.count())
                .select_from(InventorySnapshotItem)
                .where(InventorySnapshotItem.inventory_campaign_id == campaign.id)
            ).scalar_one()
        )
    return campaign.snapshot_sha256, items


def _resolve_unknown(unknown_id: str, product_id: uuid.UUID) -> tuple[int, dict[str, Any]]:
    response = client.post(
        f"/api/v1/inventory/unknown-codes/{unknown_id}/resolve",
        json={"product_id": str(product_id)},
    )
    return response.status_code, (response.json() if response.content else {})


def _count_session(
    session_id: str, entries: list[tuple[str, uuid.UUID, object]]
) -> None:
    for event_type, product_id, quantity in entries:
        code, body = _event(
            session_id, event_type, product_id=product_id, quantity=quantity
        )
        assert code == 200, body


def _two_session_campaign(
    quantities: tuple[tuple[str, str], ...] = (("A", "10"), ("B", "5")),
    counts1: tuple[tuple[str, object], ...] = (("A", 8), ("B", 5)),
    counts2: tuple[tuple[str, object], ...] = (("A", 10), ("B", 4)),
) -> dict[str, Any]:
    campaign_id, batch_id, operator_id = _ready_to_count(quantities=quantities)
    products = _product_map(batch_id)
    _as_operator(operator_id)
    session1 = _start_session(campaign_id)
    _count_session(
        str(session1["id"]),
        [("MANUAL_ADD", products[letter], qty) for letter, qty in counts1],
    )
    _submit_ok(session1)

    recounter = create_operator_user()
    _as_manager()
    status, body = _request_recount(campaign_id, recounter)
    assert status == 200, body
    _as_operator(recounter)
    session2 = _start_session(campaign_id)
    _count_session(
        str(session2["id"]),
        [("MANUAL_ADD", products[letter], qty) for letter, qty in counts2],
    )
    _submit_ok(session2)
    return {
        "campaign_id": campaign_id,
        "batch_id": batch_id,
        "products": products,
        "operator_id": operator_id,
        "recounter": recounter,
        "session1_id": str(session1["id"]),
        "session2_id": str(session2["id"]),
    }


def _one_session_campaign(
    quantities: tuple[tuple[str, str], ...] = (("A", "10"),),
    counts: tuple[tuple[str, object], ...] = (("A", 10),),
) -> dict[str, Any]:
    campaign_id, batch_id, operator_id = _ready_to_count(quantities=quantities)
    products = _product_map(batch_id)
    _as_operator(operator_id)
    session = _start_session(campaign_id)
    _count_session(
        str(session["id"]),
        [("MANUAL_ADD", products[letter], qty) for letter, qty in counts],
    )
    _submit_ok(session)
    return {
        "campaign_id": campaign_id,
        "batch_id": batch_id,
        "products": products,
        "operator_id": operator_id,
        "session_id": str(session["id"]),
    }


def _prepared(ctx: dict[str, Any]) -> dict[str, Any]:
    _as_manager()
    campaign = _campaign(ctx["campaign_id"])
    status, body = _prepare(ctx["campaign_id"], version=campaign["version"])
    assert status == 200, body
    return body


# ---------------------------------- preview ----------------------------------


def test_preview_shows_expected_and_per_session_results() -> None:
    ctx = _two_session_campaign()
    _as_manager()
    status, body = _preview(ctx["campaign_id"])
    assert status == 200, body
    assert [s["session_number"] for s in body["submitted_sessions"]] == [1, 2]
    assert [s["session_type"] for s in body["submitted_sessions"]] == ["INITIAL", "RECOUNT"]
    products = body["products"]
    assert [p["product"]["internal_reference"].split("-")[1] for p in products] == ["A", "B"]
    product_a, product_b = products
    assert product_a["kind"] == "PRODUCT"
    assert _d(product_a["expected_quantity"]) == 10
    assert [_d(s["physical_quantity"]) for s in product_a["sessions"]] == [8, 10]
    assert [_d(s["damaged_quantity"]) for s in product_a["sessions"]] == [0, 0]
    assert _d(product_b["expected_quantity"]) == 5
    assert [_d(s["physical_quantity"]) for s in product_b["sessions"]] == [5, 4]
    assert product_a["is_extra"] is False
    assert body["unresolved_unknown_count"] == 0
    assert body["warnings"] == []
    assert body["unresolved_unknowns"] == []
    assert body["campaign"]["reconciliation_prepared_at"] is None

    dumped = str(body).lower()
    for forbidden in ("cost", "price", "money", "effective_unit"):
        assert forbidden not in dumped, forbidden

    status_sessions, sessions = _sessions_endpoint(ctx["campaign_id"])
    assert status_sessions == 200
    assert len(sessions) == 2
    cleanup_inventory_test_data()


# --------------------------- seleccion por defecto ----------------------------


def test_default_session_selection_matches_and_differences() -> None:
    ctx = _two_session_campaign()
    prepared = _prepared(ctx)
    assert prepared["already_prepared"] is False
    assert prepared["status"] == "UNDER_REVIEW"

    status, body = _select(ctx["campaign_id"], ctx["session2_id"], prepared["version"])
    assert status == 200, body
    assert body["products_updated"] == 2

    rows = {row.product_id: row for row in _recon_rows(ctx["campaign_id"])}
    row_a = rows[ctx["products"]["A"]]
    row_b = rows[ctx["products"]["B"]]
    assert row_a.status is ReconciliationStatus.MATCHED
    assert _d(row_a.approved_physical_quantity) == 10
    assert _d(row_a.difference_quantity) == 0
    assert _d(row_a.missing_quantity) == 0
    assert _d(row_a.surplus_quantity) == 0
    assert row_a.selection_mode is SelectionMode.DEFAULT_SESSION
    assert row_a.selected_session_id == uuid.UUID(ctx["session2_id"])
    assert row_b.status is ReconciliationStatus.DIFFERENCE
    assert _d(row_b.approved_physical_quantity) == 4
    assert _d(row_b.difference_quantity) == -1
    assert _d(row_b.missing_quantity) == 1
    assert _d(row_b.surplus_quantity) == 0
    assert row_b.selection_mode is SelectionMode.DEFAULT_SESSION
    assert row_b.version == 2
    assert row_b.selected_by is not None
    assert row_b.selected_at is not None

    assert _campaign(ctx["campaign_id"])["version"] == body["campaign_version"]
    assert _audit_count(
        "RECONCILIATION_DEFAULT_SESSION_SELECTED", ctx["campaign_id"]
    ) == 1

    status_list, listing = _list_recon(ctx["campaign_id"])
    assert status_list == 200
    assert listing["total"] == 2
    assert len(listing["items"]) == 2
    assert all(len(item["session_results"]) == 2 for item in listing["items"])
    item_b = next(
        item for item in listing["items"] if item["product"]["id"] == str(ctx["products"]["B"])
    )
    assert _d(item_b["difference_quantity"]) == -1
    assert "cost" not in str(listing).lower()
    cleanup_inventory_test_data()


def test_product_override_changes_only_that_product() -> None:
    ctx = _two_session_campaign()
    prepared = _prepared(ctx)
    status, _body = _select(ctx["campaign_id"], ctx["session2_id"], prepared["version"])
    assert status == 200

    row_b_before = _row_for(ctx["campaign_id"], ctx["products"]["B"])
    status, body = _override(
        ctx["campaign_id"],
        ctx["products"]["B"],
        ctx["session1_id"],
        version=row_b_before.version,
    )
    assert status == 200, body
    assert body["selection_mode"] == "PRODUCT_OVERRIDE"
    assert _d(body["approved_physical_quantity"]) == 5
    assert _d(body["difference_quantity"]) == 0
    assert body["status"] == "MATCHED"

    row_b = _row_for(ctx["campaign_id"], ctx["products"]["B"])
    row_a = _row_for(ctx["campaign_id"], ctx["products"]["A"])
    assert row_b.selection_mode is SelectionMode.PRODUCT_OVERRIDE
    assert row_b.version == row_b_before.version + 1
    assert row_a.selected_session_id == uuid.UUID(ctx["session2_id"])
    assert row_a.selection_mode is SelectionMode.DEFAULT_SESSION
    assert row_a.version == 2
    assert _audit_count("RECONCILIATION_PRODUCT_OVERRIDDEN", ctx["campaign_id"]) == 1
    cleanup_inventory_test_data()


# ------------------------------ extras y unknowns -----------------------------


def test_extra_product_has_zero_expected_and_surplus() -> None:
    campaign_id, batch_id, operator_id = _ready_to_count(quantities=(("A", "10"),))
    products = _product_map(batch_id)
    extra_id = create_standalone_product(f"TEST-EXTRA-{uuid.uuid4().hex[:6]}")
    _as_operator(operator_id)
    session = _start_session(campaign_id)
    session_id = str(session["id"])
    assert _event(session_id, "MANUAL_ADD", product_id=products["A"], quantity=10)[0] == 200
    for _ in range(3):
        assert _event(session_id, "QR_SCAN", product_id=extra_id)[0] == 200
    _submit_ok(session)

    _as_manager()
    status, preview_body = _preview(campaign_id)
    assert status == 200
    extra_item = next(p for p in preview_body["products"] if p["product"]["id"] == str(extra_id))
    assert extra_item["is_extra"] is True
    assert _d(extra_item["expected_quantity"]) == 0
    assert _d(extra_item["sessions"][0]["physical_quantity"]) == 3

    campaign = _campaign(campaign_id)
    status, prepared = _prepare(campaign_id, version=campaign["version"])
    assert status == 200, prepared
    status, _body = _select(campaign_id, str(session["id"]), prepared["version"])
    assert status == 200

    row_extra = _row_for(campaign_id, extra_id)
    assert row_extra.status is ReconciliationStatus.DIFFERENCE
    assert _d(row_extra.expected_quantity) == 0
    assert _d(row_extra.approved_physical_quantity) == 3
    assert _d(row_extra.difference_quantity) == 3
    assert _d(row_extra.surplus_quantity) == 3
    assert _d(row_extra.missing_quantity) == 0
    assert _row_for(campaign_id, products["A"]).status is ReconciliationStatus.MATCHED
    assert _monetary_non_null(campaign_id) == 0
    cleanup_inventory_test_data()


def test_resolved_unknown_counts_into_effective_without_touching_totals() -> None:
    campaign_id, batch_id, operator_id = _ready_to_count(quantities=(("A", "10"),))
    products = _product_map(batch_id)
    product_a = products["A"]
    _as_operator(operator_id)
    session = _start_session(campaign_id)
    session_id = str(session["id"])
    assert _event(session_id, "MANUAL_ADD", product_id=product_a, quantity=2)[0] == 200
    for _ in range(3):
        assert _event(session_id, "QR_SCAN", code="UNK-XYZ")[0] == 200
    _submit_ok(session)

    _as_manager()
    unknown = _unknown_row(session_id, "UNK-XYZ")
    assert _d(unknown.quantity) == 3
    status, body = _resolve_unknown(str(unknown.id), product_a)
    assert status == 200, body

    status, preview_body = _preview(campaign_id)
    assert status == 200
    assert preview_body["unresolved_unknown_count"] == 0
    item_a = next(p for p in preview_body["products"] if p["product"]["id"] == str(product_a))
    assert _d(item_a["sessions"][0]["physical_quantity"]) == 5

    with SessionLocal() as db:
        total_qty = db.execute(
            select(InventoryCountTotal.quantity).where(
                InventoryCountTotal.session_id == uuid.UUID(session_id),
                InventoryCountTotal.product_id == product_a,
            )
        ).scalar_one()
        unknown_row = db.get(InventoryUnknownCode, unknown.id)
    assert _d(total_qty) == 2
    assert unknown_row is not None
    assert _d(unknown_row.quantity) == 3
    assert unknown_row.resolved_product_id == product_a

    campaign = _campaign(campaign_id)
    status, prepared = _prepare(campaign_id, version=campaign["version"])
    assert status == 200, prepared
    status, _body = _select(campaign_id, session_id, prepared["version"])
    assert status == 200
    row_a = _row_for(campaign_id, product_a)
    assert _d(row_a.approved_physical_quantity) == 5
    assert _d(row_a.difference_quantity) == -5
    cleanup_inventory_test_data()


def test_unresolved_unknown_blocks_approve_only() -> None:
    campaign_id, batch_id, operator_id = _ready_to_count(quantities=(("A", "10"),))
    products = _product_map(batch_id)
    product_a = products["A"]
    _as_operator(operator_id)
    session = _start_session(campaign_id)
    session_id = str(session["id"])
    assert _event(session_id, "MANUAL_ADD", product_id=product_a, quantity=9)[0] == 200
    assert _event(session_id, "QR_SCAN", code="UNK-BLOCK")[0] == 200
    _submit_ok(session)

    _as_manager()
    status, preview_body = _preview(campaign_id)
    assert status == 200
    assert preview_body["unresolved_unknown_count"] == 1
    assert preview_body["unresolved_unknowns"][0]["kind"] == "UNRESOLVED_UNKNOWN"
    assert preview_body["warnings"] == []

    campaign = _campaign(campaign_id)
    status, prepared = _prepare(campaign_id, version=campaign["version"])
    assert status == 200, prepared

    status, body = _approve(campaign_id, prepared["version"])
    assert status == 409
    assert body["detail"]["error"] == "UNRESOLVED_UNKNOWN_CODES"
    assert body["detail"]["unresolved_unknown_count"] == 1

    unknown = _unknown_row(session_id, "UNK-BLOCK")
    status, _body = _resolve_unknown(str(unknown.id), product_a)
    assert status == 200

    campaign = _campaign(campaign_id)
    status, body = _approve(campaign_id, campaign["version"])
    assert status == 409
    assert body["detail"]["error"] == "RECONCILIATION_NOT_COMPLETE"

    status, _body = _select(campaign_id, session_id, campaign["version"])
    assert status == 200
    campaign = _campaign(campaign_id)
    status, body = _approve(campaign_id, campaign["version"])
    assert status == 200, body
    assert body["already_approved"] is False
    cleanup_inventory_test_data()


# ------------------------- damage / missing / surplus --------------------------


def test_damage_reported_without_changing_difference() -> None:
    campaign_id, batch_id, operator_id = _ready_to_count(quantities=(("A", "10"),))
    products = _product_map(batch_id)
    product_a = products["A"]
    _as_operator(operator_id)
    session = _start_session(campaign_id)
    session_id = str(session["id"])
    assert _event(session_id, "MANUAL_ADD", product_id=product_a, quantity=10)[0] == 200
    status, body = _post_event(
        session_id,
        "DAMAGE_ADD",
        product_id=product_a,
        quantity=2,
        reason="dano en vitrina",
    )
    assert status == 200, body
    _submit_ok(session)

    _as_manager()
    campaign = _campaign(campaign_id)
    status, prepared = _prepare(campaign_id, version=campaign["version"])
    assert status == 200, prepared
    status, _body = _select(campaign_id, session_id, prepared["version"])
    assert status == 200

    row = _row_for(campaign_id, product_a)
    assert row.status is ReconciliationStatus.MATCHED
    assert _d(row.approved_physical_quantity) == 10
    assert _d(row.damaged_quantity) == 2
    assert _d(row.difference_quantity) == 0
    assert _d(row.missing_quantity) == 0
    assert _d(row.surplus_quantity) == 0
    cleanup_inventory_test_data()


def test_missing_and_surplus_quantities() -> None:
    ctx = _one_session_campaign(
        quantities=(("A", "10"), ("B", "5")),
        counts=(("A", 7), ("B", 7)),
    )
    _as_manager()
    campaign = _campaign(ctx["campaign_id"])
    status, prepared = _prepare(ctx["campaign_id"], version=campaign["version"])
    assert status == 200, prepared
    status, _body = _select(ctx["campaign_id"], ctx["session_id"], prepared["version"])
    assert status == 200

    row_a = _row_for(ctx["campaign_id"], ctx["products"]["A"])
    assert row_a.status is ReconciliationStatus.DIFFERENCE
    assert _d(row_a.difference_quantity) == -3
    assert _d(row_a.missing_quantity) == 3
    assert _d(row_a.surplus_quantity) == 0

    row_b = _row_for(ctx["campaign_id"], ctx["products"]["B"])
    assert row_b.status is ReconciliationStatus.DIFFERENCE
    assert _d(row_b.difference_quantity) == 2
    assert _d(row_b.missing_quantity) == 0
    assert _d(row_b.surplus_quantity) == 2
    cleanup_inventory_test_data()


# ------------------------------ expected negativo -----------------------------


def test_negative_expected_warns_and_blocks_approve() -> None:
    ctx = _one_session_campaign(
        quantities=(("A", "10"), ("B", "-2")),
        counts=(("A", 10),),
    )
    _as_manager()
    status, preview_body = _preview(ctx["campaign_id"])
    assert status == 200
    assert "NEGATIVE_EXPECTED_QUANTITY" in preview_body["warnings"]
    item_b = next(
        p
        for p in preview_body["products"]
        if p["product"]["id"] == str(ctx["products"]["B"])
    )
    assert item_b["negative_expected"] is True
    assert _d(item_b["expected_quantity"]) == -2

    campaign = _campaign(ctx["campaign_id"])
    status, prepared = _prepare(ctx["campaign_id"], version=campaign["version"])
    assert status == 200, prepared
    status, _body = _select(ctx["campaign_id"], ctx["session_id"], prepared["version"])
    assert status == 200

    row_b = _row_for(ctx["campaign_id"], ctx["products"]["B"])
    assert row_b.status is ReconciliationStatus.UNDER_REVIEW
    assert _d(row_b.approved_physical_quantity) == 0
    assert row_b.difference_quantity is None
    assert _d(row_b.missing_quantity) == 0
    assert _d(row_b.surplus_quantity) == 0

    campaign = _campaign(ctx["campaign_id"])
    status, body = _approve(ctx["campaign_id"], campaign["version"])
    assert status == 409
    assert (
        body["detail"]["error"]
        == "NEGATIVE_EXPECTED_QUANTITY_REQUIRES_SOURCE_CORRECTION"
    )
    cleanup_inventory_test_data()


# --------------------------- reason obligatorio -------------------------------


def test_difference_requires_reason_before_approve() -> None:
    ctx = _two_session_campaign()
    prepared = _prepared(ctx)
    status, _body = _select(ctx["campaign_id"], ctx["session2_id"], prepared["version"])
    assert status == 200

    campaign = _campaign(ctx["campaign_id"])
    status, body = _approve(ctx["campaign_id"], campaign["version"])
    assert status == 409
    assert body["detail"]["error"] == "DIFFERENCE_REASON_REQUIRED"
    assert body["detail"]["product_id"] == str(ctx["products"]["B"])

    row_b = _row_for(ctx["campaign_id"], ctx["products"]["B"])
    status, body = _override(
        ctx["campaign_id"],
        ctx["products"]["B"],
        ctx["session2_id"],
        version=row_b.version,
        reason="diferencia de bulto confirmada",
    )
    assert status == 200, body
    assert body["reason"] == "diferencia de bulto confirmada"

    campaign = _campaign(ctx["campaign_id"])
    status, body = _approve(ctx["campaign_id"], campaign["version"])
    assert status == 200, body
    assert _row_for(ctx["campaign_id"], ctx["products"]["B"]).reason == (
        "diferencia de bulto confirmada"
    )
    cleanup_inventory_test_data()


def test_override_to_match_does_not_require_reason() -> None:
    ctx = _two_session_campaign()
    prepared = _prepared(ctx)
    status, _body = _select(ctx["campaign_id"], ctx["session2_id"], prepared["version"])
    assert status == 200

    row_b = _row_for(ctx["campaign_id"], ctx["products"]["B"])
    status, body = _override(
        ctx["campaign_id"], ctx["products"]["B"], ctx["session1_id"], version=row_b.version
    )
    assert status == 200, body

    campaign = _campaign(ctx["campaign_id"])
    status, body = _approve(ctx["campaign_id"], campaign["version"])
    assert status == 200, body
    cleanup_inventory_test_data()


# ------------------------- fingerprint y refresh ------------------------------


def test_source_change_requires_explicit_refresh() -> None:
    ctx = _two_session_campaign()
    prepared = _prepared(ctx)
    status, _body = _select(ctx["campaign_id"], ctx["session2_id"], prepared["version"])
    assert status == 200
    sha_before = _campaign_row(ctx["campaign_id"]).reconciliation_source_sha256
    facts_before = _session_facts(ctx["campaign_id"])
    snapshot_before = _snapshot_facts(ctx["campaign_id"])

    operator3 = create_operator_user()
    _as_manager()
    status, recount = _request_recount(ctx["campaign_id"], operator3)
    assert status == 200, recount
    _as_operator(operator3)
    session3 = _start_session(ctx["campaign_id"])
    _submit_ok(session3)

    _as_manager()
    campaign = _campaign(ctx["campaign_id"])
    assert campaign["status"] == "SUBMITTED"
    status, body = _prepare(ctx["campaign_id"], version=campaign["version"])
    assert status == 409
    assert body["detail"]["error"] == "RECONCILIATION_SOURCE_CHANGED"

    status, body = _prepare(ctx["campaign_id"], version=campaign["version"], refresh=True)
    assert status == 200, body
    assert body["refreshed"] is True
    assert body["status"] == "UNDER_REVIEW"
    assert body["reconciliation_source_sha256"] != sha_before

    rows = _recon_rows(ctx["campaign_id"])
    assert len(rows) == 2
    for row in rows:
        assert row.status is ReconciliationStatus.PENDING
        assert row.selected_session_id is None
        assert row.selection_mode is None
        assert row.approved_physical_quantity is None
        assert row.version == 3

    facts_after = _session_facts(ctx["campaign_id"])
    assert {sid: value for sid, value in facts_after.items() if sid in facts_before} == (
        facts_before
    )
    assert _snapshot_facts(ctx["campaign_id"]) == snapshot_before
    assert _audit_count("RECONCILIATION_PREPARED", ctx["campaign_id"]) == 1
    assert _audit_count("RECONCILIATION_REFRESHED", ctx["campaign_id"]) == 1
    cleanup_inventory_test_data()


# --------------------------------- aprobacion ---------------------------------


def test_approve_completes_workflow_without_money() -> None:
    ctx = _two_session_campaign(counts2=(("A", 10), ("B", 5)))
    prepared = _prepared(ctx)
    status, selected = _select(ctx["campaign_id"], ctx["session2_id"], prepared["version"])
    assert status == 200, selected

    status, body = _approve(ctx["campaign_id"], selected["campaign_version"])
    assert status == 200, body
    assert body["already_approved"] is False
    assert body["status"] == "APPROVED"
    assert body["products_approved"] == 2

    campaign = _campaign_row(ctx["campaign_id"])
    assert campaign.status is CampaignStatus.APPROVED
    assert campaign.approved_at is not None
    assert campaign.approved_by is not None
    assert campaign.version == selected["campaign_version"] + 1

    for row in _recon_rows(ctx["campaign_id"]):
        assert row.status is ReconciliationStatus.APPROVED
        assert row.approved_at is not None
        assert row.approved_by is not None
    assert _monetary_non_null(ctx["campaign_id"]) == 0
    assert _audit_count("RECONCILIATION_APPROVED", ctx["campaign_id"]) == 1
    assert _audit_count("CAMPAIGN_APPROVED", ctx["campaign_id"]) == 1
    cleanup_inventory_test_data()


def test_approve_is_idempotent() -> None:
    ctx = _two_session_campaign(counts2=(("A", 10), ("B", 5)))
    prepared = _prepared(ctx)
    status, selected = _select(ctx["campaign_id"], ctx["session2_id"], prepared["version"])
    assert status == 200
    status, body = _approve(ctx["campaign_id"], selected["campaign_version"])
    assert status == 200, body

    version_after = _campaign_row(ctx["campaign_id"]).version
    status, again = _approve(ctx["campaign_id"], selected["campaign_version"])
    assert status == 200
    assert again["already_approved"] is True
    assert _campaign_row(ctx["campaign_id"]).version == version_after
    assert _audit_count("RECONCILIATION_APPROVED", ctx["campaign_id"]) == 1
    assert _audit_count("CAMPAIGN_APPROVED", ctx["campaign_id"]) == 1
    cleanup_inventory_test_data()


def test_reconciliation_frozen_after_approval() -> None:
    ctx = _two_session_campaign(counts2=(("A", 10), ("B", 5)))
    prepared = _prepared(ctx)
    status, selected = _select(ctx["campaign_id"], ctx["session2_id"], prepared["version"])
    assert status == 200
    status, _body = _approve(ctx["campaign_id"], selected["campaign_version"])
    assert status == 200

    campaign = _campaign(ctx["campaign_id"])
    status, body = _prepare(ctx["campaign_id"], version=campaign["version"], refresh=True)
    assert status == 409
    assert body["detail"]["error"] == "RECONCILIATION_APPROVED"

    status, body = _select(
        ctx["campaign_id"], ctx["session2_id"], campaign["version"]
    )
    assert status == 409
    assert body["detail"]["error"] == "RECONCILIATION_APPROVED"

    row_b = _row_for(ctx["campaign_id"], ctx["products"]["B"])
    status, body = _override(
        ctx["campaign_id"],
        ctx["products"]["B"],
        ctx["session1_id"],
        version=row_b.version,
    )
    assert status == 409
    assert body["detail"]["error"] == "RECONCILIATION_APPROVED"
    cleanup_inventory_test_data()


def test_sessions_and_snapshot_immutable_across_f008() -> None:
    ctx = _two_session_campaign()
    facts_before = _session_facts(ctx["campaign_id"])
    snapshot_before = _snapshot_facts(ctx["campaign_id"])
    events_before = sum(value[0] for value in facts_before.values())
    totals_before = sum(value[1] for value in facts_before.values())

    prepared = _prepared(ctx)
    status, selected = _select(ctx["campaign_id"], ctx["session2_id"], prepared["version"])
    assert status == 200
    row_b = _row_for(ctx["campaign_id"], ctx["products"]["B"])
    status, _body = _override(
        ctx["campaign_id"],
        ctx["products"]["B"],
        ctx["session1_id"],
        version=row_b.version,
        observation="revisado",
    )
    assert status == 200
    campaign = _campaign(ctx["campaign_id"])
    status, _body = _approve(ctx["campaign_id"], campaign["version"])
    assert status == 200

    facts_after = _session_facts(ctx["campaign_id"])
    assert facts_after == facts_before
    assert sum(value[0] for value in facts_after.values()) == events_before
    assert sum(value[1] for value in facts_after.values()) == totals_before
    assert _snapshot_facts(ctx["campaign_id"]) == snapshot_before
    cleanup_inventory_test_data()


# --------------------------- multiples sesiones -------------------------------


def test_preview_shows_three_submitted_sessions_after_recounts() -> None:
    ctx = _two_session_campaign()
    operator3 = create_operator_user()
    _as_manager()
    status, body = _request_recount(ctx["campaign_id"], operator3)
    assert status == 200, body
    _as_operator(operator3)
    session3 = _start_session(ctx["campaign_id"])
    _submit_ok(session3)

    _as_manager()
    status, body = _preview(ctx["campaign_id"])
    assert status == 200
    assert [s["session_number"] for s in body["submitted_sessions"]] == [1, 2, 3]
    for product in body["products"]:
        assert len(product["sessions"]) == 3
    cleanup_inventory_test_data()


# ------------------------------- sesion cancelada -----------------------------


def test_cancelled_session_is_not_eligible() -> None:
    campaign_id, batch_id, operator_id = _ready_to_count(quantities=(("A", "10"),))
    products = _product_map(batch_id)
    _as_operator(operator_id)
    session1 = _start_session(campaign_id)
    assert _event(str(session1["id"]), "MANUAL_ADD", product_id=products["A"], quantity=3)[
        0
    ] == 200
    _submit_ok(session1)

    operator2 = create_operator_user()
    operator3 = create_operator_user()
    _as_manager()
    status, body = _request_recount(campaign_id, operator2)
    assert status == 200, body

    _as_operator(operator2)
    session2 = _start_session(campaign_id)
    assert (
        _event(str(session2["id"]), "MANUAL_ADD", product_id=products["A"], quantity=7)[0]
        == 200
    )

    _as_manager()
    with SessionLocal() as db:
        recount_id = db.execute(
            select(InventoryRecount.id)
            .where(InventoryRecount.inventory_campaign_id == uuid.UUID(campaign_id))
            .order_by(InventoryRecount.created_at.desc())
            .limit(1)
        ).scalar_one()
    response = client.post(
        f"/api/v1/inventory/recounts/{recount_id}/reassign",
        json={"user_id": str(operator3), "expected_version": 2},
    )
    assert response.status_code == 200, response.text

    cancelled = _session_rows(campaign_id)[1]
    assert cancelled.id == uuid.UUID(str(session2["id"]))
    assert cancelled.status is SessionStatus.CANCELLED

    _as_operator(operator3)
    session3 = _start_session(campaign_id)
    assert _event(str(session3["id"]), "MANUAL_ADD", product_id=products["A"], quantity=10)[
        0
    ] == 200
    _submit_ok(session3)

    _as_manager()
    status, preview_body = _preview(campaign_id)
    assert status == 200
    assert [s["id"] for s in preview_body["submitted_sessions"]] == [
        str(session1["id"]),
        str(session3["id"]),
    ]
    assert preview_body["products"][0]["sessions"][1]["session_id"] == str(session3["id"])

    campaign = _campaign(campaign_id)
    status, prepared = _prepare(campaign_id, version=campaign["version"])
    assert status == 200, prepared

    status, body = _select(campaign_id, str(session2["id"]), prepared["version"])
    assert status == 409
    assert body["detail"]["error"] == "SESSION_NOT_ELIGIBLE"

    status, body = _select(campaign_id, str(session3["id"]), prepared["version"])
    assert status == 200, body
    assert _row_for(campaign_id, products["A"]).selected_session_id == uuid.UUID(
        str(session3["id"])
    )
    cleanup_inventory_test_data()


# ------------------------- prepare: validaciones ------------------------------


def test_prepare_rejects_bad_status_and_stale_version() -> None:
    campaign_id, _batch, _operator = _ready_to_count()
    _as_manager()
    campaign = _campaign(campaign_id)
    status, body = _prepare(campaign_id, version=campaign["version"])
    assert status == 409
    assert body["detail"]["error"] == "CAMPAIGN_NOT_RECONCILABLE"
    cleanup_inventory_test_data()

    ctx = _two_session_campaign()
    _as_manager()
    campaign = _campaign(ctx["campaign_id"])
    status, body = _prepare(ctx["campaign_id"], version=campaign["version"] + 5)
    assert status == 409
    assert "Conflicto de version" in body["detail"]["message"]
    cleanup_inventory_test_data()


def test_prepare_is_idempotent_when_source_unchanged() -> None:
    ctx = _two_session_campaign()
    prepared = _prepared(ctx)
    version_after_first = prepared["version"]

    status, again = _prepare(ctx["campaign_id"], version=version_after_first)
    assert status == 200, again
    assert again["already_prepared"] is True
    assert again["version"] == version_after_first
    assert _audit_count("RECONCILIATION_PREPARED", ctx["campaign_id"]) == 1

    status, stale = _prepare(ctx["campaign_id"], version=version_after_first + 9)
    assert status == 200, stale
    assert stale["already_prepared"] is True
    cleanup_inventory_test_data()


# ----------------------------------- blind ------------------------------------


def test_f008_endpoints_require_reconcile_or_approve() -> None:
    ctx = _two_session_campaign()
    operator = ctx["operator_id"]
    _as_operator(operator)
    campaign = _campaign(ctx["campaign_id"])
    session_id = ctx["session2_id"]

    assert _preview(ctx["campaign_id"])[0] == 403
    assert _prepare(ctx["campaign_id"], version=campaign["version"])[0] == 403
    assert _list_recon(ctx["campaign_id"])[0] == 403
    assert _sessions_endpoint(ctx["campaign_id"])[0] == 403
    assert _select(ctx["campaign_id"], session_id, campaign["version"])[0] == 403
    assert _approve(ctx["campaign_id"], campaign["version"])[0] == 403
    assert (
        _override(
            ctx["campaign_id"],
            ctx["products"]["A"],
            session_id,
            version=1,
        )[0]
        == 403
    )

    as_user(PERMS_READ)
    assert _preview(ctx["campaign_id"])[0] == 403
    assert _list_recon(ctx["campaign_id"])[0] == 403

    as_user({"inventory.reconcile"})
    assert _preview(ctx["campaign_id"])[0] == 200
    assert _approve(ctx["campaign_id"], campaign["version"])[0] == 403

    as_user(PERMS_MONITOR | {"inventory.reconcile"})
    assert _list_recon(ctx["campaign_id"])[0] == 200
    cleanup_inventory_test_data()


# -------------------------------- concurrencia --------------------------------


def test_concurrent_select_session_only_one_wins() -> None:
    ctx = _two_session_campaign()
    prepared = _prepared(ctx)
    version = prepared["version"]

    def _try(_index: int) -> int:
        return _select(ctx["campaign_id"], ctx["session2_id"], version)[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(_try, [0, 1]))

    assert sorted(codes) == [200, 409], codes
    rows = _recon_rows(ctx["campaign_id"])
    assert all(row.selection_mode is SelectionMode.DEFAULT_SESSION for row in rows)
    selected_ids = {row.selected_session_id for row in rows}
    assert selected_ids == {uuid.UUID(ctx["session2_id"])}
    cleanup_inventory_test_data()


def test_concurrent_override_same_product_only_one_wins() -> None:
    ctx = _two_session_campaign()
    prepared = _prepared(ctx)
    status, _body = _select(ctx["campaign_id"], ctx["session2_id"], prepared["version"])
    assert status == 200
    row_version = _row_for(ctx["campaign_id"], ctx["products"]["B"]).version

    def _try(_index: int) -> int:
        return _override(
            ctx["campaign_id"],
            ctx["products"]["B"],
            ctx["session1_id"],
            version=row_version,
        )[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(_try, [0, 1]))

    assert sorted(codes) == [200, 409], codes
    row_b = _row_for(ctx["campaign_id"], ctx["products"]["B"])
    assert row_b.version == row_version + 1
    assert row_b.selection_mode is SelectionMode.PRODUCT_OVERRIDE
    cleanup_inventory_test_data()


def test_concurrent_approve_and_override_stay_coherent() -> None:
    ctx = _two_session_campaign(counts2=(("A", 10), ("B", 5)))
    prepared = _prepared(ctx)
    status, selected = _select(ctx["campaign_id"], ctx["session2_id"], prepared["version"])
    assert status == 200
    row_version = _row_for(ctx["campaign_id"], ctx["products"]["B"]).version
    campaign_version = selected["campaign_version"]

    def _approve_try(_index: int) -> int:
        return _approve(ctx["campaign_id"], campaign_version)[0]

    def _override_try(_index: int) -> int:
        return _override(
            ctx["campaign_id"],
            ctx["products"]["B"],
            ctx["session2_id"],
            version=row_version,
            reason="verificado en paralelo",
        )[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        future_approve = pool.submit(_approve_try, 0)
        future_override = pool.submit(_override_try, 1)
        approve_code = future_approve.result()
        override_code = future_override.result()

    assert approve_code == 200, approve_code
    assert override_code in (200, 409), override_code
    assert _campaign(ctx["campaign_id"])["status"] == "APPROVED"
    rows = _recon_rows(ctx["campaign_id"])
    assert all(row.status is ReconciliationStatus.APPROVED for row in rows)
    assert _monetary_non_null(ctx["campaign_id"]) == 0
    cleanup_inventory_test_data()
