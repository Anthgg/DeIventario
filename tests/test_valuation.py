"""Tests de valorizacion economica F009: calculo, idempotencia, blind y regresion."""

from __future__ import annotations

import decimal
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import select

from app.db.session import SessionLocal
from app.models import AuditEvent, InventoryReconciliation, Product
from app.models.enums import CampaignStatus, ReconciliationStatus
from tests.auth_helpers import override_auth
from tests.inventory_helpers import (
    PERMS_ASSIGN,
    PERMS_CREATE,
    as_user,
    batch_products,
    cleanup_inventory_test_data,
    client,
    create_operator_user,
    create_standalone_product,
    create_test_source_batch,
)
from tests.test_count_engine import _event, _future, _ready_to_count, _start_session
from tests.test_exceptions_damage import _post_event, _unknown_row
from tests.test_reconciliation import (
    _approve,
    _as_manager,
    _as_operator,
    _audit_count,
    _campaign,
    _campaign_row,
    _d,
    _monetary_non_null,
    _override,
    _prepare,
    _product_map,
    _recon_rows,
    _resolve_unknown,
    _row_for,
    _session_rows,
    _submit_ok,
)

_BLIND_FORBIDDEN = (
    "missing_cost",
    "damage_cost",
    "surplus_cost",
    "affected_sale",
    "effective_unit_cost",
    "sale_price",
    "confirmed_loss",
    '"cost"',
    '"valuation"',
)


@pytest.fixture(autouse=True)
def _cleanup_after_valuation_test() -> Iterator[None]:
    yield
    cleanup_inventory_test_data()


def _unwrap(response: Any) -> dict[str, Any]:
    data = response.json() if response.content else {}
    if isinstance(data, dict) and isinstance(data.get("detail"), dict):
        return data["detail"]
    return data if isinstance(data, dict) else {}


def _vpreview(campaign_id: str) -> tuple[int, dict[str, Any]]:
    response = client.get(f"/api/v1/inventory/campaigns/{campaign_id}/valuation/preview")
    return response.status_code, _unwrap(response)


def _vcalculate(campaign_id: str, version: int) -> tuple[int, dict[str, Any]]:
    response = client.post(
        f"/api/v1/inventory/campaigns/{campaign_id}/valuation/calculate",
        json={"expected_campaign_version": version},
    )
    return response.status_code, _unwrap(response)


def _vread(campaign_id: str) -> tuple[int, dict[str, Any]]:
    response = client.get(f"/api/v1/inventory/campaigns/{campaign_id}/valuation")
    return response.status_code, _unwrap(response)


def _item_for(body: dict[str, Any], product_id: uuid.UUID) -> dict[str, Any]:
    for item in body["items"]:
        if item["product"]["id"] == str(product_id):
            return item
    raise AssertionError(f"producto {product_id} sin item de valorizacion")


def _frozen_campaign(
    quantities: tuple[tuple[str, str], ...],
    *,
    master: dict[str, dict[str, object]] | None = None,
) -> tuple[str, str, uuid.UUID]:
    """Campana IN_PROGRESS con snapshot ya congelado (master mutado ANTES del freeze)."""
    cleanup_inventory_test_data()
    as_user(PERMS_CREATE, roles=("MANAGER",))
    batch_id = str(create_test_source_batch(quantities=quantities))
    if master:
        with SessionLocal() as db:
            for pid, ref in batch_products(batch_id):
                updates = master.get(ref.split("-")[1])
                if not updates:
                    continue
                product = db.get(Product, pid)
                assert product is not None
                for attr, value in updates.items():
                    setattr(product, attr, value)
            db.commit()
    campaign = client.post(
        "/api/v1/inventory/campaigns",
        json={
            "name": f"test-val-{uuid.uuid4().hex[:8]}",
            "source_import_batch_id": batch_id,
            "deadline_at": _future(),
        },
    )
    assert campaign.status_code == 200, campaign.text
    campaign_id = campaign.json()["id"]
    operator_id = create_operator_user()
    as_user(PERMS_ASSIGN, roles=("MANAGER",))
    assigned = client.post(
        f"/api/v1/inventory/campaigns/{campaign_id}/assign",
        json={"user_id": str(operator_id), "expected_version": 1},
    )
    assert assigned.status_code == 200, assigned.text
    as_user(PERMS_CREATE, roles=("MANAGER",))
    started = client.post(
        f"/api/v1/inventory/campaigns/{campaign_id}/start", json={"expected_version": 2}
    )
    assert started.status_code == 200, started.text
    return campaign_id, batch_id, operator_id


