"""Servicio de asignaciones: responsable unico, reasignacion e historial."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.permissions import INVENTORY_COUNT
from app.models import InventoryAssignment, User
from app.models.enums import AssignmentStatus, CampaignStatus
from app.services.auth import audit_service, rbac_service
from app.services.counting import session_service as counting_sessions
from app.services.inventory import campaign_service


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def active_assignment(db: Session, campaign_id: uuid.UUID) -> InventoryAssignment | None:
    return campaign_service.active_assignment(db, campaign_id)


def assign(
    db: Session,
    *,
    actor_id: uuid.UUID,
    campaign_id: uuid.UUID,
    user_id: uuid.UUID,
    expected_version: int | None,
) -> tuple[InventoryAssignment, bool, bool]:
    """Asigna/reasigna responsable. Devuelve (assignment, created, reassigned)."""
    campaign = campaign_service.lock_campaign(db, campaign_id)
    campaign_service.check_version(campaign, expected_version)
    if campaign_service.expire_campaign_if_due(db, campaign):
        db.commit()
        raise campaign_service.CampaignError("La campana expiro", 409)
    if campaign.status not in (
        CampaignStatus.DRAFT,
        CampaignStatus.ASSIGNED,
        CampaignStatus.IN_PROGRESS,
    ):
        raise campaign_service.CampaignError(
            "La campana no admite asignacion en su estado actual", 409
        )
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise campaign_service.CampaignError("Usuario no valido o inactivo", 400)
    if INVENTORY_COUNT not in rbac_service.user_permissions(db, user.id):
        raise campaign_service.CampaignError("El usuario no tiene permiso inventory.count", 400)

    current = campaign_service.active_assignment(db, campaign_id)
    if current is not None and current.user_id == user_id:
        db.commit()
        return current, False, False

    reassigned = current is not None
    metadata: dict[str, object] = {"new_user_id": str(user_id)}
    if current is not None:
        # F005: la sesion de conteo del responsable anterior se cancela
        # (sin borrar eventos ni totales) en la misma transaccion.
        counting_sessions.cancel_sessions_for_assignment(db, current.id)
        current.status = AssignmentStatus.REVOKED
        current.revoked_at = _now()
        db.flush()
        metadata["previous_user_id"] = str(current.user_id)
        metadata["previous_assignment_id"] = str(current.id)

    assignment = InventoryAssignment(
        inventory_campaign_id=campaign_id,
        user_id=user_id,
        assigned_by=actor_id,
        status=AssignmentStatus.ACTIVE,
    )
    db.add(assignment)
    db.flush()
    metadata["new_assignment_id"] = str(assignment.id)
    if campaign.status is CampaignStatus.DRAFT:
        campaign.status = CampaignStatus.ASSIGNED
    campaign.version += 1
    audit_service.record(
        db,
        action=(
            audit_service.CAMPAIGN_REASSIGNED if reassigned else audit_service.CAMPAIGN_ASSIGNED
        ),
        actor_user_id=actor_id,
        entity_type="inventory_campaign",
        entity_id=campaign_id,
        metadata=metadata,
    )
    db.commit()
    return assignment, True, reassigned


def unassign(
    db: Session,
    *,
    actor_id: uuid.UUID,
    campaign_id: uuid.UUID,
    expected_version: int | None,
) -> None:
    campaign = campaign_service.lock_campaign(db, campaign_id)
    campaign_service.check_version(campaign, expected_version)
    if campaign.status not in (CampaignStatus.DRAFT, CampaignStatus.ASSIGNED):
        raise campaign_service.CampaignError(
            "Solo se puede desasignar antes de IN_PROGRESS (use reintento)", 409
        )
    current = campaign_service.active_assignment(db, campaign_id)
    if current is not None:
        counting_sessions.cancel_sessions_for_assignment(db, current.id)
        current.status = AssignmentStatus.REVOKED
        current.revoked_at = _now()
        campaign.status = CampaignStatus.DRAFT
        campaign.version += 1
        audit_service.record(
            db,
            action=audit_service.CAMPAIGN_UNASSIGNED,
            actor_user_id=actor_id,
            entity_type="inventory_campaign",
            entity_id=campaign_id,
            metadata={
                "assignment_id": str(current.id),
                "user_id": str(current.user_id),
                "version": campaign.version,
            },
        )
    db.commit()


def history(
    db: Session, campaign_id: uuid.UUID, *, limit: int = 100, offset: int = 0
) -> list[InventoryAssignment]:
    return list(
        db.execute(
            select(InventoryAssignment)
            .where(InventoryAssignment.inventory_campaign_id == campaign_id)
            .order_by(InventoryAssignment.assigned_at, InventoryAssignment.id)
            .offset(offset)
            .limit(limit)
        ).scalars()
    )


def history_payload(rows: list[InventoryAssignment]) -> list[dict[str, object]]:
    return [
        {
            "assignment_id": str(row.id),
            "user_id": str(row.user_id),
            "status": row.status.value,
            "assigned_at": row.assigned_at.isoformat() if row.assigned_at else None,
            "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
        }
        for row in rows
    ]
