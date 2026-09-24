"""Calculo puro de valorizacion economica (F009).

Toma la conciliacion APPROVED y el snapshot congelado; nunca escribe ni
recalcula conteos. Toda la aritmetica es Decimal (nunca float) y se cuantiza
a la precision de almacenamiento NUMERIC(18,4) para que preview, persistencia
y lectura coincidan exactamente.
"""

from __future__ import annotations

import decimal
import hashlib
import uuid
from typing import TypedDict

from app.importers.cost import resolve_effective_cost
from app.models import InventoryReconciliation, InventorySnapshotItem
from app.models.enums import CostSource

ZERO = decimal.Decimal("0")
_AMOUNT_QUANTUM = decimal.Decimal("0.0001")


class ItemCalc(TypedDict):
    product_id: uuid.UUID
    internal_reference: str | None
    name: str | None
    expected_quantity: decimal.Decimal
    approved_physical_quantity: decimal.Decimal | None
    missing_quantity: decimal.Decimal
    surplus_quantity: decimal.Decimal
    damaged_quantity: decimal.Decimal
    effective_unit_cost: decimal.Decimal
    cost_source: str
    currency: str | None
    missing_cost_value: decimal.Decimal
    damage_cost_value: decimal.Decimal
    surplus_cost_value: decimal.Decimal
    affected_sale_value: decimal.Decimal
    warnings: list[str]


class Totals(TypedDict):
    total_missing_units: decimal.Decimal
    total_damaged_units: decimal.Decimal
    total_surplus_units: decimal.Decimal
    total_missing_cost: decimal.Decimal
    total_damage_cost: decimal.Decimal
    total_surplus_cost: decimal.Decimal
    total_affected_sale_value: decimal.Decimal
    total_confirmed_loss_cost: decimal.Decimal


def amount(value: decimal.Decimal) -> decimal.Decimal:
    """Cuantiza a la precision de almacenamiento (NUMERIC(18,4))."""
    return value.quantize(_AMOUNT_QUANTUM)


def _or_zero(value: decimal.Decimal | None) -> decimal.Decimal:
    return value if value is not None else ZERO


def snapshot_meta(
    snapshot: InventorySnapshotItem | None,
) -> tuple[decimal.Decimal, str, str | None, list[str]]:
    """(costo_unitario, cost_source, moneda, warnings) del snapshot congelado."""
    if snapshot is None:
        return (
            ZERO,
            CostSource.ZERO.value,
            None,
            ["MISSING_SNAPSHOT_ITEM", "NO_SNAPSHOT_COST", "NO_SNAPSHOT_SALE_PRICE"],
        )
    warnings: list[str] = []
    if snapshot.effective_cost_snapshot is None:
        effective, source = resolve_effective_cost(
            snapshot.cost_snapshot, snapshot.consignment_cost_snapshot
        )
        cost_source = source.value
    else:
        effective = snapshot.effective_cost_snapshot
        cost_source = snapshot.cost_source.value
    if effective == ZERO:
        warnings.append("ZERO_EFFECTIVE_COST")
    if snapshot.sale_price_snapshot is None:
        warnings.append("NO_SNAPSHOT_SALE_PRICE")
    return effective, cost_source, snapshot.currency_snapshot, warnings


def _amounts(
    row: InventoryReconciliation,
    unit_cost: decimal.Decimal,
    sale_price: decimal.Decimal | None,
) -> tuple[decimal.Decimal, decimal.Decimal, decimal.Decimal, decimal.Decimal]:
    missing_cost = amount(row.missing_quantity * unit_cost)
    damage_cost = amount(row.damaged_quantity * unit_cost)
    surplus_cost = amount(row.surplus_quantity * unit_cost)
    affected_sale = amount(
        (row.missing_quantity + row.damaged_quantity) * _or_zero(sale_price)
    )
    return missing_cost, damage_cost, surplus_cost, affected_sale