def _finalize_approved(campaign_id: str) -> None:
    """Prepare -> override con reason -> approve (flujo F008)."""
    _as_manager()
    campaign = _campaign(campaign_id)
    status, prepared = _prepare(campaign_id, version=campaign["version"])
    assert status == 200, prepared
    sessions = [s for s in _session_rows(campaign_id) if s.status.value == "SUBMITTED"]
    assert len(sessions) == 1, [s.status for s in _session_rows(campaign_id)]
    session_id = str(sessions[0].id)
    for row in _recon_rows(campaign_id):
        status, body = _override(
            campaign_id,
            row.product_id,
            session_id,
            version=row.version,
            reason="revision F009",
        )
        assert status == 200, body
    campaign = _campaign(campaign_id)
    status, body = _approve(campaign_id, campaign["version"])
    assert status == 200, body


def _valued_campaign(
    quantities: tuple[tuple[str, str], ...],
    entries: list[tuple[str, object]],
    *,
    master: dict[str, dict[str, object]] | None = None,
    damages: list[tuple[str, object]] | None = None,
) -> dict[str, Any]:
    """Campana APPROVED lista para valorizar. entries: [(letra, cantidad)] de conteo."""
    campaign_id, batch_id, operator_id = _frozen_campaign(quantities, master=master)
    products = _product_map(batch_id)
    _as_operator(operator_id)
    session = _start_session(campaign_id)
    session_id = str(session["id"])
    for letter, qty in entries:
        status, body = _event(
            session_id, "MANUAL_ADD", product_id=products[letter], quantity=qty
        )
        assert status == 200, body
    for letter, qty in damages or []:
        status, body = _post_event(
            session_id,
            "DAMAGE_ADD",
            product_id=products[letter],
            quantity=qty,
            reason="dano F009",
        )
        assert status == 200, body
    _submit_ok(session)
    _finalize_approved(campaign_id)
    return {
        "campaign_id": campaign_id,
        "batch_id": batch_id,
        "operator_id": operator_id,
        "products": products,
        "session_id": session_id,
    }


def test_simple_match_has_zero_impact() -> None:
    ctx = _valued_campaign((("A", "5"),), [("A", 5)])
    _as_manager()
    status, preview = _vpreview(ctx["campaign_id"])
    assert status == 200, preview
    assert preview["currency"] == "PEN"
    assert preview["warnings"] == []
    assert len(preview["items"]) == 1
    item = preview["items"][0]
    assert _d(item["approved_physical_quantity"]) == 5
    assert _d(item["missing_quantity"]) == 0
    assert _d(item["surplus_quantity"]) == 0
    assert _d(item["damaged_quantity"]) == 0
    assert _d(item["effective_unit_cost"]) == 2
    assert item["cost_source"] == "COST"
    assert item["currency"] == "PEN"
    assert _d(item["missing_cost_value"]) == 0
    assert _d(item["damage_cost_value"]) == 0
    assert _d(item["surplus_cost_value"]) == 0
    assert _d(item["affected_sale_value"]) == 0
    assert item["warnings"] == []
    totals = preview["totals"]
    assert _d(totals["total_missing_units"]) == 0
    assert _d(totals["total_damaged_units"]) == 0
    assert _d(totals["total_surplus_units"]) == 0
    assert _d(totals["total_missing_cost"]) == 0
    assert _d(totals["total_damage_cost"]) == 0
    assert _d(totals["total_surplus_cost"]) == 0
    assert _d(totals["total_affected_sale_value"]) == 0
    assert _d(totals["total_confirmed_loss_cost"]) == 0

    version = _campaign(ctx["campaign_id"])["version"]
    status, body = _vcalculate(ctx["campaign_id"], version)
    assert status == 200, body
    assert body["already_calculated"] is False
    assert body["currency"] == "PEN"
    assert body["items_count"] == 1

    status, read = _vread(ctx["campaign_id"])
    assert status == 200, read
    assert read["summary"] == totals
    assert read["items"] == preview["items"]
    assert read["currency"] == "PEN"
    assert read["campaign"]["valuation_source_sha256"] == body["valuation_source_sha256"]


