"""Reconteos ciegos (F007): sesiones independientes sin conciliacion.

Reglas clave:
- Maximo UN reconteo abierto por campana (indice unico parcial + check).
- La sesion de reconteo arranca en cero: sin copiar eventos, totales,
  extras, unknowns ni dannos de la sesion de origen.
- El source_session_id es solo referencia administrativa: NUNCA se precarga
  ni se expone al operario asignado (blind mode total).
- Sin conciliacion, diferencias ni valuacion (eso es F008).
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.permissions import INVENTORY_COUNT
from app.models import (
    InventoryAssignment,
    InventoryCountSession,
    InventoryRecount,
    User,
)
from app.models.enums import (
    AssignmentStatus,
    CampaignStatus,
    RecountStatus,
    SessionStatus,
)
from app.services.auth import audit_service, rbac_service
from app.services.counting import session_service
from app.services.inventory import campaign_service

OPEN_RECOUNT_STATUSES = (
    RecountStatus.REQUESTED,
    RecountStatus.ASSIGNED,
    RecountStatus.IN_PROGRESS,
)

REQUESTABLE_CAMPAIGN_STATUSES = (
    CampaignStatus.SUBMITTED,
    CampaignStatus.RECOUNT,
    CampaignStatus.UNDER_REVIEW,
)


class RecountError(Exception):
    """Error de dominio de reconteos (codigo estable y payload opcional)."""

    def __init__(
        self,
        message: str,
        status_code: int = 400,
        code: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.payload = payload or {}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _ref(value: uuid.UUID | None) -> str | None:
    return str(value) if value is not None else None


def lock_recount(db: Session, recount_id: uuid.UUID) -> InventoryRecount:
    recount = db.execute(
        select(InventoryRecount).where(InventoryRecount.id == recount_id).with_for_update()
    ).scalar_one_or_none()
    if recount is None:
        raise RecountError("Reconteo no encontrado", 404, "RECOUNT_NOT_FOUND")
    return recount


def check_recount_version(recount: InventoryRecount, expected_version: int | None) -> None:
    if expected_version is not None and recount.expected_version != expected_version:
        raise RecountError("Conflicto de version", 409, "VERSION_CONFLICT")


def open_recount_for_campaign(
    db: Session, campaign_id: uuid.UUID, *, for_update: bool = False
) -> InventoryRecount | None:
    query = (
        select(InventoryRecount)
        .where(
            InventoryRecount.inventory_campaign_id == campaign_id,
            InventoryRecount.status.in_(OPEN_RECOUNT_STATUSES),
        )
        .limit(1)
    )
    if for_update:
        query = query.with_for_update()
    return db.execute(query).scalar_one_or_none()


def _latest_submitted_session_id(db: Session, campaign_id: uuid.UUID) -> uuid.UUID | None:
    """Sesion SUBMITTED mas reciente (regla por defecto de source_session_id).

    Orden: submitted_at DESC con NULLS ultimo y, en empate, session_number DESC.
    """
    return db.execute(
        select(InventoryCountSession.id)
        .where(
            InventoryCountSession.inventory_campaign_id == campaign_id,
            InventoryCountSession.status == SessionStatus.SUBMITTED,
        )
        .order_by(
            InventoryCountSession.submitted_at.desc().nullslast(),
            InventoryCountSession.session_number.desc(),
        )
        .limit(1)
    ).scalar_one_or_none()


def _validate_assignee(db: Session, user_id: uuid.UUID) -> User:
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise RecountError("Usuario no valido o inactivo", 400)
    if INVENTORY_COUNT not in rbac_service.user_permissions(db, user.id):
        raise RecountError("El usuario no tiene permiso inventory.count", 400)
    return user


def _current_assignment_id(
    db: Session, campaign_id: uuid.UUID, user_id: uuid.UUID
) -> uuid.UUID | None:
    """Assignment actual del reconteo (ACTIVE si existe; si no, la mas reciente)."""
    active = db.execute(
        select(InventoryAssignment.id)
        .where(
            InventoryAssignment.inventory_campaign_id == campaign_id,
            InventoryAssignment.user_id == user_id,
            InventoryAssignment.status == AssignmentStatus.ACTIVE,
            InventoryAssignment.revoked_at.is_(None),
        )
        .limit(1)
    ).scalar_one_or_none()
    if active is not None:
        return active
    latest = db.execute(
        select(InventoryAssignment.id)
        .where(
            InventoryAssignment.inventory_campaign_id == campaign_id,
            InventoryAssignment.user_id == user_id,
        )
        .order_by(InventoryAssignment.assigned_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return latest


def request_recount(
    db: Session,
    *,
    actor_id: uuid.UUID,
    campaign_id: uuid.UUID,
    assigned_user_id: uuid.UUID,
    reason: str | None = None,
    source_session_id: uuid.UUID | None = None,
    expected_version: int | None = None,
) -> InventoryRecount:
    """Solicita un reconteo ciego y crea su assignment (estado ASSIGNED)."""
    campaign = campaign_service.lock_campaign(db, campaign_id)
    campaign_service.require_not_closed(campaign)
    campaign_service.check_version(campaign, expected_version)
    if campaign.status not in REQUESTABLE_CAMPAIGN_STATUSES:
        raise RecountError(
            "La campana no admite reconteos en su estado actual", 409
        )
    # §44: deadline vencido => no se crea el reconteo. La campana queda
    # EXPIRED para que un administrador haga reopen valido via F004.
    if campaign.deadline_at is not None and campaign.deadline_at <= _now():
        campaign.status = CampaignStatus.EXPIRED
        campaign.version += 1
        audit_service.record(
            db,
            action=audit_service.CAMPAIGN_EXPIRED,
            entity_type="inventory_campaign",
            entity_id=campaign.id,
            metadata={"code": campaign.code, "version": campaign.version},
        )
        db.commit()
        raise RecountError("La campana expiro", 409, "CAMPAIGN_EXPIRED")
    if open_recount_for_campaign(db, campaign_id, for_update=True) is not None:
        raise RecountError(
            "Ya existe un reconteo abierto para esta campana", 409, "RECOUNT_ALREADY_OPEN"
        )
    _validate_assignee(db, assigned_user_id)

    if source_session_id is not None:
        source = db.get(InventoryCountSession, source_session_id)
        if source is None:
            raise RecountError("Sesion de origen no encontrada", 404)
        if (
            source.inventory_campaign_id != campaign_id
            or source.status is not SessionStatus.SUBMITTED
        ):
            raise RecountError(
                "La sesion de origen debe ser SUBMITTED de esta campana",
                409,
                "RECOUNT_SOURCE_INVALID",
            )
    else:
        # Regla por defecto documentada: ultima sesion SUBMITTED de la campana.
        source_session_id = _latest_submitted_session_id(db, campaign_id)

    # §11: la assignment ACTIVE previa (si existe) se revoca al entrar en
    # RECOUNT; su historia se conserva. Sesiones IN_PROGRESS huérfanas se
    # cancelan sin borrar eventos.
    current = campaign_service.active_assignment(db, campaign_id)
    if current is not None:
        session_service.cancel_sessions_for_assignment(db, current.id)
        current.status = AssignmentStatus.REVOKED
        current.revoked_at = _now()

    assignment = InventoryAssignment(
        inventory_campaign_id=campaign_id,
        user_id=assigned_user_id,
        assigned_by=actor_id,
        status=AssignmentStatus.ACTIVE,
    )
    db.add(assignment)
    db.flush()

    recount = InventoryRecount(
        inventory_campaign_id=campaign_id,
        requested_by=actor_id,
        assigned_user_id=assigned_user_id,
        source_session_id=source_session_id,
        status=RecountStatus.ASSIGNED,
        expected_version=1,
        reason=reason.strip() if reason and reason.strip() else None,
    )
    db.add(recount)
    db.flush()

    campaign.status = CampaignStatus.RECOUNT
    campaign.version += 1
    audit_service.record(
        db,
        action=audit_service.RECOUNT_REQUESTED,
        actor_user_id=actor_id,
        entity_type="inventory_recount",
        entity_id=recount.id,
        metadata={
            "campaign_id": str(campaign_id),
            "assigned_user_id": str(assigned_user_id),
            "source_session_id": _ref(source_session_id),
            "expected_version": recount.expected_version,
        },
    )
    audit_service.record(
        db,
        action=audit_service.RECOUNT_ASSIGNED,
        actor_user_id=actor_id,
        entity_type="inventory_recount",
        entity_id=recount.id,
        metadata={
            "campaign_id": str(campaign_id),
            "assignment_id": str(assignment.id),
            "assigned_user_id": str(assigned_user_id),
            "expected_version": recount.expected_version,
        },
    )
    try:
        db.commit()
    except IntegrityError as exc:
        # Respaldo del indice unico parcial ante dos requests concurrentes.
        db.rollback()
        raise RecountError(
            "Ya existe un reconteo abierto para esta campana", 409, "RECOUNT_ALREADY_OPEN"
        ) from exc
    return recount


def cancel_recount(
    db: Session,
    *,
    actor_id: uuid.UUID,
    recount_id: uuid.UUID,
    reason: str,
    expected_version: int,
) -> InventoryRecount:
    """Cancela un reconteo abierto y su sesion resultante (sin borrar historia)."""
    preview = db.get(InventoryRecount, recount_id)
    if preview is None:
        raise RecountError("Reconteo no encontrado", 404, "RECOUNT_NOT_FOUND")
    campaign = campaign_service.lock_campaign(db, preview.inventory_campaign_id)
    db.refresh(preview)
    session = (
        db.execute(
            select(InventoryCountSession)
            .where(InventoryCountSession.id == preview.resulting_session_id)
            .with_for_update()
        ).scalar_one_or_none()
        if preview.resulting_session_id is not None
        else None
    )
    recount = lock_recount(db, recount_id)
    check_recount_version(recount, expected_version)
    if not reason or not reason.strip():
        raise RecountError("El motivo es obligatorio", 422)
    if recount.status not in OPEN_RECOUNT_STATUSES:
        raise RecountError("El reconteo no esta abierto", 409, "RECOUNT_NOT_OPEN")

    session_cancelled = False
    if session is not None and session.status is SessionStatus.IN_PROGRESS:
        session.status = SessionStatus.CANCELLED
        session.version += 1
        session_cancelled = True

    assignment = db.execute(
        select(InventoryAssignment)
        .where(
            InventoryAssignment.inventory_campaign_id == campaign.id,
            InventoryAssignment.user_id == recount.assigned_user_id,
            InventoryAssignment.status == AssignmentStatus.ACTIVE,
            InventoryAssignment.revoked_at.is_(None),
        )
        .limit(1)
    ).scalar_one_or_none()
    if assignment is not None:
        assignment.status = AssignmentStatus.REVOKED
        assignment.revoked_at = _now()

    recount.status = RecountStatus.CANCELLED
    recount.cancelled_at = _now()
    recount.cancelled_by = actor_id
    recount.cancel_reason = reason.strip()
    recount.expected_version += 1

    # §29: si hay una sesion SUBMITTED previa, la campana vuelve a SUBMITTED.
    # Nunca se resucitan campanas EXPIRED/CLOSED/CANCELLED.
    has_submitted = (
        db.execute(
            select(func.count())
            .select_from(InventoryCountSession)
            .where(
                InventoryCountSession.inventory_campaign_id == campaign.id,
                InventoryCountSession.status == SessionStatus.SUBMITTED,
            )
        ).scalar_one()
        > 0
    )
    if campaign.status in (CampaignStatus.RECOUNT, CampaignStatus.UNDER_REVIEW) and has_submitted:
        campaign.status = CampaignStatus.SUBMITTED
        campaign.version += 1

    audit_service.record(
        db,
        action=audit_service.RECOUNT_CANCELLED,
        actor_user_id=actor_id,
        entity_type="inventory_recount",
        entity_id=recount.id,
        metadata={
            "campaign_id": str(campaign.id),
            "session_cancelled": session_cancelled,
            "expected_version": recount.expected_version,
            "campaign_version": campaign.version,
        },
    )
    db.commit()
    return recount


def reassign_recount(
    db: Session,
    *,
    actor_id: uuid.UUID,
    recount_id: uuid.UUID,
    new_user_id: uuid.UUID,
    expected_version: int,
) -> InventoryRecount:
    """Reasigna el reconteo (antes o despues de iniciar la sesion)."""
    preview = db.get(InventoryRecount, recount_id)
    if preview is None:
        raise RecountError("Reconteo no encontrado", 404, "RECOUNT_NOT_FOUND")
    campaign = campaign_service.lock_campaign(db, preview.inventory_campaign_id)
    db.refresh(preview)
    current = campaign_service.active_assignment(db, campaign.id)
    session_ids = {preview.resulting_session_id} if preview.resulting_session_id else set()
    if current is not None:
        session_ids.update(
            db.execute(
                select(InventoryCountSession.id)
                .where(
                    InventoryCountSession.assignment_id == current.id,
                    InventoryCountSession.status == SessionStatus.IN_PROGRESS,
                )
                .order_by(InventoryCountSession.id)
            ).scalars()
        )
    locked_sessions = (
        {
            session.id: session
            for session in db.execute(
                select(InventoryCountSession)
                .where(InventoryCountSession.id.in_(session_ids))
                .order_by(InventoryCountSession.id)
                .with_for_update()
            ).scalars()
        }
        if session_ids
        else {}
    )
    recount = lock_recount(db, recount_id)
    check_recount_version(recount, expected_version)
    if recount.status not in OPEN_RECOUNT_STATUSES:
        raise RecountError("El reconteo no esta abierto", 409, "RECOUNT_NOT_OPEN")
    if new_user_id == recount.assigned_user_id:
        db.commit()
        return recount
    _validate_assignee(db, new_user_id)

    previous_user_id = recount.assigned_user_id
    previous_session_id = recount.resulting_session_id

    if recount.status is RecountStatus.IN_PROGRESS:
        # §32: la sesion ya iniciada se CANCELA (datos preservados) y el
        # reconteo vuelve a ASSIGNED para iniciar una sesion nueva.
        if previous_session_id is not None:
            session = locked_sessions.get(previous_session_id)
            if session is not None and session.status is SessionStatus.IN_PROGRESS:
                session.status = SessionStatus.CANCELLED
                session.version += 1
        recount.resulting_session_id = None

    if current is not None:
        for session in locked_sessions.values():
            if (
                session.assignment_id != current.id
                or session.status is not SessionStatus.IN_PROGRESS
            ):
                continue
            session.status = SessionStatus.CANCELLED
            session.version += 1
        current.status = AssignmentStatus.REVOKED
        current.revoked_at = _now()

    assignment = InventoryAssignment(
        inventory_campaign_id=campaign.id,
        user_id=new_user_id,
        assigned_by=actor_id,
        status=AssignmentStatus.ACTIVE,
    )
    db.add(assignment)
    db.flush()

    recount.assigned_user_id = new_user_id
    recount.status = RecountStatus.ASSIGNED
    recount.expected_version += 1
    audit_service.record(
        db,
        action=audit_service.RECOUNT_REASSIGNED,
        actor_user_id=actor_id,
        entity_type="inventory_recount",
        entity_id=recount.id,
        metadata={
            "campaign_id": str(campaign.id),
            "previous_user_id": str(previous_user_id),
            "new_user_id": str(new_user_id),
            "previous_session_id": _ref(previous_session_id),
            "assignment_id": str(assignment.id),
            "expected_version": recount.expected_version,
        },
    )
    db.commit()
    return recount


def get_recount(db: Session, recount_id: uuid.UUID) -> InventoryRecount:
    recount = db.get(InventoryRecount, recount_id)
    if recount is None:
        raise RecountError("Reconteo no encontrado", 404, "RECOUNT_NOT_FOUND")
    return recount


def list_campaign_recounts(
    db: Session, campaign_id: uuid.UUID, *, limit: int = 100, offset: int = 0
) -> list[InventoryRecount]:
    return list(
        db.execute(
            select(InventoryRecount)
            .where(InventoryRecount.inventory_campaign_id == campaign_id)
            .order_by(InventoryRecount.created_at.desc(), InventoryRecount.id)
            .offset(offset)
            .limit(limit)
        ).scalars()
    )


def list_my_recounts(
    db: Session, user_id: uuid.UUID, *, limit: int = 100, offset: int = 0
) -> list[InventoryRecount]:
    return list(
        db.execute(
            select(InventoryRecount)
            .where(InventoryRecount.assigned_user_id == user_id)
            .order_by(InventoryRecount.created_at.desc(), InventoryRecount.id)
            .offset(offset)
            .limit(limit)
        ).scalars()
    )


def admin_payload(db: Session, recount: InventoryRecount) -> dict[str, object]:
    """Serializacion administrativa (inventory.monitor): incluye source_session_id."""
    return {
        "id": str(recount.id),
        "campaign_id": str(recount.inventory_campaign_id),
        "status": recount.status.value,
        "requested_by": _ref(recount.requested_by),
        "assigned_user_id": str(recount.assigned_user_id),
        "source_session_id": _ref(recount.source_session_id),
        "resulting_session_id": _ref(recount.resulting_session_id),
        "reason": recount.reason,
        "created_at": _iso(recount.created_at),
        "started_at": _iso(recount.started_at),
        "completed_at": _iso(recount.completed_at),
        "cancelled_at": _iso(recount.cancelled_at),
        "cancelled_by": _ref(recount.cancelled_by),
        "cancel_reason": recount.cancel_reason,
        "expected_version": recount.expected_version,
    }


def owner_payload(db: Session, recount: InventoryRecount) -> dict[str, object]:
    """Serializacion BLIND-SAFE para el operario asignado (§17).

    NUNCA incluye source_session_id, cantidades esperadas, costos, precios,
    snapshot hash ni datos de la sesion de origen.
    """
    campaign = campaign_service.get_campaign(db, recount.inventory_campaign_id)
    return {
        "id": str(recount.id),
        "campaign": campaign_service.campaign_summary(campaign),
        "status": recount.status.value,
        "reason": recount.reason,
        "created_at": _iso(recount.created_at),
        "started_at": _iso(recount.started_at),
        "assignment_id": _ref(
            _current_assignment_id(db, campaign.id, recount.assigned_user_id)
        ),
        "resulting_session_id": _ref(recount.resulting_session_id),
    }
