"""Valorizacion F009: preview, calculate y lectura persistida.

Solo LEE snapshot congelado + conciliacion APPROVED. ``calculate`` escribe los
campos monetarios de inventory_reconciliations y los metadatos de valorizacion
de la campana (transaccional, idempotente, con huella anti-tampering). Nunca
recalcula conteos, ni seleccion, ni snapshot, ni cierra la campana (F010).
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    InventoryCampaign,
    InventoryReconciliation,
    InventorySnapshotItem,
    Product,
)
from app.models.enums import CampaignStatus, ReconciliationStatus
from app.services.auth import audit_service
from app.services.inventory import campaign_service, recount_service
from app.services.reconciliation import comparison_service
from app.services.valuation import valuation_calculator
from app.services.valuation.errors import ValuationError
from app.services.valuation.valuation_calculator import ItemCalc, Totals

_F008_SOURCE_ERROR = (
    "La fuente de conciliacion cambio: la conciliacion aprobada no es valida",
    "VALUATION_SOURCE_CHANGED",
)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _q(value: decimal.Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _campaign_block(campaign: InventoryCampaign) -> dict[str, object]:
    return {
        "id": str(campaign.id),
        "code": campaign.code,
        "name": campaign.name,
        "status": campaign.status.value,
        "version": campaign.version,
        "approved_at": _iso(campaign.approved_at),
        "snapshot_frozen_at": _iso(campaign.snapshot_frozen_at),
        "reconciliation_source_sha256": campaign.reconciliation_source_sha256,
        "valuation_calculated_at": _iso(campaign.valuation_calculated_at),
        "valuation_source_sha256": campaign.valuation_source_sha256,
    }


def _item_payload(calc: ItemCalc) -> dict[str, object]:
    return {
        "product": {
            "id": str(calc["product_id"]),
            "internal_reference": calc["internal_reference"],
            "name": calc["name"],
        },
        "expected_quantity": _q(calc["expected_quantity"]),
        "approved_physical_quantity": _q(calc["approved_physical_quantity"]),
        "missing_quantity": _q(calc["missing_quantity"]),
        "surplus_quantity": _q(calc["surplus_quantity"]),
        "damaged_quantity": _q(calc["damaged_quantity"]),
        "effective_unit_cost": _q(calc["effective_unit_cost"]),
        "cost_source": calc["cost_source"],
        "currency": calc["currency"],
        "missing_cost_value": _q(calc["missing_cost_value"]),
        "damage_cost_value": _q(calc["damage_cost_value"]),
        "surplus_cost_value": _q(calc["surplus_cost_value"]),
        "affected_sale_value": _q(calc["affected_sale_value"]),
        "warnings": list(calc["warnings"]),
    }


def _totals_payload(totals: Totals) -> dict[str, str | None]:
    return {
        "total_missing_units": _q(totals["total_missing_units"]),
        "total_damaged_units": _q(totals["total_damaged_units"]),
        "total_surplus_units": _q(totals["total_surplus_units"]),
        "total_missing_cost": _q(totals["total_missing_cost"]),
        "total_damage_cost": _q(totals["total_damage_cost"]),
        "total_surplus_cost": _q(totals["total_surplus_cost"]),
        "total_affected_sale_value": _q(totals["total_affected_sale_value"]),
        "total_confirmed_loss_cost": _q(totals["total_confirmed_loss_cost"]),
    }


def _load_rows(
    db: Session, campaign_id: uuid.UUID, *, for_update: bool
) -> list[InventoryReconciliation]:
    query = (
        select(InventoryReconciliation)
        .where(InventoryReconciliation.inventory_campaign_id == campaign_id)
        .order_by(InventoryReconciliation.product_id)
    )
    if for_update:
        query = query.with_for_update()
    return list(db.execute(query).scalars())


def _ref_of(
    row: InventoryReconciliation,
    snapshots: dict[uuid.UUID, InventorySnapshotItem],
    products: dict[uuid.UUID, Product],
) -> str:
    snapshot = snapshots.get(row.product_id)
    if snapshot is not None:
        return snapshot.internal_reference_snapshot
    product = products.get(row.product_id)
    return product.internal_reference if product is not None else ""


def _context(
    db: Session, campaign_id: uuid.UUID, rows: list[InventoryReconciliation]
) -> tuple[
    list[InventoryReconciliation],
    dict[uuid.UUID, InventorySnapshotItem],
    dict[uuid.UUID, Product],
]:
    snapshots = {
        snapshot.product_id: snapshot
        for snapshot in db.execute(
            select(InventorySnapshotItem).where(
                InventorySnapshotItem.inventory_campaign_id == campaign_id
            )
        ).scalars()
    }
    product_ids = [row.product_id for row in rows]
    products: dict[uuid.UUID, Product] = {}
    if product_ids:
        products = {
            product.id: product
            for product in db.execute(
                select(Product).where(Product.id.in_(product_ids))
            ).scalars()
        }
    ordered = sorted(
        rows, key=lambda row: (_ref_of(row, snapshots, products), str(row.product_id))
    )
    return ordered, snapshots, products


def _ref_and_name(
    row: InventoryReconciliation,
    snapshots: dict[uuid.UUID, InventorySnapshotItem],
    products: dict[uuid.UUID, Product],
) -> tuple[str, str | None]:
    snapshot = snapshots.get(row.product_id)
    if snapshot is not None:
        return snapshot.internal_reference_snapshot, snapshot.description_snapshot
    product = products.get(row.product_id)
    if product is None:
        return "", None
    return product.internal_reference, product.name


def _compute(
    ordered: list[InventoryReconciliation],
    snapshots: dict[uuid.UUID, InventorySnapshotItem],
    products: dict[uuid.UUID, Product],
    *,
    persisted: bool,
) -> list[ItemCalc]:
    calcs: list[ItemCalc] = []
    for row in ordered:
        ref, name = _ref_and_name(row, snapshots, products)
        snapshot = snapshots.get(row.product_id)
        if persisted:
            calc = valuation_calculator.persisted_item(
                row, snapshot, product_ref=ref, product_name=name
            )
        else:
            calc = valuation_calculator.compute_item(
                row, snapshot, product_ref=ref, product_name=name
            )
        calcs.append(calc)
    return calcs


def _require_campaign_approved(campaign: InventoryCampaign) -> None:
    if campaign.status is CampaignStatus.CLOSED:
        raise ValuationError("La campana esta cerrada", 409, "CAMPAIGN_CLOSED")
    if campaign.status is not CampaignStatus.APPROVED:
        raise ValuationError(
            "La campana no esta aprobada", 409, "CAMPAIGN_NOT_APPROVED"
        )


def _require_rows_approved(rows: list[InventoryReconciliation]) -> None:
    if not rows:
        raise ValuationError(
            "La conciliacion no esta preparada", 409, "RECONCILIATION_NOT_PREPARED"
        )
    for row in rows:
        if row.status is not ReconciliationStatus.APPROVED:
            raise ValuationError(
                "La conciliacion no esta aprobada por completo",
                409,
                "RECONCILIATION_NOT_APPROVED",
            )


def _require_source_intact(db: Session, campaign: InventoryCampaign) -> None:
    stored = campaign.reconciliation_source_sha256
    if stored is None or comparison_service.source_fingerprint(db, campaign.id) != stored:
        raise ValuationError(_F008_SOURCE_ERROR[0], 409, _F008_SOURCE_ERROR[1])


def _require_frozen(campaign: InventoryCampaign) -> None:
    if campaign.snapshot_frozen_at is None:
        raise ValuationError("El snapshot no esta congelado", 409, "SNAPSHOT_NOT_FROZEN")


def _require_no_open_recount(db: Session, campaign_id: uuid.UUID) -> None:
    if recount_service.open_recount_for_campaign(db, campaign_id) is not None:
        raise ValuationError(
            "Hay un reconteo abierto: no se puede valorizar", 409, "OPEN_RECOUNT_EXISTS"
        )


def _require_resolved_unknowns(db: Session, campaign_id: uuid.UUID) -> None:
    unresolved = comparison_service.unresolved_unknown_count(db, campaign_id)
    if unresolved > 0:
        raise ValuationError(
            "Hay codigos desconocidos sin resolver",
            409,
            "UNRESOLVED_UNKNOWN_CODES",
            {"unresolved_unknown_count": unresolved},
        )


def _require_non_negative(rows: list[InventoryReconciliation]) -> None:
    for row in rows:
        if row.expected_quantity < 0:
            raise ValuationError(
                "Hay cantidades esperadas negativas: corrija la fuente",
                409,
                "NEGATIVE_EXPECTED_QUANTITY_REQUIRES_SOURCE_CORRECTION",
                {"product_id": str(row.product_id)},
            )


def _all_valued(rows: list[InventoryReconciliation]) -> bool:
    return all(
        row.effective_unit_cost is not None
        and row.missing_cost_value is not None
        and row.damage_cost_value is not None
        and row.surplus_cost_value is not None
        and row.affected_sale_value is not None
        for row in rows
    )


def _currency_or_mixed(calcs: list[ItemCalc]) -> tuple[str | None, list[str]]:
    return valuation_calculator.campaign_currency([calc["currency"] for calc in calcs])


def preview(db: Session, campaign_id: uuid.UUID) -> dict[str, object]:
    """Vista previa de valorizacion SIN persistir (§17)."""
    campaign = campaign_service.get_campaign(db, campaign_id)
    _require_campaign_approved(campaign)
    rows = _load_rows(db, campaign_id, for_update=False)
    _require_rows_approved(rows)
    ordered, snapshots, products = _context(db, campaign_id, rows)
    calcs = _compute(ordered, snapshots, products, persisted=False)
    currency, warnings = _currency_or_mixed(calcs)
    totals = valuation_calculator.aggregate_totals(calcs)
    return {
        "campaign": _campaign_block(campaign),
        "currency": currency,
        "calculated_at": _iso(campaign.valuation_calculated_at),
        "warnings": warnings,
        "items": [_item_payload(calc) for calc in calcs],
        "totals": _totals_payload(totals),
    }


def calculate(
    db: Session,
    *,
    actor_id: uuid.UUID,
    campaign_id: uuid.UUID,
    expected_version: int,
) -> dict[str, object]:
    """Calcula y persiste la valorizacion de la conciliacion APPROVED (§22-§26)."""
    campaign = campaign_service.lock_campaign(db, campaign_id)
    _require_campaign_approved(campaign)
    rows = _load_rows(db, campaign_id, for_update=True)
    _require_rows_approved(rows)
    _require_source_intact(db, campaign)
    ordered, snapshots, products = _context(db, campaign_id, rows)
    pairs = [(_ref_of(row, snapshots, products), row) for row in ordered]
    fingerprint = valuation_calculator.valuation_fingerprint(pairs)

    # Idempotencia (estilo F008): huella intacta + montos persistidos -> no-op,
    # sin version++ ni audit, y tolerante al mismo cuerpo reenviado.
    if (
        campaign.valuation_source_sha256 is not None
        and campaign.valuation_source_sha256 == fingerprint
        and _all_valued(rows)
    ):
        currency, _warnings = _currency_or_mixed(
            _compute(ordered, snapshots, products, persisted=True)
        )
        db.commit()
        return {
            "already_calculated": True,
            "campaign_id": str(campaign.id),
            "status": campaign.status.value,
            "campaign_version": campaign.version,
            "valuation_source_sha256": campaign.valuation_source_sha256,
            "calculated_at": _iso(campaign.valuation_calculated_at),
            "currency": currency,
            "items_count": len(rows),
        }

    campaign_service.check_version(campaign, expected_version)
    _require_frozen(campaign)
    _require_no_open_recount(db, campaign_id)
    _require_resolved_unknowns(db, campaign_id)
    _require_non_negative(rows)

    calcs = _compute(ordered, snapshots, products, persisted=False)
    currency, warnings = _currency_or_mixed(calcs)
    if "MIXED_SNAPSHOT_CURRENCIES" in warnings:
        currencies = sorted({calc["currency"] for calc in calcs if calc["currency"]})
        raise ValuationError(
            "El snapshot congelado tiene multiples monedas: no se puede valorizar",
            409,
            "MIXED_SNAPSHOT_CURRENCIES",
            {"currencies": currencies},
        )
    if (
        campaign.valuation_source_sha256 is not None
        and campaign.valuation_source_sha256 != fingerprint
    ):
        raise ValuationError(
            "La fuente de valorizacion cambio: no se sobrescribe",
            409,
            "VALUATION_SOURCE_CHANGED",
        )
    # Huella estable pero montos incompletos: se recalcula y persiste.

    now = _now()
    for row, calc in zip(ordered, calcs, strict=True):
        row.effective_unit_cost = calc["effective_unit_cost"]
        row.missing_cost_value = calc["missing_cost_value"]
        row.damage_cost_value = calc["damage_cost_value"]
        row.surplus_cost_value = calc["surplus_cost_value"]
        row.affected_sale_value = calc["affected_sale_value"]
    previous_version = campaign.version
    campaign.valuation_calculated_at = now
    campaign.valuation_source_sha256 = fingerprint
    campaign.version += 1
    audit_service.record(
        db,
        actor_user_id=actor_id,
        action=audit_service.VALUATION_CALCULATED,
        entity_type="inventory_campaign",
        entity_id=campaign.id,
        metadata={
            "campaign_id": str(campaign.id),
            "valuation_hash": fingerprint,
            "currency": currency,
            "previous_version": previous_version,
            "version": campaign.version,
        },
    )
    db.commit()
    return {
        "already_calculated": False,
        "campaign_id": str(campaign.id),
        "status": campaign.status.value,
        "campaign_version": campaign.version,
        "valuation_source_sha256": fingerprint,
        "calculated_at": _iso(campaign.valuation_calculated_at),
        "currency": currency,
        "items_count": len(rows),
    }


def read_valuation(db: Session, campaign_id: uuid.UUID) -> dict[str, object]:
    """LEE la valorizacion persistida (F010 debe leer persistido, no recalcular)."""
    campaign = campaign_service.get_campaign(db, campaign_id)
    _require_campaign_approved(campaign)
    if campaign.valuation_calculated_at is None or (
        campaign.valuation_source_sha256 is None
    ):
        raise ValuationError(
            "La valorizacion no esta calculada", 409, "VALUATION_NOT_CALCULATED"
        )
    rows = _load_rows(db, campaign_id, for_update=False)
    _require_rows_approved(rows)
    if not _all_valued(rows):
        raise ValuationError(
            "La valorizacion no esta calculada", 409, "VALUATION_NOT_CALCULATED"
        )
    ordered, snapshots, products = _context(db, campaign_id, rows)
    calcs = _compute(ordered, snapshots, products, persisted=True)
    currency, warnings = _currency_or_mixed(calcs)
    totals = valuation_calculator.aggregate_totals(calcs)
    return {
        "campaign": _campaign_block(campaign),
        "currency": currency,
        "calculated_at": _iso(campaign.valuation_calculated_at),
        "warnings": warnings,
        "summary": _totals_payload(totals),
        "items": [_item_payload(calc) for calc in calcs],
    }
