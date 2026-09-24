"""Aprobacion de la conciliacion F008 (fase administrativa).

Congela la conciliacion: filas APPROVED + campana APPROVED. Idempotente.
NO calcula dinero (F009) y NO cierra la campana (F007 no tiene CLOSE aun).
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import InventoryCountSession, InventoryReconciliation
from app.models.enums import CampaignStatus, ReconciliationStatus, SessionStatus
from app.services.auth import audit_service
from app.services.inventory import campaign_service, recount_service
from app.services.reconciliation import comparison_service
from app.services.reconciliation.errors import ReconciliationError


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def approve(
    db: Session,
    *,
    actor_id: uuid.UUID,
    campaign_id: uuid.UUID,
    expected_version: int,
) -> dict[str, object]:
    """Aprueba toda la conciliacion de la campana (§42-§45)."""
    campaign = campaign_service.lock_campaign(db, campaign_id)
    campaign_service.require_not_closed(campaign)
    if campaign.status is CampaignStatus.APPROVED:
        db.commit()
        return {
            "already_approved": True,
            "campaign_id": str(campaign.id),
            "status": campaign.status.value,
            "campaign_version": campaign.version,
            "approved_at": _iso(campaign.approved_at),
            "products_approved": 0,
        }
    campaign_service.check_version(campaign, expected_version)
    if campaign.status is not CampaignStatus.UNDER_REVIEW:
        raise ReconciliationError(
            "La campana no esta en revision de conciliacion",
            409,
            "RECONCILIATION_NOT_READY",
        )
    fingerprint = comparison_service.source_fingerprint(db, campaign_id)
    if fingerprint != campaign.reconciliation_source_sha256:
        raise ReconciliationError(
            "La fuente de conciliacion cambio: refresque antes de aprobar",
            409,
            "RECONCILIATION_SOURCE_CHANGED",
        )
    if recount_service.open_recount_for_campaign(db, campaign_id) is not None:
        raise ReconciliationError(
            "Hay un reconteo abierto: no se puede aprobar", 409, "OPEN_RECOUNT_EXISTS"
        )
    unresolved_count = comparison_service.unresolved_unknown_count(db, campaign_id)
    if unresolved_count > 0:
        raise ReconciliationError(
            "Hay codigos desconocidos sin resolver",
            409,
            "UNRESOLVED_UNKNOWN_CODES",
            {"unresolved_unknown_count": unresolved_count},
        )

    rows = list(
        db.execute(
            select(InventoryReconciliation)
            .where(InventoryReconciliation.inventory_campaign_id == campaign_id)
            .order_by(InventoryReconciliation.product_id)
            .with_for_update()
        ).scalars()
    )
    if not rows:
        raise ReconciliationError(
            "La conciliacion no esta preparada", 409, "RECONCILIATION_NOT_PREPARED"
        )

    for row in rows:
        if row.expected_quantity < 0:
            raise ReconciliationError(
                "Hay cantidades esperadas negativas: corrija la fuente",
                409,
                "NEGATIVE_EXPECTED_QUANTITY_REQUIRES_SOURCE_CORRECTION",
                {"product_id": str(row.product_id)},
            )
    for row in rows:
        if row.selected_session_id is None or row.approved_physical_quantity is None:
            raise ReconciliationError(
                "Faltan productos con conteo oficial seleccionado",
                409,
                "RECONCILIATION_NOT_COMPLETE",
                {"product_id": str(row.product_id)},
            )
        if row.status is ReconciliationStatus.DIFFERENCE and not row.reason:
            raise ReconciliationError(
                "Toda diferencia requiere un reason justificado",
                409,
                "DIFFERENCE_REASON_REQUIRED",
                {"product_id": str(row.product_id)},
            )
        session = db.get(InventoryCountSession, row.selected_session_id)
        if (
            session is None
            or session.inventory_campaign_id != campaign_id
            or session.status is not SessionStatus.SUBMITTED
        ):
            raise ReconciliationError(
                "El conteo oficial seleccionado ya no es valido",
                409,
                "SESSION_NOT_ELIGIBLE",
                {"product_id": str(row.product_id)},
            )

    now = _now()
    for row in rows:
        row.status = ReconciliationStatus.APPROVED
        row.approved_by = actor_id
        row.approved_at = now
        row.version += 1
    campaign.status = CampaignStatus.APPROVED
    campaign.approved_by = actor_id
    campaign.approved_at = now
    campaign.version += 1
    audit_service.record(
        db,
        actor_user_id=actor_id,
        action=audit_service.RECONCILIATION_APPROVED,
        entity_type="inventory_campaign",
        entity_id=campaign.id,
        metadata={
            "campaign_id": str(campaign.id),
            "products_approved": len(rows),
            "campaign_version": campaign.version,
        },
    )
    audit_service.record(
        db,
        actor_user_id=actor_id,
        action=audit_service.CAMPAIGN_APPROVED,
        entity_type="inventory_campaign",
        entity_id=campaign.id,
        metadata={"code": campaign.code, "version": campaign.version},
    )
    db.commit()
    return {
        "already_approved": False,
        "campaign_id": str(campaign.id),
        "status": campaign.status.value,
        "campaign_version": campaign.version,
        "approved_at": _iso(campaign.approved_at),
        "products_approved": len(rows),
    }
