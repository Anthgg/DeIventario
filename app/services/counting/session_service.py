"""Servicio de sesiones de conteo: arranque, consulta, finish-check y submit."""

from __future__ import annotations

import datetime as dt
import decimal
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    InventoryAssignment,
    InventoryCampaign,
    InventoryCountSession,
    InventoryCountTotal,
    InventoryDamage,
    InventoryExtraItem,
    InventoryRecount,
    InventorySnapshotItem,
    InventoryUnknownCode,
    Product,
    User,
)
from app.models.enums import (
    AssignmentStatus,
    CampaignStatus,
    RecountStatus,
    SessionStatus,
    SessionType,
)
from app.services.auth import audit_service
from app.services.counting.errors import CountError
from app.services.inventory import campaign_service


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


OPEN_RECOUNT_STATUSES = (
    RecountStatus.REQUESTED,
    RecountStatus.ASSIGNED,
    RecountStatus.IN_PROGRESS,
)


def lock_session(db: Session, session_id: uuid.UUID) -> InventoryCountSession:
    session = db.execute(
        select(InventoryCountSession)
        .where(InventoryCountSession.id == session_id)
        .with_for_update()
    ).scalar_one_or_none()
    if session is None:
        raise CountError("Sesion de conteo no encontrada", 404)
    return session


def get_session(db: Session, session_id: uuid.UUID) -> InventoryCountSession:
    session = db.get(InventoryCountSession, session_id)
    if session is None:
        raise CountError("Sesion de conteo no encontrada", 404)
    return session


def assert_campaign_active(db: Session, session: InventoryCountSession) -> InventoryCampaign:
    campaign = db.get(InventoryCampaign, session.inventory_campaign_id)
    if campaign is None:
        raise CountError("Campana no encontrada", 404)
    if campaign_service.expire_campaign_if_due(db, campaign):
        db.commit()
        raise CountError("La campana expiro", 409, "CAMPAIGN_EXPIRED")
    if campaign.status is CampaignStatus.CLOSED:
        raise CountError("La campana esta cerrada", 409, "CAMPAIGN_CLOSED")
    # F007: RECOUNT es un estado operativo (la campana esta en reconteo).
    if campaign.status not in (CampaignStatus.IN_PROGRESS, CampaignStatus.RECOUNT):
        raise CountError("La campana no esta en curso", 409, "CAMPAIGN_NOT_ACTIVE")
    if campaign.deadline_at is not None and campaign.deadline_at <= _now():
        raise CountError("La campana expiro", 409, "CAMPAIGN_EXPIRED")
    return campaign