def test_missing_units_valued_at_cost() -> None:
    ctx = _valued_campaign((("A", "5"),), [("A", 3)])
    _as_manager()
    status, preview = _vpreview(ctx["campaign_id"])
    assert status == 200, preview
    item = preview["items"][0]
    assert _d(item["missing_quantity"]) == 2
    assert item["missing_cost_value"] == "4.0000"
    assert _d(item["missing_cost_value"]) == decimal.Decimal("4")
    assert _d(item["damage_cost_value"]) == 0
    assert _d(item["surplus_cost_value"]) == 0
    assert item["affected_sale_value"] == "20.0000"
    assert _d(preview["totals"]["total_missing_units"]) == 2
    assert _d(preview["totals"]["total_missing_cost"]) == 4
    assert _d(preview["totals"]["total_confirmed_loss_cost"]) == 4


def test_damage_does_not_reduce_physical_no_double_count() -> None:
    ctx = _valued_campaign((("A", "5"),), [("A", 5)], damages=[("A", 2)])
    _as_manager()
    status, preview = _vpreview(ctx["campaign_id"])
    assert status == 200, preview
    item = preview["items"][0]
    assert _d(item["approved_physical_quantity"]) == 5
    assert _d(item["missing_quantity"]) == 0
    assert _d(item["damaged_quantity"]) == 2
    assert _d(item["missing_cost_value"]) == 0
    assert item["damage_cost_value"] == "4.0000"
    assert item["affected_sale_value"] == "20.0000"
    assert _d(preview["totals"]["total_confirmed_loss_cost"]) == 4


def test_missing_and_damage_summed_without_overlap() -> None:
    ctx = _valued_campaign((("A", "5"),), [("A", 3)], damages=[("A", 2)])
    _as_manager()
    status, preview = _vpreview(ctx["campaign_id"])
    assert status == 200, preview
    item = preview["items"][0]
    assert _d(item["missing_quantity"]) == 2
    assert _d(item["damaged_quantity"]) == 2
    assert _d(item["missing_cost_value"]) == 4
    assert _d(item["damage_cost_value"]) == 4
    assert item["affected_sale_value"] == "40.0000"
    assert _d(preview["totals"]["total_confirmed_loss_cost"]) == 8
    assert _d(preview["totals"]["total_missing_cost"]) == 4
    assert _d(preview["totals"]["total_damage_cost"]) == 4


def test_surplus_is_never_netted_against_loss() -> None:
    ctx = _valued_campaign((("A", "5"),), [("A", 7)])
    _as_manager()
    status, preview = _vpreview(ctx["campaign_id"])
    assert status == 200, preview
    item = preview["items"][0]
    assert _d(item["surplus_quantity"]) == 2
    assert _d(item["missing_quantity"]) == 0
    assert item["surplus_cost_value"] == "4.0000"
    assert _d(item["missing_cost_value"]) == 0
    assert _d(item["affected_sale_value"]) == 0
    totals = preview["totals"]
    assert _d(totals["total_surplus_cost"]) == 4
    assert _d(totals["total_confirmed_loss_cost"]) == 0
    assert "loss" not in totals
    version = _campaign(ctx["campaign_id"])["version"]
    status, body = _vcalculate(ctx["campaign_id"], version)
    assert status == 200, body
    status, read = _vread(ctx["campaign_id"])
    assert status == 200, read
    assert _d(read["summary"]["total_surplus_cost"]) == 4
    assert _d(read["summary"]["total_confirmed_loss_cost"]) == 0


