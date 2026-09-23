"""Lectura pura de evidencia para la conciliacion (F008).

Solo LEE sesiones, totales, unknowns y snapshot. Nunca escribe nada.
Cantidades efectivas por producto/sesion: conocido (count_totals) mas
unknowns resueltos de la misma sesion. El damage NO reduce lo fisico.
"""

from __future__ import annotations

import datetime as dt
import decimal
import hashlib
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    InventoryCountSession,
    InventoryCountTotal,
    InventorySnapshotItem,
    InventoryUnknownCode,
    Product,
    User,
)
from app.models.enums import SessionStatus
from app.services.reconciliation.errors import ReconciliationError

ZERO = decimal.Decimal("0")

EffectiveMap = dict[uuid.UUID, dict[uuid.UUID, tuple[decimal.Decimal, decimal.Decimal]]]


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def submitted_sessions(db: Session, campaign_id: uuid.UUID) -> list[InventoryCountSession]:
    return list(
        db.execute(
            select(InventoryCountSession)
            .where(
                InventoryCountSession.inventory_campaign_id == campaign_id,
                InventoryCountSession.status == SessionStatus.SUBMITTED,
            )
            .order_by(InventoryCountSession.session_number, InventoryCountSession.id)
        ).scalars()
    )


def source_fingerprint(db: Session, campaign_id: uuid.UUID) -> str:
    """SHA-256 determinista de las sesiones SUBMITTED (fuente oficial F008)."""
    hasher = hashlib.sha256()
    for session in submitted_sessions(db, campaign_id):
        submitted = _iso(session.submitted_at) or ""
        hasher.update(
            f"{session.id}|{session.session_number}|{session.session_type.value}"
            f"|{submitted}|{session.version}\n".encode()
        )
    return hasher.hexdigest()


def effective_map(db: Session, campaign_id: uuid.UUID) -> EffectiveMap:
    """{session_id: {product_id: (physical, damaged)}} para sesiones SUBMITTED."""
    sessions = submitted_sessions(db, campaign_id)
    session_ids = [session.id for session in sessions]
    result: EffectiveMap = {session_id: {} for session_id in session_ids}
    if not session_ids:
        return result

    totals = db.execute(
        select(
            InventoryCountTotal.session_id,
            InventoryCountTotal.product_id,
            InventoryCountTotal.quantity,
            InventoryCountTotal.damaged_quantity,
        ).where(InventoryCountTotal.session_id.in_(session_ids))
    ).all()
    mutable: dict[uuid.UUID, dict[uuid.UUID, list[decimal.Decimal]]] = {
        session_id: {} for session_id in session_ids
    }
    for session_id, product_id, quantity, damaged in totals:
        mutable[session_id][product_id] = [quantity, damaged]

    unknowns = db.execute(
        select(
            InventoryUnknownCode.session_id,
            InventoryUnknownCode.resolved_product_id,
            func.sum(InventoryUnknownCode.quantity),
            func.sum(InventoryUnknownCode.damaged_quantity),
        )
        .where(
            InventoryUnknownCode.session_id.in_(session_ids),
            InventoryUnknownCode.resolved_product_id.is_not(None),
        )
        .group_by(
            InventoryUnknownCode.session_id,
            InventoryUnknownCode.resolved_product_id,
        )
    ).all()
    for session_id, product_id, quantity, damaged in unknowns:
        if product_id is None:
            continue
        slot = mutable[session_id].setdefault(product_id, [ZERO, ZERO])
        slot[0] += quantity if quantity is not None else ZERO
        slot[1] += damaged if damaged is not None else ZERO

    for session_id, products in mutable.items():
        for product_id, (physical, damaged) in products.items():
            if damaged > physical:
                raise ReconciliationError(
                    "Integridad violada: dano mayor que la cantidad fisica",
                    409,
                    "DAMAGED_EXCEEDS_PHYSICAL",
                    {"session_id": str(session_id), "product_id": str(product_id)},
                )
            result[session_id][product_id] = (physical, damaged)
    return result


