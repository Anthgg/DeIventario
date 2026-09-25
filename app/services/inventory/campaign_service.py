"""Servicio de campanas de inventario (ciclo de vida F004)."""

from __future__ import annotations

import datetime as dt
import secrets
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    InventoryAssignment,
    InventoryCampaign,
    InventoryCountSession,
    InventoryRecount,
    Location,
)
from app.models.enums import (
    AssignmentStatus,
    CampaignStatus,
    RecountStatus,
    SessionStatus,
    StockScope,
)
from app.services.auth import audit_service
from app.services.inventory import snapshot_service


class CampaignError(Exception):
    def __init__(
        self, message: str, status_code: int = 400, code: str | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def require_not_closed(campaign: InventoryCampaign) -> None:
    """F010: una campana CLOSED es inmutable para todo cambio posterior."""
    if campaign.status is CampaignStatus.CLOSED:
        raise CampaignError("La campana esta cerrada", 409, "CAMPAIGN_CLOSED")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def generate_code(db: Session) -> str:
    stamp = _now().strftime("%Y%m%d")
    for _ in range(10):
        code = f"INV-{stamp}-{secrets.token_hex(3).upper()}"
        existing = db.execute(
            select(InventoryCampaign.id).where(InventoryCampaign.code == code).limit(1)
        ).scalar_one_or_none()
        if existing is None:
            return code
    raise CampaignError("No se pudo generar un codigo unico", 500)


def get_campaign(db: Session, campaign_id: uuid.UUID) -> InventoryCampaign:
    campaign = db.get(InventoryCampaign, campaign_id)
    if campaign is None:
        raise CampaignError("Campana no encontrada", 404)
    return campaign


def lock_campaign(db: Session, campaign_id: uuid.UUID) -> InventoryCampaign:
    campaign = db.execute(
        select(InventoryCampaign).where(InventoryCampaign.id == campaign_id).with_for_update()
    ).scalar_one_or_none()
    if campaign is None:
        raise CampaignError("Campana no encontrada", 404)
    return campaign


def check_version(campaign: InventoryCampaign, expected_version: int | None) -> None:
    if expected_version is not None and campaign.version != expected_version:
        raise CampaignError("Conflicto de version", 409)


def active_assignment(db: Session, campaign_id: uuid.UUID) -> InventoryAssignment | None:
    return db.execute(
        select(InventoryAssignment)
        .where(
            InventoryAssignment.inventory_campaign_id == campaign_id,
            InventoryAssignment.status == AssignmentStatus.ACTIVE,
            InventoryAssignment.revoked_at.is_(None),
        )
        .limit(1)
    ).scalar_one_or_none()


def expire_campaign_if_due(db: Session, campaign: InventoryCampaign) -> bool:
    """Marca EXPIRED si la campana vigente ya supero su deadline (no hace commit)."""
    if (
        campaign.status
        in (CampaignStatus.ASSIGNED, CampaignStatus.IN_PROGRESS, CampaignStatus.RECOUNT)
        and campaign.deadline_at is not None
        and _now() >= campaign.deadline_at
    ):
        campaign.status = CampaignStatus.EXPIRED
        campaign.version += 1
        audit_service.record(
            db,
            action=audit_service.CAMPAIGN_EXPIRED,
            entity_type="inventory_campaign",
            entity_id=campaign.id,
            metadata={"code": campaign.code, "version": campaign.version},
        )
        return True
    return False


def refresh_expiry(db: Session, campaign: InventoryCampaign) -> InventoryCampaign:
    """Persiste la expiracion si corresponde (autocuracion sin scheduler)."""
    if expire_campaign_if_due(db, campaign):
        db.commit()
    return campaign


def create_campaign(
    db: Session,
    *,
    actor_id: uuid.UUID,
    name: str,
    location_id: uuid.UUID | None = None,
    source_import_batch_id: uuid.UUID | None = None,
    deadline_at: dt.datetime | None = None,
) -> InventoryCampaign:
    if not name or not name.strip():
        raise CampaignError("El nombre es obligatorio", 422)
    if deadline_at is not None and deadline_at.tzinfo is None:
        raise CampaignError("deadline_at debe incluir zona horaria", 422)
    if location_id is not None and db.get(Location, location_id) is None:
        raise CampaignError("Ubicacion no encontrada", 404)
    if source_import_batch_id is not None:
        snapshot_service.get_source_batch(db, source_import_batch_id)
    campaign = InventoryCampaign(
        code=generate_code(db),
        name=name.strip(),
        status=CampaignStatus.DRAFT,
        version=1,
        location_id=location_id,
        source_import_batch_id=source_import_batch_id,
        deadline_at=deadline_at,
        created_by=actor_id,
    )
    db.add(campaign)
    db.flush()
    audit_service.record(
        db,
        action=audit_service.CAMPAIGN_CREATED,
        actor_user_id=actor_id,
        entity_type="inventory_campaign",
        entity_id=campaign.id,
        metadata={"code": campaign.code, "version": campaign.version},
    )
    db.commit()
    return campaign


def update_campaign(
    db: Session,
    *,
    actor_id: uuid.UUID,
    campaign_id: uuid.UUID,
    expected_version: int | None,
    name: str | None = None,
    location_id: uuid.UUID | None = None,
    source_import_batch_id: uuid.UUID | None = None,
    deadline_at: dt.datetime | None = None,
) -> InventoryCampaign:
    campaign = lock_campaign(db, campaign_id)
    check_version(campaign, expected_version)
    if campaign.snapshot_frozen_at is not None and (
        location_id is not None or source_import_batch_id is not None or deadline_at is not None
    ):
        raise CampaignError(
            "La campana ya tiene snapshot congelado: no se puede cambiar "
            "fuente, ubicacion ni deadline",
            409,
        )
    if name is not None:
        if not name.strip():
            raise CampaignError("El nombre no puede quedar vacio", 422)
        campaign.name = name.strip()
    if location_id is not None:
        if db.get(Location, location_id) is None:
            raise CampaignError("Ubicacion no encontrada", 404)
        campaign.location_id = location_id
    if source_import_batch_id is not None:
        snapshot_service.get_source_batch(db, source_import_batch_id)
        campaign.source_import_batch_id = source_import_batch_id
    if deadline_at is not None:
        if deadline_at.tzinfo is None:
            raise CampaignError("deadline_at debe incluir zona horaria", 422)
        campaign.deadline_at = deadline_at
    campaign.version += 1
    audit_service.record(
        db,
        action=audit_service.CAMPAIGN_UPDATED,
        actor_user_id=actor_id,
        entity_type="inventory_campaign",
        entity_id=campaign.id,
        metadata={"code": campaign.code, "version": campaign.version},
    )
    db.commit()
    return campaign


def list_campaigns(
    db: Session,
    *,
    status: CampaignStatus | None = None,
    location_id: uuid.UUID | None = None,
    assigned_user_id: uuid.UUID | None = None,
    created_from: dt.datetime | None = None,
    created_to: dt.datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[InventoryCampaign], int]:
    query = select(InventoryCampaign)
    if status is not None:
        query = query.where(InventoryCampaign.status == status)
    if location_id is not None:
        query = query.where(InventoryCampaign.location_id == location_id)
    if created_from is not None:
        query = query.where(InventoryCampaign.created_at >= created_from)
    if created_to is not None:
        query = query.where(InventoryCampaign.created_at <= created_to)
    if assigned_user_id is not None:
        query = query.join(
            InventoryAssignment,
            InventoryAssignment.inventory_campaign_id == InventoryCampaign.id,
        ).where(InventoryAssignment.user_id == assigned_user_id)
    total = db.execute(select(func.count()).select_from(query.subquery())).scalar_one()
    rows = list(
        db.execute(
            query.order_by(InventoryCampaign.created_at.desc()).limit(limit).offset(offset)
        ).scalars()
    )
    return rows, total


def start_campaign(
    db: Session,
    *,
    campaign_id: uuid.UUID,
    expected_version: int | None,
    confirm_aggregate_source: bool = False,
) -> tuple[InventoryCampaign, bool]:
    """Congela el snapshot y arranca la campana. Devuelve (campana, already_started)."""
    from app.auth.permissions import INVENTORY_COUNT
    from app.models import User
    from app.services.auth import rbac_service

    campaign = lock_campaign(db, campaign_id)
    # Idempotencia (seccion 32): si ya esta IN_PROGRESS con snapshot congelado,
    # no se duplica nada y se responde already_started incluso con version vieja.
    if (
        campaign.status is CampaignStatus.IN_PROGRESS
        and campaign.snapshot_frozen_at is not None
    ):
        db.commit()
        return campaign, True
    check_version(campaign, expected_version)
    if campaign.deadline_at is None:
        raise CampaignError("La campana no tiene deadline", 409)
    if campaign.deadline_at <= _now():
        expire_campaign_if_due(db, campaign)
        db.commit()
        raise CampaignError("La campana expiro", 409)
    if campaign.status not in (CampaignStatus.DRAFT, CampaignStatus.ASSIGNED):
        raise CampaignError("La campana no puede iniciar en su estado actual", 409)
    if campaign.source_import_batch_id is None:
        raise CampaignError("La campana no tiene lote de origen", 409)
    batch = snapshot_service.get_source_batch(db, campaign.source_import_batch_id)
    scope = snapshot_service.detect_scope(db, batch.id)
    if (
        campaign.location_id is not None
        and scope is StockScope.AGGREGATE
        and not confirm_aggregate_source
    ):
        raise CampaignError(
            "El origen es AGGREGATE y la campana tiene ubicacion fisica: "
            "confirme con confirm_aggregate_source=true",
            409,
        )
    snapshot_service.assert_source_consistent(db, campaign)
    assignment = active_assignment(db, campaign.id)
    if assignment is None:
        raise CampaignError("La campana no tiene responsable activo", 409)
    responsible = db.get(User, assignment.user_id)
    if (
        responsible is None
        or not responsible.is_active
        or INVENTORY_COUNT not in rbac_service.user_permissions(db, responsible.id)
    ):
        raise CampaignError("El responsable asignado no es valido", 409)

    _count, sha = snapshot_service.build_snapshot(db, campaign)
    campaign.snapshot_frozen_at = _now()
    campaign.snapshot_sha256 = sha
    campaign.starts_at = _now()
    campaign.status = CampaignStatus.IN_PROGRESS
    campaign.version += 1
    audit_service.record(
        db,
        action=audit_service.SNAPSHOT_FROZEN,
        actor_user_id=None,
        entity_type="inventory_campaign",
        entity_id=campaign.id,
        metadata={"code": campaign.code, "snapshot_sha256": sha},
    )
    audit_service.record(
        db,
        action=audit_service.CAMPAIGN_STARTED,
        entity_type="inventory_campaign",
        entity_id=campaign.id,
        metadata={"code": campaign.code, "version": campaign.version},
    )
    db.commit()
    return campaign, False


def reopen_campaign(
    db: Session,
    *,
    actor_id: uuid.UUID,
    campaign_id: uuid.UUID,
    expected_version: int | None,
    reason: str,
    new_deadline_at: dt.datetime,
) -> InventoryCampaign:
    campaign = lock_campaign(db, campaign_id)
    check_version(campaign, expected_version)
    if not reason or not reason.strip():
        raise CampaignError("El motivo es obligatorio", 422)
    if new_deadline_at.tzinfo is None:
        raise CampaignError("new_deadline_at debe incluir zona horaria", 422)
    if new_deadline_at <= _now():
        raise CampaignError("new_deadline_at debe ser futuro", 422)
    if campaign.status not in (CampaignStatus.EXPIRED, CampaignStatus.CLOSED):
        raise CampaignError("La campana no puede reabrirse en su estado actual", 409)
    # F007: una sesion de reconteo caducada NO revive con el reopen. Se
    # cancela (sin borrar historia) y el reconteo vuelve a ASSIGNED para que
    # un administrador reasigne o reinicie de forma explicita.
    stale_recount_ids = list(
        db.execute(
            select(InventoryRecount.id)
            .where(
                InventoryRecount.inventory_campaign_id == campaign_id,
                InventoryRecount.status == RecountStatus.IN_PROGRESS,
            )
            .order_by(InventoryRecount.id)
        ).scalars()
    )
    stale_session_ids = list(
        db.execute(
            select(InventoryRecount.resulting_session_id)
            .where(
                InventoryRecount.id.in_(stale_recount_ids),
                InventoryRecount.resulting_session_id.is_not(None),
            )
            .order_by(InventoryRecount.resulting_session_id)
        ).scalars()
    ) if stale_recount_ids else []
    # A stale recount may cancel its session. Lock sessions before recount rows,
    # matching submit_session's campaign -> session -> recount order.
    stale_sessions = {
        session.id: session
        for session in (
            db.execute(
                select(InventoryCountSession)
                .where(InventoryCountSession.id.in_(stale_session_ids))
                .order_by(InventoryCountSession.id)
                .with_for_update()
            ).scalars()
            if stale_session_ids
            else []
        )
    }
    stale_recounts = (
        list(
            db.execute(
                select(InventoryRecount)
                .where(InventoryRecount.id.in_(stale_recount_ids))
                .order_by(InventoryRecount.id)
                .with_for_update()
            ).scalars()
        )
        if stale_recount_ids
        else []
    )
    for recount in stale_recounts:
        if recount.resulting_session_id is not None:
            stale_session = stale_sessions.get(recount.resulting_session_id)
            if (
                stale_session is not None
                and stale_session.status is SessionStatus.IN_PROGRESS
            ):
                stale_session.status = SessionStatus.CANCELLED
                stale_session.version += 1
        recount.resulting_session_id = None
        recount.status = RecountStatus.ASSIGNED
        recount.expected_version += 1
    if stale_recounts:
        db.flush()
    open_recount = db.execute(
        select(InventoryRecount.id)
        .where(
            InventoryRecount.inventory_campaign_id == campaign_id,
            InventoryRecount.status.in_(
                (RecountStatus.REQUESTED, RecountStatus.ASSIGNED, RecountStatus.IN_PROGRESS)
            ),
        )
        .limit(1)
    ).scalar_one_or_none()
    campaign.status = (
        CampaignStatus.RECOUNT if open_recount is not None else CampaignStatus.IN_PROGRESS
    )
    campaign.deadline_at = new_deadline_at
    campaign.reopened_at = _now()
    campaign.reopened_by = actor_id
    campaign.reopen_reason = reason.strip()
    campaign.closed_at = None
    campaign.version += 1
    audit_service.record(
        db,
        action=audit_service.CAMPAIGN_REOPENED,
        actor_user_id=actor_id,
        entity_type="inventory_campaign",
        entity_id=campaign.id,
        metadata={"code": campaign.code, "version": campaign.version},
    )
    db.commit()
    return campaign


def cancel_campaign(
    db: Session,
    *,
    actor_id: uuid.UUID,
    campaign_id: uuid.UUID,
    expected_version: int | None,
    reason: str,
) -> InventoryCampaign:
    campaign = lock_campaign(db, campaign_id)
    check_version(campaign, expected_version)
    if not reason or not reason.strip():
        raise CampaignError("El motivo es obligatorio", 422)
    if campaign.status not in (
        CampaignStatus.DRAFT,
        CampaignStatus.ASSIGNED,
        CampaignStatus.IN_PROGRESS,
    ):
        raise CampaignError("La campana no puede cancelarse en su estado actual", 409)
    campaign.status = CampaignStatus.CANCELLED
    campaign.version += 1
    audit_service.record(
        db,
        action=audit_service.CAMPAIGN_CANCELLED,
        actor_user_id=actor_id,
        entity_type="inventory_campaign",
        entity_id=campaign.id,
        metadata={"code": campaign.code, "version": campaign.version, "reason": reason.strip()},
    )
    db.commit()
    return campaign


def campaign_summary(campaign: InventoryCampaign) -> dict[str, object]:
    """Serializacion BLIND-SAFE para listados (sin expected/costos/precios)."""
    return {
        "id": str(campaign.id),
        "code": campaign.code,
        "name": campaign.name,
        "status": campaign.status.value,
        "location_id": str(campaign.location_id) if campaign.location_id else None,
        "deadline_at": _iso(campaign.deadline_at),
        "created_at": _iso(campaign.created_at),
        "version": campaign.version,
    }


def campaign_detail(campaign: InventoryCampaign) -> dict[str, object]:
    """Serializacion BLIND-SAFE del detalle (sin expected/costos/precios)."""
    data = campaign_summary(campaign)
    data["starts_at"] = _iso(campaign.starts_at)
    return data