def test_master_price_change_after_freeze_is_ignored() -> None:
    ctx = _valued_campaign((("A", "5"),), [("A", 3)])
    product_id = ctx["products"]["A"]
    with SessionLocal() as db:
        product = db.get(Product, product_id)
        assert product is not None
        product.cost = decimal.Decimal("99")
        product.sale_price = decimal.Decimal("80")
        db.commit()
    _as_manager()
    status, preview = _vpreview(ctx["campaign_id"])
    assert status == 200, preview
    item = _item_for(preview, product_id)
    assert item["effective_unit_cost"] == "2.0000"
    assert item["missing_cost_value"] == "4.0000"
    assert item["affected_sale_value"] == "20.0000"
    version = _campaign(ctx["campaign_id"])["version"]
    status, body = _vcalculate(ctx["campaign_id"], version)
    assert status == 200, body
    status, read = _vread(ctx["campaign_id"])
    assert status == 200, read
    read_item = _item_for(read, product_id)
    assert read_item["effective_unit_cost"] == "2.0000"
    assert read_item["missing_cost_value"] == "4.0000"
    assert read_item["affected_sale_value"] == "20.0000"


def test_calculate_is_idempotent_without_version_bump_or_audit() -> None:
    ctx = _valued_campaign((("A", "5"),), [("A", 3)])
    _as_manager()
    version = _campaign(ctx["campaign_id"])["version"]
    status, first = _vcalculate(ctx["campaign_id"], version)
    assert status == 200, first
    assert first["already_calculated"] is False
    next_version = _campaign(ctx["campaign_id"])["version"]
    assert next_version == version + 1
    status, second = _vcalculate(ctx["campaign_id"], next_version)
    assert status == 200, second
    assert second["already_calculated"] is True
    assert second["campaign_version"] == next_version
    assert _campaign(ctx["campaign_id"])["version"] == next_version
    status, third = _vcalculate(ctx["campaign_id"], version)
    assert status == 200, third
    assert third["already_calculated"] is True
    assert _audit_count("VALUATION_CALCULATED", ctx["campaign_id"]) == 1


def test_tampered_fingerprint_blocks_recalculate_and_preserves_values() -> None:
    ctx = _valued_campaign((("A", "5"),), [("A", 3)])
    product_id = ctx["products"]["A"]
    _as_manager()
    version = _campaign(ctx["campaign_id"])["version"]
    status, body = _vcalculate(ctx["campaign_id"], version)
    assert status == 200, body
    with SessionLocal() as db:
        row = db.execute(
            select(InventoryReconciliation).where(
                InventoryReconciliation.inventory_campaign_id
                == _campaign_row(ctx["campaign_id"]).id,
                InventoryReconciliation.product_id == product_id,
            )
        ).scalar_one()
        row.version += 1
        row.missing_cost_value = decimal.Decimal("77")
        db.commit()
    current = _campaign(ctx["campaign_id"])["version"]
    status, error = _vcalculate(ctx["campaign_id"], current)
    assert status == 409, error
    assert error["error"] == "VALUATION_SOURCE_CHANGED"
    preserved = _row_for(ctx["campaign_id"], product_id)
    assert _d(preserved.missing_cost_value) == 77
    assert _audit_count("VALUATION_CALCULATED", ctx["campaign_id"]) == 1


def test_preview_persists_nothing() -> None:
    ctx = _valued_campaign((("A", "5"),), [("A", 3)])
    _as_manager()
    status, preview = _vpreview(ctx["campaign_id"])
    assert status == 200, preview
    assert _d(preview["totals"]["total_missing_cost"]) == 4
    assert _monetary_non_null(ctx["campaign_id"]) == 0
    campaign = _campaign_row(ctx["campaign_id"])
    assert campaign.valuation_calculated_at is None
    assert campaign.valuation_source_sha256 is None
    assert campaign.status is CampaignStatus.APPROVED
    assert _audit_count("VALUATION_CALCULATED", ctx["campaign_id"]) == 0


