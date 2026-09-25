"""Resolucion administrativa de codigos desconocidos (F006).

Regla F006/F008: resolver un UNKNOWN NO modifica eventos originales, NO
reemplaza scanned_code, NO borra inventory_unknown_codes y NO suma su
quantity dentro de inventory_count_totals del producto resuelto. Esa
combinacion (conteo conocido + unknown resuelto) pertenece a la
conciliacion de F008.
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    InventoryCountSession,
    InventoryCountTotal,
    InventoryDamage,
    InventoryExtraItem,
    InventoryUnknownCode,
    Product,
    User,
)
from app.services.auth import audit_service


class ResolveError(Exception):
    def __init__(self, message: str, status_code: int = 400, code: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _q(value: decimal.Decimal | int | None) -> str:
    return f"{decimal.Decimal(value if value is not None else 0):.4f}"


def resolve_unknown(
    db: Session, *, unknown_id: uuid.UUID, product_id: uuid.UUID, actor_id: uuid.UUID
) -> tuple[InventoryUnknownCode, bool]:
    """Resuelve administrativamente un codigo desconocido (idempotente)."""
    unknown = db.execute(
        select(InventoryUnknownCode)
        .where(InventoryUnknownCode.id == unknown_id)
        .with_for_update()
    ).scalar_one_or_none()
    if unknown is None:
        raise ResolveError("Codigo desconocido no encontrado", 404)

    product = db.get(Product, product_id)
    if product is None:
        raise ResolveError("Producto no encontrado", 404)
    if not product.active:
        raise ResolveError("El producto no esta activo", 409, "PRODUCT_INACTIVE")

    if unknown.resolved_product_id is not None:
        if unknown.resolved_product_id == product.id:
            # Mismo product_id: responde idempotentemente sin mutar nada.
            return unknown, True
        raise ResolveError(
            "El codigo ya fue resuelto hacia otro producto", 409, "UNKNOWN_ALREADY_RESOLVED"
        )

    unknown.resolved_product_id = product.id
    unknown.resolved_by = actor_id
    unknown.resolved_at = _now()
    audit_service.record(
        db,
        action=audit_service.UNKNOWN_CODE_RESOLVED,
        actor_user_id=actor_id,
        entity_type="inventory_unknown_code",
        entity_id=unknown.id,
        metadata={
            "session_id": str(unknown.session_id),
            "scanned_code": unknown.scanned_code,
            "product_id": str(product.id),
        },
    )
    db.commit()
    return unknown, False


def campaign_damages(
    db: Session, campaign_id: uuid.UUID, *, offset: int, limit: int
) -> tuple[list[dict[str, object]], int]:
    total = db.execute(
        select(func.count())
        .select_from(InventoryDamage)
        .join(
            InventoryCountSession,
            InventoryCountSession.id == InventoryDamage.session_id,
        )
        .where(InventoryCountSession.inventory_campaign_id == campaign_id)
    ).scalar_one()
    rows = db.execute(
        select(InventoryDamage, InventoryCountSession, Product, User)
        .join(
            InventoryCountSession,
            InventoryCountSession.id == InventoryDamage.session_id,
        )
        .outerjoin(Product, Product.id == InventoryDamage.product_id)
        .outerjoin(User, User.id == InventoryDamage.created_by)
        .where(InventoryCountSession.inventory_campaign_id == campaign_id)
        .order_by(InventoryDamage.created_at)
        .offset(offset)
        .limit(limit)
    ).all()
    items: list[dict[str, object]] = []
    for damage, session, product, user in rows:
        items.append(
            {
                "id": str(damage.id),
                "session_id": str(session.id),
                "session_number": session.session_number,
                "user_id": str(damage.created_by) if damage.created_by else None,
                "user_display_name": user.display_name if user is not None else None,
                "product_id": str(damage.product_id) if damage.product_id else None,
                "internal_reference": product.internal_reference if product is not None else None,
                "scanned_code": damage.scanned_code,
                "quantity": _q(damage.quantity),
                "action": damage.action.value,
                "reason": damage.reason,
                "observation": damage.observation,
                "has_evidence": damage.evidence_path is not None,
                "created_at": _iso(damage.created_at),
            }
        )
    return items, int(total)


def campaign_extras(
    db: Session,
    campaign_id: uuid.UUID,
    *,
    include_zero: bool,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, object]]:
    query = (
        select(InventoryExtraItem, Product, InventoryCountSession)
        .join(Product, Product.id == InventoryExtraItem.product_id)
        .join(
            InventoryCountSession,
            InventoryCountSession.id == InventoryExtraItem.session_id,
        )
        .where(InventoryCountSession.inventory_campaign_id == campaign_id)
    )
    if not include_zero:
        query = query.where(InventoryExtraItem.quantity != 0)
    rows = db.execute(
        query.order_by(Product.internal_reference, InventoryExtraItem.id)
        .offset(offset)
        .limit(limit)
    ).all()
    return [
        {
            "id": str(extra.id),
            "session_id": str(extra.session_id),
            "product_id": str(extra.product_id),
            "internal_reference": product.internal_reference,
            "name": product.name,
            "quantity": _q(extra.quantity),
            "source": "DIRECT_PRODUCT_SCAN",
            "first_detected_at": _iso(extra.first_detected_at),
        }
        for extra, product, _session in rows
    ]


def campaign_unknown_codes(
    db: Session, campaign_id: uuid.UUID, *, limit: int = 100, offset: int = 0
) -> list[dict[str, object]]:
    rows = db.execute(
        select(InventoryUnknownCode)
        .join(
            InventoryCountSession,
            InventoryCountSession.id == InventoryUnknownCode.session_id,
        )
        .where(InventoryCountSession.inventory_campaign_id == campaign_id)
        .order_by(InventoryUnknownCode.created_at, InventoryUnknownCode.id)
        .offset(offset)
        .limit(limit)
    ).scalars()
    return [
        {
            "id": str(unknown.id),
            "session_id": str(unknown.session_id),
            "scanned_code": unknown.scanned_code,
            "quantity": _q(unknown.quantity),
            "damaged_quantity": _q(unknown.damaged_quantity),
            "resolved_product_id": (
                str(unknown.resolved_product_id) if unknown.resolved_product_id else None
            ),
            "resolved_at": _iso(unknown.resolved_at),
            "created_at": _iso(unknown.created_at),
        }
        for unknown in rows
    ]


def exceptions_summary(db: Session, campaign_id: uuid.UUID) -> dict[str, object]:
    """Resumen administrativo de excepciones. Sin dinero (F009 hara valuacion)."""
    session_ids = select(InventoryCountSession.id).where(
        InventoryCountSession.inventory_campaign_id == campaign_id
    )
    damaged_records = db.execute(
        select(func.count())
        .select_from(InventoryDamage)
        .where(InventoryDamage.session_id.in_(session_ids))
    ).scalar_one()
    damaged_units = db.execute(
        select(func.coalesce(func.sum(InventoryCountTotal.damaged_quantity), 0)).where(
            InventoryCountTotal.session_id.in_(session_ids)
        )
    ).scalar_one()
    unknown_damaged = db.execute(
        select(func.coalesce(func.sum(InventoryUnknownCode.damaged_quantity), 0))
        .select_from(InventoryUnknownCode)
        .where(InventoryUnknownCode.session_id.in_(session_ids))
    ).scalar_one()
    extras = db.execute(
        select(
            func.count(),
            func.coalesce(func.sum(InventoryExtraItem.quantity), 0),
        )
        .select_from(InventoryExtraItem)
        .where(
            InventoryExtraItem.session_id.in_(session_ids),
            InventoryExtraItem.quantity != 0,
        )
    ).one()
    unknown_count = db.execute(
        select(func.count())
        .select_from(InventoryUnknownCode)
        .where(InventoryUnknownCode.session_id.in_(session_ids))
    ).scalar_one()
    resolved_count = db.execute(
        select(func.count())
        .select_from(InventoryUnknownCode)
        .where(
            InventoryUnknownCode.session_id.in_(session_ids),
            InventoryUnknownCode.resolved_product_id.is_not(None),
        )
    ).scalar_one()
    unknown_units = db.execute(
        select(func.coalesce(func.sum(InventoryUnknownCode.quantity), 0))
        .select_from(InventoryUnknownCode)
        .where(InventoryUnknownCode.session_id.in_(session_ids))
    ).scalar_one()
    return {
        "damaged_records_count": int(damaged_records),
        "damaged_units_current": _q(
            decimal.Decimal(str(damaged_units)) + decimal.Decimal(str(unknown_damaged))
        ),
        "extra_products_count": int(extras[0]),
        "extra_units_current": _q(extras[1]),
        "unknown_codes_count": int(unknown_count),
        "unknown_units_current": _q(unknown_units),
        "resolved_unknown_count": int(resolved_count),
        "unresolved_unknown_count": int(unknown_count) - int(resolved_count),
    }