def compute_item(
    row: InventoryReconciliation,
    snapshot: InventorySnapshotItem | None,
    *,
    product_ref: str | None,
    product_name: str | None,
) -> ItemCalc:
    """Valores de un producto a partir del snapshot congelado (preview/calculate)."""
    unit_cost, cost_source, currency, warnings = snapshot_meta(snapshot)
    sale_price = snapshot.sale_price_snapshot if snapshot is not None else None
    missing_cost, damage_cost, surplus_cost, affected_sale = _amounts(
        row, unit_cost, sale_price
    )
    return ItemCalc(
        product_id=row.product_id,
        internal_reference=product_ref,
        name=product_name,
        expected_quantity=row.expected_quantity,
        approved_physical_quantity=row.approved_physical_quantity,
        missing_quantity=row.missing_quantity,
        surplus_quantity=row.surplus_quantity,
        damaged_quantity=row.damaged_quantity,
        effective_unit_cost=amount(unit_cost),
        cost_source=cost_source,
        currency=currency,
        missing_cost_value=missing_cost,
        damage_cost_value=damage_cost,
        surplus_cost_value=surplus_cost,
        affected_sale_value=affected_sale,
        warnings=warnings,
    )


def persisted_item(
    row: InventoryReconciliation,
    snapshot: InventorySnapshotItem | None,
    *,
    product_ref: str | None,
    product_name: str | None,
) -> ItemCalc:
    """Valores desde los campos PERSISTIDOS (GET valuation; F010 lee persistido).

    Los montos y el costo unitario salen de inventory_reconciliations;
    cost_source, moneda y warnings se derivan del snapshot congelado.
    """
    _unit, cost_source, currency, warnings = snapshot_meta(snapshot)
    return ItemCalc(
        product_id=row.product_id,
        internal_reference=product_ref,
        name=product_name,
        expected_quantity=row.expected_quantity,
        approved_physical_quantity=row.approved_physical_quantity,
        missing_quantity=row.missing_quantity,
        surplus_quantity=row.surplus_quantity,
        damaged_quantity=row.damaged_quantity,
        effective_unit_cost=_or_zero(row.effective_unit_cost),
        cost_source=cost_source,
        currency=currency,
        missing_cost_value=_or_zero(row.missing_cost_value),
        damage_cost_value=_or_zero(row.damage_cost_value),
        surplus_cost_value=_or_zero(row.surplus_cost_value),
        affected_sale_value=_or_zero(row.affected_sale_value),
        warnings=warnings,
    )


def aggregate_totals(items: list[ItemCalc]) -> Totals:
    """Totales de campana. confirmed_loss = missing + damage (NUNCA netea sobrante)."""
    missing_units = amount(sum((item["missing_quantity"] for item in items), ZERO))
    damaged_units = amount(sum((item["damaged_quantity"] for item in items), ZERO))
    surplus_units = amount(sum((item["surplus_quantity"] for item in items), ZERO))
    missing_cost = amount(sum((item["missing_cost_value"] for item in items), ZERO))
    damage_cost = amount(sum((item["damage_cost_value"] for item in items), ZERO))
    surplus_cost = amount(sum((item["surplus_cost_value"] for item in items), ZERO))
    affected_sale = amount(sum((item["affected_sale_value"] for item in items), ZERO))
    return Totals(
        total_missing_units=missing_units,
        total_damaged_units=damaged_units,
        total_surplus_units=surplus_units,
        total_missing_cost=missing_cost,
        total_damage_cost=damage_cost,
        total_surplus_cost=surplus_cost,
        total_affected_sale_value=affected_sale,
        total_confirmed_loss_cost=amount(missing_cost + damage_cost),
    )


def campaign_currency(
    currencies: list[str | None],
) -> tuple[str | None, list[str]]:
    """Moneda unica de la campana. >1 moneda no nula -> warning, sin conversion."""
    distinct = sorted({currency for currency in currencies if currency})
    if len(distinct) > 1:
        return None, ["MIXED_SNAPSHOT_CURRENCIES"]
    return (distinct[0] if distinct else None), []


def _num(value: decimal.Decimal | None) -> str:
    return "" if value is None else str(value)


def valuation_fingerprint(pairs: list[tuple[str, InventoryReconciliation]]) -> str:
    """SHA-256 estable de la conciliacion APPROVED (orden por referencia+producto)."""
    hasher = hashlib.sha256()
    for _ref, row in sorted(pairs, key=lambda pair: (pair[0], str(pair[1].product_id))):
        hasher.update(
            f"{row.id}|{row.product_id}|{_num(row.approved_physical_quantity)}"
            f"|{_num(row.expected_quantity)}|{_num(row.difference_quantity)}"
            f"|{_num(row.missing_quantity)}|{_num(row.surplus_quantity)}"
            f"|{_num(row.damaged_quantity)}|{row.selected_session_id}|{row.version}\n".encode()
        )
    return hasher.hexdigest()