def snapshot_expected(db: Session, campaign_id: uuid.UUID) -> dict[uuid.UUID, decimal.Decimal]:
    rows = db.execute(
        select(
            InventorySnapshotItem.product_id,
            InventorySnapshotItem.expected_quantity,
        ).where(InventorySnapshotItem.inventory_campaign_id == campaign_id)
    ).all()
    return {row[0]: row[1] for row in rows}


def product_universe(db: Session, campaign_id: uuid.UUID) -> dict[uuid.UUID, decimal.Decimal]:
    """{product_id: expected_quantity}: snapshot + contados + resueltos (§5)."""
    expected = snapshot_expected(db, campaign_id)
    session_ids = [s.id for s in submitted_sessions(db, campaign_id)]
    if session_ids:
        for (product_id,) in db.execute(
            select(InventoryCountTotal.product_id)
            .where(InventoryCountTotal.session_id.in_(session_ids))
            .distinct()
        ).all():
            expected.setdefault(product_id, ZERO)
        for (product_id,) in db.execute(
            select(InventoryUnknownCode.resolved_product_id)
            .where(
                InventoryUnknownCode.session_id.in_(session_ids),
                InventoryUnknownCode.resolved_product_id.is_not(None),
            )
            .distinct()
        ).all():
            if product_id is not None:
                expected.setdefault(product_id, ZERO)
    return expected


def unresolved_unknowns(db: Session, campaign_id: uuid.UUID) -> list[InventoryUnknownCode]:
    session_ids = [s.id for s in submitted_sessions(db, campaign_id)]
    if not session_ids:
        return []
    return list(
        db.execute(
            select(InventoryUnknownCode)
            .where(
                InventoryUnknownCode.session_id.in_(session_ids),
                InventoryUnknownCode.resolved_product_id.is_(None),
                InventoryUnknownCode.quantity > 0,
            )
            .order_by(InventoryUnknownCode.session_id, InventoryUnknownCode.scanned_code)
        ).scalars()
    )


def unresolved_unknown_count(db: Session, campaign_id: uuid.UUID) -> int:
    session_ids = [s.id for s in submitted_sessions(db, campaign_id)]
    if not session_ids:
        return 0
    return int(
        db.execute(
            select(func.count())
            .select_from(InventoryUnknownCode)
            .where(
                InventoryUnknownCode.session_id.in_(session_ids),
                InventoryUnknownCode.resolved_product_id.is_(None),
                InventoryUnknownCode.quantity > 0,
            )
        ).scalar_one()
    )


def negative_expected(universe: dict[uuid.UUID, decimal.Decimal]) -> list[uuid.UUID]:
    return sorted(
        (pid for pid, qty in universe.items() if qty < 0),
        key=str,
    )


def products_sorted(
    db: Session, universe: dict[uuid.UUID, decimal.Decimal]
) -> list[tuple[uuid.UUID, decimal.Decimal, Product | None]]:
    product_ids = list(universe)
    products: dict[uuid.UUID, Product] = {}
    if product_ids:
        products = {
            row.id: row
            for row in db.execute(select(Product).where(Product.id.in_(product_ids))).scalars()
        }
    ordered = sorted(
        product_ids,
        key=lambda pid: (
            products[pid].internal_reference if pid in products else "",
            str(pid),
        ),
    )
    return [(pid, universe[pid], products.get(pid)) for pid in ordered]


def session_summaries(
    db: Session, sessions: list[InventoryCountSession]
) -> list[dict[str, object]]:
    user_ids = {session.user_id for session in sessions}
    users: dict[uuid.UUID, User] = {}
    if user_ids:
        users = {
            row.id: row for row in db.execute(select(User).where(User.id.in_(user_ids))).scalars()
        }
    summaries: list[dict[str, object]] = []
    for session in sessions:
        user = users.get(session.user_id)
        summaries.append(
            {
                "id": str(session.id),
                "session_number": session.session_number,
                "session_type": session.session_type.value,
                "status": session.status.value,
                "user_id": str(session.user_id),
                "user_display_name": user.display_name if user is not None else None,
                "submitted_at": _iso(session.submitted_at),
            }
        )
    return summaries
