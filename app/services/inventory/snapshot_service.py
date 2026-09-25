"""Servicio de snapshot: fuentes elegibles, scope, preview y congelado."""

from __future__ import annotations

import datetime as dt
import decimal
import hashlib
import json
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.importers.cost import resolve_effective_cost
from app.models import (
    ImportBatch,
    InventoryCampaign,
    InventorySnapshotItem,
    Product,
    ProductSupplierRef,
    StockSnapshot,
)
from app.models.enums import ImportBatchStatus, ImportBatchType, StockScope

ELIGIBLE_TYPES = (ImportBatchType.PRODUCTS, ImportBatchType.MIXED_PRODUCT_EXPORT)
ELIGIBLE_STATUSES = (ImportBatchStatus.COMPLETED, ImportBatchStatus.COMPLETED_WITH_WARNINGS)


class SnapshotError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def get_source_batch(db: Session, batch_id: uuid.UUID) -> ImportBatch:
    batch = db.get(ImportBatch, batch_id)
    if batch is None:
        raise SnapshotError("Lote de importacion no encontrado", 404)
    if batch.status not in ELIGIBLE_STATUSES:
        raise SnapshotError("El lote de origen no esta completado", 409)
    if batch.import_type not in ELIGIBLE_TYPES:
        raise SnapshotError("El lote no es una fuente valida de stock", 409)
    count = db.execute(
        select(func.count())
        .select_from(StockSnapshot)
        .where(StockSnapshot.import_batch_id == batch_id)
    ).scalar_one()
    if count == 0:
        raise SnapshotError("El lote de origen no contiene stock_snapshots", 409)
    return batch


def detect_scope(db: Session, batch_id: uuid.UUID) -> StockScope:
    with_location = db.execute(
        select(func.count())
        .select_from(StockSnapshot)
        .where(StockSnapshot.import_batch_id == batch_id, StockSnapshot.location_id.is_not(None))
    ).scalar_one()
    return StockScope.LOCATION if with_location > 0 else StockScope.AGGREGATE


def eligible_sources(
    db: Session, *, limit: int = 100, offset: int = 0
) -> list[dict[str, object]]:
    rows = db.execute(
        select(ImportBatch, func.count(StockSnapshot.id))
        .join(StockSnapshot, StockSnapshot.import_batch_id == ImportBatch.id)
        .where(
            ImportBatch.status.in_(ELIGIBLE_STATUSES),
            ImportBatch.import_type.in_(ELIGIBLE_TYPES),
        )
        .group_by(ImportBatch.id)
        .order_by(ImportBatch.completed_at.desc(), ImportBatch.id)
        .offset(offset)
        .limit(limit)
    ).all()
    return [
        {
            "id": str(batch.id),
            "import_type": batch.import_type.value,
            "source_filename": batch.source_filename,
            "completed_at": batch.completed_at.isoformat() if batch.completed_at else None,
            "stock_snapshot_count": count,
            "stock_scope": detect_scope(db, batch.id).value,
        }
        for batch, count in rows
    ]


def _source_rows(db: Session, batch_id: uuid.UUID) -> list[tuple[StockSnapshot, Product]]:
    rows = db.execute(
        select(StockSnapshot, Product)
        .join(Product, Product.id == StockSnapshot.product_id)
        .where(StockSnapshot.import_batch_id == batch_id)
        .order_by(Product.internal_reference, Product.id)
    ).all()
    return [(snapshot, product) for snapshot, product in rows]


def _duplicate_keys(rows: list[tuple[StockSnapshot, Product]]) -> list[str]:
    seen: dict[tuple[uuid.UUID, uuid.UUID | None], int] = {}
    for snapshot, product in rows:
        key = (product.id, snapshot.location_id)
        seen[key] = seen.get(key, 0) + 1
    return [f"{product_id}:{location_id}" for (product_id, location_id), n in seen.items() if n > 1]


def _supplier_map(db: Session, product_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[str]]:
    if not product_ids:
        return {}
    result: dict[uuid.UUID, list[str]] = {}
    rows = db.execute(
        select(ProductSupplierRef.product_id, ProductSupplierRef.supplier_reference).where(
            ProductSupplierRef.product_id.in_(product_ids)
        )
    ).all()
    for product_id, reference in rows:
        if reference:
            result.setdefault(product_id, []).append(reference)
    return result


def _cost_breakdown(rows: list[tuple[StockSnapshot, Product]]) -> tuple[int, int, int]:
    using_cost = using_consignment = zero = 0
    for _snapshot, product in rows:
        _effective, source = resolve_effective_cost(product.cost, product.consignment_cost)
        if source.value == "COST":
            using_cost += 1
        elif source.value == "CONSIGNMENT":
            using_consignment += 1
        else:
            zero += 1
    return using_cost, using_consignment, zero