def test_mixed_snapshot_currency_blocks_calculate() -> None:
    ctx = _valued_campaign(
        (("A", "5"), ("B", "3")),
        [("A", 5), ("B", 3)],
        master={"B": {"currency": "USD"}},
    )
    _as_manager()
    status, preview = _vpreview(ctx["campaign_id"])
    assert status == 200, preview
    assert preview["currency"] is None
    assert "MIXED_SNAPSHOT_CURRENCIES" in preview["warnings"]
    item_a = _item_for(preview, ctx["products"]["A"])
    item_b = _item_for(preview, ctx["products"]["B"])
    assert item_a["currency"] == "PEN"
    assert item_b["currency"] == "USD"
    version = _campaign(ctx["campaign_id"])["version"]
    status, error = _vcalculate(ctx["campaign_id"], version)
    assert status == 409, error
    assert error["error"] == "MIXED_SNAPSHOT_CURRENCIES"
    assert error["currencies"] == ["PEN", "USD"]
    status, read = _vread(ctx["campaign_id"])
    assert status == 409, read
    assert read["error"] == "VALUATION_NOT_CALCULATED"
    assert _monetary_non_null(ctx["campaign_id"]) == 0
    assert _audit_count("VALUATION_CALCULATED", ctx["campaign_id"]) == 0


def test_extra_outside_snapshot_valued_at_zero_with_warnings() -> None:
    campaign_id, batch_id, operator_id = _frozen_campaign((("A", "5"),))
    products = _product_map(batch_id)
    extra_id = create_standalone_product(f"TEST-XTRA-{uuid.uuid4().hex[:6]}")
    _as_operator(operator_id)
    session = _start_session(campaign_id)
    session_id = str(session["id"])
    status, body = _event(
        session_id, "MANUAL_ADD", product_id=products["A"], quantity=5
    )
    assert status == 200, body
    status, body = _event(session_id, "QR_SCAN", code="UNK-VAL-1")
    assert status == 200, body
    _submit_ok(session)
    _as_manager()
    unknown = _unknown_row(session_id, "UNK-VAL-1")
    status, body = _resolve_unknown(str(unknown.id), extra_id)
    assert status == 200, body
    _finalize_approved(campaign_id)
    _as_manager()
    status, preview = _vpreview(campaign_id)
    assert status == 200, preview
    extra = _item_for(preview, extra_id)
    assert _d(extra["expected_quantity"]) == 0
    assert extra["effective_unit_cost"] == "0.0000"
    assert extra["cost_source"] == "ZERO"
    assert extra["currency"] is None
    assert set(extra["warnings"]) == {
        "MISSING_SNAPSHOT_ITEM",
        "NO_SNAPSHOT_COST",
        "NO_SNAPSHOT_SALE_PRICE",
    }
    assert "ZERO_EFFECTIVE_COST" not in extra["warnings"]
    assert _d(extra["surplus_quantity"]) == 1
    assert extra["surplus_cost_value"] == "0.0000"
    assert extra["affected_sale_value"] == "0.0000"
    assert preview["currency"] == "PEN"
    version = _campaign(campaign_id)["version"]
    status, body = _vcalculate(campaign_id, version)
    assert status == 200, body
    status, read = _vread(campaign_id)
    assert status == 200, read
    read_extra = _item_for(read, extra_id)
    assert read_extra["warnings"] == extra["warnings"]
    assert _d(read["summary"]["total_surplus_cost"]) == 0


def test_zero_effective_cost_kept_zero_with_warning() -> None:
    ctx = _valued_campaign(
        (("A", "5"),),
        [("A", 3)],
        master={
            "A": {
                "cost": decimal.Decimal("0"),
                "consignment_cost": decimal.Decimal("0"),
            }
        },
    )
    _as_manager()
    status, preview = _vpreview(ctx["campaign_id"])
    assert status == 200, preview
    item = preview["items"][0]
    assert item["effective_unit_cost"] == "0.0000"
    assert item["cost_source"] == "ZERO"
    assert "ZERO_EFFECTIVE_COST" in item["warnings"]
    assert _d(item["missing_quantity"]) == 2
    assert item["missing_cost_value"] == "0.0000"
    assert item["affected_sale_value"] == "20.0000"
    assert _d(preview["totals"]["total_confirmed_loss_cost"]) == 0