def start_session(
    db: Session, *, campaign_id: uuid.UUID, actor_id: uuid.UUID
) -> tuple[InventoryCountSession, bool]:
    campaign = campaign_service.lock_campaign(db, campaign_id)
    if campaign_service.expire_campaign_if_due(db, campaign):
        db.commit()
        raise CountError("La campana expiro", 409, "CAMPAIGN_EXPIRED")
    if campaign.status is CampaignStatus.CLOSED:
        raise CountError("La campana esta cerrada", 409, "CAMPAIGN_CLOSED")
    if campaign.status not in (CampaignStatus.IN_PROGRESS, CampaignStatus.RECOUNT):
        raise CountError("La campana no esta en curso", 409, "CAMPAIGN_NOT_ACTIVE")
    if campaign.deadline_at is not None and campaign.deadline_at <= _now():
        raise CountError("La campana expiro", 409, "CAMPAIGN_EXPIRED")

    assignment = campaign_service.active_assignment(db, campaign_id)
    if assignment is None:
        raise CountError("La campana no tiene responsable activo", 409)
    if assignment.user_id != actor_id:
        raise CountError("No eres el responsable activo de esta campana", 403)

    existing = db.execute(
        select(InventoryCountSession)
        .where(
            InventoryCountSession.assignment_id == assignment.id,
            InventoryCountSession.status.in_((SessionStatus.PENDING, SessionStatus.IN_PROGRESS)),
        )
        .limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        db.commit()
        return existing, True

    prior = db.execute(
        select(func.count())
        .select_from(InventoryAssignment)
        .where(
            InventoryAssignment.inventory_campaign_id == campaign_id,
            InventoryAssignment.id != assignment.id,
        )
    ).scalar_one()
    session_type = SessionType.REASSIGNMENT if prior > 0 else SessionType.INITIAL
    # F007: si el responsable activo tiene un reconteo abierto, la sesion que
    # arranca es SIEMPRE la de reconteo (lo decide el servidor, no el cliente).
    open_recount = db.execute(
        select(InventoryRecount)
        .where(
            InventoryRecount.inventory_campaign_id == campaign_id,
            InventoryRecount.assigned_user_id == actor_id,
            InventoryRecount.status.in_((RecountStatus.REQUESTED, RecountStatus.ASSIGNED)),
        )
        .with_for_update()
        .limit(1)
    ).scalar_one_or_none()
    if open_recount is not None:
        session_type = SessionType.RECOUNT
    max_number = db.execute(
        select(func.coalesce(func.max(InventoryCountSession.session_number), 0)).where(
            InventoryCountSession.inventory_campaign_id == campaign_id
        )
    ).scalar_one()
    session = InventoryCountSession(
        inventory_campaign_id=campaign_id,
        assignment_id=assignment.id,
        user_id=actor_id,
        session_number=int(max_number) + 1,
        session_type=session_type,
        status=SessionStatus.IN_PROGRESS,
        started_at=_now(),
        version=1,
        last_sequence=0,
        last_activity_at=_now(),
    )
    db.add(session)
    db.flush()
    audit_service.record(
        db,
        action=audit_service.COUNT_SESSION_STARTED,
        actor_user_id=actor_id,
        entity_type="inventory_count_session",
        entity_id=session.id,
        metadata={
            "campaign_id": str(campaign_id),
            "assignment_id": str(assignment.id),
            "session_number": session.session_number,
            "session_type": session_type.value,
        },
    )
    if open_recount is not None:
        open_recount.status = RecountStatus.IN_PROGRESS
        open_recount.resulting_session_id = session.id
        open_recount.started_at = _now()
        open_recount.expected_version += 1
        audit_service.record(
            db,
            action=audit_service.RECOUNT_STARTED,
            actor_user_id=actor_id,
            entity_type="inventory_recount",
            entity_id=open_recount.id,
            metadata={
                "campaign_id": str(campaign_id),
                "session_id": str(session.id),
                "assigned_user_id": str(open_recount.assigned_user_id),
                "expected_version": open_recount.expected_version,
            },
        )
    db.commit()
    return session, False


def cancel_sessions_for_assignment(db: Session, assignment_id: uuid.UUID) -> int:
    """Cancela las sesiones IN_PROGRESS de una asignacion revocada (sin borrar historia)."""
    sessions = list(
        db.execute(
            select(InventoryCountSession).where(
                InventoryCountSession.assignment_id == assignment_id,
                InventoryCountSession.status == SessionStatus.IN_PROGRESS,
            )
        ).scalars()
    )
    for session in sessions:
        session.status = SessionStatus.CANCELLED
        session.version += 1
        audit_service.record(
            db,
            action=audit_service.COUNT_SESSION_CANCELLED_BY_REASSIGNMENT,
            entity_type="inventory_count_session",
            entity_id=session.id,
            metadata={
                "assignment_id": str(assignment_id),
                "session_number": session.session_number,
            },
        )
    if sessions:
        db.flush()
    return len(sessions)


def _q(value: decimal.Decimal | int | None) -> str:
    """Cantidades con escala fija de 4 decimales (formato estable para la PWA)."""
    return f"{decimal.Decimal(value if value is not None else 0):.4f}"


def session_actuals(db: Session, session_id: uuid.UUID) -> tuple[decimal.Decimal, int, int]:
    units = db.execute(
        select(func.coalesce(func.sum(InventoryCountTotal.quantity), 0)).where(
            InventoryCountTotal.session_id == session_id
        )
    ).scalar_one()
    distinct = db.execute(
        select(func.count())
        .select_from(InventoryCountTotal)
        .where(InventoryCountTotal.session_id == session_id, InventoryCountTotal.quantity != 0)
    ).scalar_one()
    from app.models import InventoryCountEvent

    events = db.execute(
        select(func.count())
        .select_from(InventoryCountEvent)
        .where(InventoryCountEvent.session_id == session_id)
    ).scalar_one()
    return decimal.Decimal(units), int(distinct), int(events)


def session_items(db: Session, session_id: uuid.UUID) -> list[dict[str, object]]:
    """Items operativos BLIND-SAFE: PRODUCT (incluye extras sin marcar) y UNKNOWN.

    Nunca expone is_extra, expected, diferencia ni costos: la clasificacion es
    administrativa. Un UNKNOWN se identifica solo por kind para que el frontend
    sepa que no hay descripcion maestra (sin error ni bloqueo).
    """
    rows = db.execute(
        select(InventoryCountTotal, Product)
        .join(Product, Product.id == InventoryCountTotal.product_id)
        .where(
            InventoryCountTotal.session_id == session_id,
            InventoryCountTotal.quantity != 0,
        )
    ).all()
    items: list[dict[str, object]] = [
        {
            "kind": "PRODUCT",
            "product_id": str(product.id),
            "internal_reference": product.internal_reference,
            "name": product.name,
            "quantity": _q(total.quantity),
            "damaged_quantity": _q(total.damaged_quantity),
        }
        for total, product in rows
    ]
    unknowns = db.execute(
        select(InventoryUnknownCode)
        .where(
            InventoryUnknownCode.session_id == session_id,
            InventoryUnknownCode.quantity != 0,
        )
    ).scalars()
    items.extend(
        {
            "kind": "UNKNOWN",
            "product_id": None,
            "internal_reference": unknown.scanned_code,
            "name": None,
            "quantity": _q(unknown.quantity),
            "damaged_quantity": _q(unknown.damaged_quantity),
        }
        for unknown in unknowns
    )
    items.sort(key=lambda item: str(item["internal_reference"]))
    return items


def finish_check(db: Session, session: InventoryCountSession) -> dict[str, object]:
    """Presencia/ausencia de productos del snapshot (sin cantidades esperadas).

    Un UNKNOWN resuelto con quantity > 0 satisface la presencia de su
    producto resuelto; un UNKNOWN sin resolver no satisface a ninguno.
    """
    snapshot_items = list(
        db.execute(
            select(InventorySnapshotItem).where(
                InventorySnapshotItem.inventory_campaign_id == session.inventory_campaign_id
            )
        ).scalars()
    )
    totals = {
        total.product_id: total.quantity
        for total in db.execute(
            select(InventoryCountTotal).where(InventoryCountTotal.session_id == session.id)
        ).scalars()
    }
    present = {product_id for product_id, quantity in totals.items() if quantity != 0}
    resolved = db.execute(
        select(InventoryUnknownCode.resolved_product_id).where(
            InventoryUnknownCode.session_id == session.id,
            InventoryUnknownCode.quantity != 0,
            InventoryUnknownCode.resolved_product_id.is_not(None),
        )
    ).scalars()
    present.update(product_id for product_id in resolved if product_id is not None)
    missing = [item for item in snapshot_items if item.product_id not in present]
    return {
        "has_missing": bool(missing),
        "missing_products": [
            {
                "product_id": str(item.product_id),
                "internal_reference": item.internal_reference_snapshot,
                "name": item.description_snapshot,
            }
            for item in missing
        ],
    }


def _has_exceptions(db: Session, session_id: uuid.UUID) -> dict[str, bool]:
    """Presencia de excepciones para el audit de submit (sin cantidades)."""
    damages = db.execute(
        select(func.count())
        .select_from(InventoryDamage)
        .where(InventoryDamage.session_id == session_id)
    ).scalar_one()
    extras = db.execute(
        select(func.count())
        .select_from(InventoryExtraItem)
        .where(InventoryExtraItem.session_id == session_id, InventoryExtraItem.quantity != 0)
    ).scalar_one()
    unknowns = db.execute(
        select(func.count())
        .select_from(InventoryUnknownCode)
        .where(InventoryUnknownCode.session_id == session_id, InventoryUnknownCode.quantity != 0)
    ).scalar_one()
    return {
        "has_damage": damages > 0,
        "has_extras": extras > 0,
        "has_unknowns": unknowns > 0,
    }


def submit_session(
    db: Session,
    *,
    session_id: uuid.UUID,
    actor_id: uuid.UUID,
    expected_version: int,
    confirm_missing: bool,
) -> tuple[InventoryCountSession, bool]:
    session = lock_session(db, session_id)
    if session.status is SessionStatus.SUBMITTED:
        db.commit()
        return session, True
    if session.version != expected_version:
        raise CountError("Conflicto de version", 409)
    if session.status is not SessionStatus.IN_PROGRESS:
        raise CountError("La sesion no esta en curso", 409, "SESSION_NOT_OPEN")
    campaign = assert_campaign_active(db, session)
    if session.user_id != actor_id:
        raise CountError("No eres el dueno de esta sesion", 403)
    assignment = campaign_service.active_assignment(db, session.inventory_campaign_id)
    if assignment is None or assignment.user_id != actor_id:
        raise CountError("La asignacion activa no te corresponde", 409)

    check = finish_check(db, session)
    if check["has_missing"] and not confirm_missing:
        raise CountError(
            "Existen productos sin registrar: confirme para enviar",
            409,
            "MISSING_PRODUCTS_CONFIRMATION_REQUIRED",
            payload={"missing_products": check["missing_products"]},
        )

    # F006: dano/extras/unknowns NUNCA bloquean el submit; quedan disponibles
    # en los endpoints administrativos (auto-envio a revision sin email/push).
    exceptions = _has_exceptions(db, session.id)

    session.status = SessionStatus.SUBMITTED
    session.submitted_at = _now()
    session.version += 1
    assignment.status = AssignmentStatus.COMPLETED
    campaign.status = CampaignStatus.SUBMITTED
    campaign.submitted_at = _now()
    campaign.version += 1
    audit_service.record(
        db,
        action=audit_service.COUNT_SESSION_SUBMITTED,
        actor_user_id=actor_id,
        entity_type="inventory_count_session",
        entity_id=session.id,
        metadata={"campaign_id": str(campaign.id), "session_number": session.session_number},
    )
    audit_service.record(
        db,
        action=audit_service.CAMPAIGN_SUBMITTED,
        actor_user_id=actor_id,
        entity_type="inventory_campaign",
        entity_id=campaign.id,
        metadata={"code": campaign.code, "version": campaign.version},
    )
    if any(exceptions.values()):
        audit_service.record(
            db,
            action=audit_service.COUNT_EXCEPTIONS_READY,
            actor_user_id=actor_id,
            entity_type="inventory_count_session",
            entity_id=session.id,
            metadata={
                "session_id": str(session.id),
                "has_damage": exceptions["has_damage"],
                "has_extras": exceptions["has_extras"],
                "has_unknowns": exceptions["has_unknowns"],
            },
        )
    # F007: al enviar la sesion de reconteo, el reconteo queda COMPLETED.
    # La sesion SUBMITTED previa y el estado de campana ya fueron registrados
    # arriba (sin eventos duplicados).
    if session.session_type is SessionType.RECOUNT:
        recount = db.execute(
            select(InventoryRecount)
            .where(
                InventoryRecount.resulting_session_id == session.id,
                InventoryRecount.status.in_(OPEN_RECOUNT_STATUSES),
            )
            .with_for_update()
        ).scalar_one_or_none()
        if recount is not None:
            recount.status = RecountStatus.COMPLETED
            recount.completed_at = _now()
            recount.expected_version += 1
            audit_service.record(
                db,
                action=audit_service.RECOUNT_COMPLETED,
                actor_user_id=actor_id,
                entity_type="inventory_recount",
                entity_id=recount.id,
                metadata={
                    "campaign_id": str(campaign.id),
                    "session_id": str(session.id),
                    "assigned_user_id": str(recount.assigned_user_id),
                    "expected_version": recount.expected_version,
                },
            )
    db.commit()
    return session, False


def list_campaign_sessions(db: Session, campaign_id: uuid.UUID) -> list[dict[str, object]]:
    sessions = list(
        db.execute(
            select(InventoryCountSession)
            .where(InventoryCountSession.inventory_campaign_id == campaign_id)
            .order_by(InventoryCountSession.session_number)
        ).scalars()
    )
    user_ids = {session.user_id for session in sessions}
    users: dict[uuid.UUID, User] = {}
    if user_ids:
        users = {
            user.id: user
            for user in db.execute(select(User).where(User.id.in_(user_ids))).scalars()
        }
    result: list[dict[str, object]] = []
    for session in sessions:
        units, distinct, events = session_actuals(db, session.id)
        user = users.get(session.user_id)
        result.append(
            {
                "id": str(session.id),
                "session_number": session.session_number,
                "session_type": session.session_type.value,
                "status": session.status.value,
                "user_id": str(session.user_id),
                "user_display_name": user.display_name if user is not None else None,
                "assignment_id": str(session.assignment_id) if session.assignment_id else None,
                "started_at": session.started_at.isoformat() if session.started_at else None,
                "submitted_at": session.submitted_at.isoformat() if session.submitted_at else None,
                "last_activity_at": (
                    session.last_activity_at.isoformat() if session.last_activity_at else None
                ),
                "actual_units_registered": _q(units),
                "distinct_products_registered": distinct,
                "event_count": events,
            }
        )
    return result


def session_payload(db: Session, session: InventoryCountSession) -> dict[str, object]:
    units, distinct, events = session_actuals(db, session.id)
    return {
        "id": str(session.id),
        "campaign_id": str(session.inventory_campaign_id),
        "assignment_id": str(session.assignment_id) if session.assignment_id else None,
        "user_id": str(session.user_id),
        "session_number": session.session_number,
        "session_type": session.session_type.value,
        "status": session.status.value,
        "started_at": session.started_at.isoformat() if session.started_at else None,
        "submitted_at": session.submitted_at.isoformat() if session.submitted_at else None,
        "last_activity_at": (
            session.last_activity_at.isoformat() if session.last_activity_at else None
        ),
        "version": session.version,
        "actual_units_registered": _q(units),
        "distinct_products_registered": distinct,
        "event_count": events,
    }