def preview(db: Session, campaign: InventoryCampaign) -> dict[str, object]:
    if campaign.source_import_batch_id is None:
        raise SnapshotError("La campana no tiene lote de origen seleccionado", 409)
    batch = get_source_batch(db, campaign.source_import_batch_id)
    scope = detect_scope(db, batch.id)
    rows = _source_rows(db, batch.id)

    candidates = [row for row in rows if row[0].quantity != 0]
    zero_rows = [row for row in rows if row[0].quantity == 0]
    negative_rows = [row for row in rows if row[0].quantity < 0]
    duplicates = _duplicate_keys(rows)
    supplier_map = _supplier_map(db, [product.id for _s, product in candidates])
    multiple_suppliers = [str(pid) for pid, refs in supplier_map.items() if len(refs) > 1]
    using_cost, using_consignment, zero_cost = _cost_breakdown(candidates)

    warnings: list[str] = []
    if negative_rows:
        warnings.append("NEGATIVE_EXPECTED_QUANTITY")
    if duplicates:
        warnings.append("INCONSISTENT_SOURCE_SNAPSHOT")
    if multiple_suppliers:
        warnings.append("MULTIPLE_SUPPLIER_REFERENCES")
    if campaign.location_id is not None and scope is StockScope.AGGREGATE:
        warnings.append("AGGREGATE_SOURCE_WITH_PHYSICAL_LOCATION")

    total_expected = sum((snapshot.quantity for snapshot, _p in candidates), decimal.Decimal("0"))
    return {
        "source_import_batch_id": str(batch.id),
        "source_filename": batch.source_filename,
        "source_stock_scope": scope.value,
        "source_rows": len(rows),
        "candidate_products": len(candidates),
        "zero_quantity_products": len(zero_rows),
        "negative_quantity_products": len(negative_rows),
        "products_using_cost": using_cost,
        "products_using_consignment_cost": using_consignment,
        "products_with_zero_effective_cost": zero_cost,
        "total_expected_quantity": str(total_expected),
        "warnings": warnings,
    }


def _snapshot_hash(items: list[dict[str, object]]) -> str:
    canonical = [
        {
            "product": str(item["product_id"]),
            "reference": item["internal_reference_snapshot"],
            "expected_quantity": str(item["expected_quantity"]),
            "sale_price": str(item["sale_price_snapshot"]),
            "cost": str(item["cost_snapshot"]),
            "consignment_cost": str(item["consignment_cost_snapshot"]),
            "effective_cost": str(item["effective_cost_snapshot"]),
            "cost_source": item["cost_source"],
            "currency": item["currency_snapshot"],
            "location": str(item["location_id"]),
        }
        for item in sorted(
            items,
            key=lambda i: (str(i["internal_reference_snapshot"]), str(i["product_id"])),
        )
    ]
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def assert_source_consistent(db: Session, campaign: InventoryCampaign) -> None:
    if campaign.source_import_batch_id is None:
        raise SnapshotError("La campana no tiene lote de origen seleccionado", 409)
    rows = _source_rows(db, campaign.source_import_batch_id)
    if _duplicate_keys(rows):
        raise SnapshotError("Fuente inconsistente: hay filas duplicadas producto/ubicacion", 409)


def build_snapshot(db: Session, campaign: InventoryCampaign) -> tuple[int, str]:
    """Crea inventory_snapshot_items y devuelve (cantidad, sha256)."""
    if campaign.source_import_batch_id is None:
        raise SnapshotError("La campana no tiene lote de origen seleccionado", 409)
    rows = _source_rows(db, campaign.source_import_batch_id)
    candidates = [(snapshot, product) for snapshot, product in rows if snapshot.quantity != 0]
    supplier_map = _supplier_map(db, [product.id for _s, product in candidates])

    hashed: list[dict[str, object]] = []
    for snapshot, product in candidates:
        effective, source = resolve_effective_cost(product.cost, product.consignment_cost)
        refs = sorted(supplier_map.get(product.id, []))
        supplier_ref = refs[0] if refs else None
        db.add(
            InventorySnapshotItem(
                inventory_campaign_id=campaign.id,
                product_id=product.id,
                location_id=snapshot.location_id,
                internal_reference_snapshot=product.internal_reference,
                description_snapshot=product.name,
                expected_quantity=snapshot.quantity,
                sale_price_snapshot=product.sale_price,
                cost_snapshot=product.cost,
                consignment_cost_snapshot=product.consignment_cost,
                effective_cost_snapshot=effective,
                cost_source=source,
                currency_snapshot=product.currency,
                supplier_reference_snapshot=supplier_ref,
            )
        )
        hashed.append(
            {
                "product_id": product.id,
                "internal_reference_snapshot": product.internal_reference,
                "expected_quantity": snapshot.quantity,
                "sale_price_snapshot": product.sale_price,
                "cost_snapshot": product.cost,
                "consignment_cost_snapshot": product.consignment_cost,
                "effective_cost_snapshot": effective,
                "cost_source": source.value,
                "currency_snapshot": product.currency,
                "location_id": snapshot.location_id,
            }
        )
    db.flush()
    return len(hashed), _snapshot_hash(hashed)


def snapshot_item_count(db: Session, campaign_id: uuid.UUID) -> int:
    return db.execute(
        select(func.count())
        .select_from(InventorySnapshotItem)
        .where(InventorySnapshotItem.inventory_campaign_id == campaign_id)
    ).scalar_one()


def list_snapshot_items(
    db: Session, campaign_id: uuid.UUID, *, offset: int, limit: int
) -> list[InventorySnapshotItem]:
    return list(
        db.execute(
            select(InventorySnapshotItem)
            .where(InventorySnapshotItem.inventory_campaign_id == campaign_id)
            .order_by(InventorySnapshotItem.internal_reference_snapshot)
            .offset(offset)
            .limit(limit)
        ).scalars()
    )


def freeze_timestamp() -> dt.datetime:
    return dt.datetime.now(dt.UTC)