def test_consignment_fallback_used_when_cost_zero() -> None:
    ctx = _valued_campaign(
        (("A", "5"),),
        [("A", 3)],
        master={
            "A": {
                "cost": decimal.Decimal("0"),
                "consignment_cost": decimal.Decimal("3.5"),
            }
        },
    )
    _as_manager()
    status, preview = _vpreview(ctx["campaign_id"])
    assert status == 200, preview
    item = preview["items"][0]
    assert item["cost_source"] == "CONSIGNMENT"
    assert item["effective_unit_cost"] == "3.5000"
    assert "ZERO_EFFECTIVE_COST" not in item["warnings"]
    assert item["missing_cost_value"] == "7.0000"
    assert _d(preview["totals"]["total_confirmed_loss_cost"]) == 7


def test_decimal_precision_is_exact_no_float() -> None:
    ctx = _valued_campaign(
        (("A", "5"),),
        [("A", 2)],
        master={"A": {"cost": decimal.Decimal("12.3456")}},
    )
    _as_manager()
    status, preview = _vpreview(ctx["campaign_id"])
    assert status == 200, preview
    item = preview["items"][0]
    assert item["effective_unit_cost"] == "12.3456"
    assert _d(item["missing_quantity"]) == 3
    assert item["missing_cost_value"] == "37.0368"
    assert _d(item["missing_cost_value"]) == decimal.Decimal("37.0368")
    assert preview["totals"]["total_missing_cost"] == "37.0368"
    version = _campaign(ctx["campaign_id"])["version"]
    status, body = _vcalculate(ctx["campaign_id"], version)
    assert status == 200, body
    status, read = _vread(ctx["campaign_id"])
    assert status == 200, read
    assert read["summary"]["total_missing_cost"] == "37.0368"


def test_read_requires_calculated_valuation() -> None:
    ctx = _valued_campaign((("A", "5"),), [("A", 3)])
    _as_manager()
    status, body = _vread(ctx["campaign_id"])
    assert status == 409, body
    assert body["error"] == "VALUATION_NOT_CALCULATED"


def test_valuation_requires_approved_campaign() -> None:
    campaign_id, _batch_id, _operator_id = _ready_to_count()
    _as_manager()
    status, body = _vpreview(campaign_id)
    assert status == 409, body
    assert body["error"] == "CAMPAIGN_NOT_APPROVED"
    version = _campaign(campaign_id)["version"]
    status, body = _vcalculate(campaign_id, version)
    assert status == 409, body
    assert body["error"] == "CAMPAIGN_NOT_APPROVED"
    status, body = _vread(campaign_id)
    assert status == 409, body
    assert body["error"] == "CAMPAIGN_NOT_APPROVED"
    assert _monetary_non_null(campaign_id) == 0


def test_operator_cannot_access_valuation_endpoints() -> None:
    ctx = _valued_campaign((("A", "5"),), [("A", 3)])
    _as_manager()
    version = _campaign(ctx["campaign_id"])["version"]
    override_auth(
        {"inventory.count", "inventory.read"}, user_id=ctx["operator_id"]
    )
    status, _body = _vpreview(ctx["campaign_id"])
    assert status == 403
    status, _body = _vcalculate(ctx["campaign_id"], version)
    assert status == 403
    status, _body = _vread(ctx["campaign_id"])
    assert status == 403
    assert _monetary_non_null(ctx["campaign_id"]) == 0


def test_approve_and_reconcile_permissions_are_split() -> None:
    ctx = _valued_campaign((("A", "5"),), [("A", 3)])
    _as_manager()
    version = _campaign(ctx["campaign_id"])["version"]
    as_user({"inventory.reconcile"})
    status, body = _vread(ctx["campaign_id"])
    assert status == 409, body
    assert body["error"] == "VALUATION_NOT_CALCULATED"
    status, _body = _vpreview(ctx["campaign_id"])
    assert status == 403
    status, _body = _vcalculate(ctx["campaign_id"], version)
    assert status == 403
    as_user({"inventory.approve"})
    status, _body = _vpreview(ctx["campaign_id"])
    assert status == 200
    status, body = _vcalculate(ctx["campaign_id"], version)
    assert status == 200, body
    assert body["already_calculated"] is False
    status, _body = _vread(ctx["campaign_id"])
    assert status == 403


def test_valuation_audit_metadata_has_no_amounts() -> None:
    ctx = _valued_campaign((("A", "5"),), [("A", 3)])
    _as_manager()
    version = _campaign(ctx["campaign_id"])["version"]
    status, body = _vcalculate(ctx["campaign_id"], version)
    assert status == 200, body
    status, second = _vcalculate(ctx["campaign_id"], version + 1)
    assert status == 200 and second["already_calculated"] is True
    assert _audit_count("VALUATION_CALCULATED", ctx["campaign_id"]) == 1
    with SessionLocal() as db:
        event = db.execute(
            select(AuditEvent).where(
                AuditEvent.action == "VALUATION_CALCULATED",
                AuditEvent.entity_id == _campaign_row(ctx["campaign_id"]).id,
            )
        ).scalar_one()
    meta = event.metadata_
    assert meta is not None
    assert set(meta) == {
        "campaign_id",
        "valuation_hash",
        "currency",
        "previous_version",
        "version",
    }
    assert meta["currency"] == "PEN"
    assert len(str(meta["valuation_hash"])) == 64
    for key in meta:
        lowered = key.lower()
        assert "cost" not in lowered
        assert "amount" not in lowered
        assert "price" not in lowered


def test_valuation_preserves_approved_status_everywhere() -> None:
    ctx = _valued_campaign((("A", "5"),), [("A", 3)])
    _as_manager()
    version = _campaign(ctx["campaign_id"])["version"]
    status, body = _vcalculate(ctx["campaign_id"], version)
    assert status == 200, body
    assert _campaign(ctx["campaign_id"])["status"] == "APPROVED"
    campaign = _campaign_row(ctx["campaign_id"])
    assert campaign.status is CampaignStatus.APPROVED
    assert campaign.valuation_calculated_at is not None
    assert campaign.valuation_source_sha256 == body["valuation_source_sha256"]
    rows = _recon_rows(ctx["campaign_id"])
    assert rows
    for row in rows:
        assert row.status is ReconciliationStatus.APPROVED
        assert row.effective_unit_cost is not None
        assert row.missing_cost_value is not None
        assert row.damage_cost_value is not None
        assert row.surplus_cost_value is not None
        assert row.affected_sale_value is not None


def test_operational_endpoints_stay_blind_after_valuation() -> None:
    ctx = _valued_campaign((("A", "5"),), [("A", 3)])
    campaign_id = ctx["campaign_id"]
    _as_manager()
    version = _campaign(campaign_id)["version"]
    status, body = _vcalculate(campaign_id, version)
    assert status == 200, body
    session_id = ctx["session_id"]
    urls = [
        f"/api/v1/inventory/count-sessions/{session_id}",
        f"/api/v1/inventory/count-sessions/{session_id}/items",
        f"/api/v1/inventory/count-sessions/{session_id}/events",
        "/api/v1/inventory/my-assignments",
        f"/api/v1/inventory/campaigns/{campaign_id}",
        f"/api/v1/inventory/campaigns/{campaign_id}/count-sessions",
        f"/api/v1/inventory/campaigns/{campaign_id}/reconciliation",
        "/api/v1/inventory/my-recounts",
        f"/api/v1/inventory/campaigns/{campaign_id}/recounts",
    ]
    for url in urls:
        response = client.get(url)
        assert response.status_code == 200, (url, response.text)
        text = response.text.lower()
        for token in _BLIND_FORBIDDEN:
            assert token.lower() not in text, (url, token)


def test_regressions_f008_endpoints_still_work_after_valuation() -> None:
    ctx = _valued_campaign((("A", "5"),), [("A", 3)])
    _as_manager()
    version = _campaign(ctx["campaign_id"])["version"]
    status, body = _vcalculate(ctx["campaign_id"], version)
    assert status == 200, body
    response = client.get(
        f"/api/v1/inventory/campaigns/{ctx['campaign_id']}/reconciliation"
    )
    assert response.status_code == 200, response.text
    recon = response.json()
    assert recon["total"] == 1
    row = recon["items"][0]
    assert _d(row["missing_quantity"]) == 2
    assert row["status"] == "APPROVED"
    for key in row:
        assert "cost" not in key
        assert "sale_price" not in key
    response = client.get(
        f"/api/v1/inventory/campaigns/{ctx['campaign_id']}/reconciliation/preview"
    )
    assert response.status_code == 200, response.text
