"""Endpoints administrativos de excepciones F006: dano, extras, unknowns.

Blind-safe: ninguno expone expected, diferencia ni costos. Acceso por
permisos (damage.review / inventory.reconcile), nunca solo con inventory.count.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.dependencies import (
    CurrentUser,
    Database,
    require_any_permission,
    require_permission,
)
from app.auth.permissions import DAMAGE_REPORT, DAMAGE_REVIEW, INVENTORY_RECONCILE
from app.core.config import get_settings
from app.models import InventoryCampaign, InventoryCountSession, InventoryDamage
from app.models.enums import SessionStatus
from app.services.auth import audit_service
from app.services.inventory import exception_service
from app.services.inventory.evidence_service import (
    EvidenceError,
    evidence_relative_path,
    media_type_for,
    resolve_evidence_file,
    validate_upload,
    write_evidence_file,
)
from app.services.inventory.exception_service import ResolveError

router = APIRouter(prefix="/inventory", tags=["inventory-exceptions"])

RequireReconcile = Annotated[CurrentUser, Depends(require_permission(INVENTORY_RECONCILE))]
RequireDamageReview = Annotated[CurrentUser, Depends(require_permission(DAMAGE_REVIEW))]
RequireDamageEvidence = Annotated[
    CurrentUser, Depends(require_any_permission(DAMAGE_REPORT, DAMAGE_REVIEW))
]

_MAX_READ = 500


class ResolveRequest(BaseModel):
    product_id: uuid.UUID


def _campaign_or_404(db: Session, campaign_id: uuid.UUID) -> InventoryCampaign:
    campaign = db.get(InventoryCampaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail={"message": "Campana no encontrada"})
    return campaign


def _damage_or_404(db: Session, damage_id: uuid.UUID) -> InventoryDamage:
    damage = db.get(InventoryDamage, damage_id)
    if damage is None:
        raise HTTPException(status_code=404, detail={"message": "Registro de dano no encontrado"})
    return damage


def _session_of(db: Session, damage: InventoryDamage) -> InventoryCountSession:
    session = db.get(InventoryCountSession, damage.session_id)
    if session is None:  # pragma: no cover - FK RESTRICT
        raise HTTPException(status_code=404, detail={"message": "Sesion no encontrada"})
    return session


def _can_review(current: CurrentUser) -> bool:
    return current.has_permission(DAMAGE_REVIEW)


# --------------------------- lista de dannos ---------------------------------


@router.get("/campaigns/{campaign_id}/damages")
def list_campaign_damages(
    campaign_id: uuid.UUID,
    current: RequireDamageReview,
    db: Database,
    limit: Annotated[int, Query(ge=1, le=_MAX_READ)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    _campaign_or_404(db, campaign_id)
    items, total = exception_service.campaign_damages(
        db, campaign_id, offset=offset, limit=limit
    )
    return {"total": total, "limit": limit, "offset": offset, "items": items}


# ------------------------------- evidencia -----------------------------------


@router.post("/damages/{damage_id}/evidence")
async def upload_damage_evidence(
    damage_id: uuid.UUID,
    current: RequireDamageEvidence,
    db: Database,
    file: Annotated[UploadFile, File()],
) -> dict[str, Any]:
    damage = _damage_or_404(db, damage_id)
    session = _session_of(db, damage)
    is_owner = session.user_id == current.id and session.status is SessionStatus.IN_PROGRESS
    if not (_can_review(current) or is_owner):
        raise HTTPException(status_code=403, detail={"message": "Sin permiso"})

    if damage.evidence_path is not None:
        raise HTTPException(
            status_code=409,
            detail={"message": "La evidencia ya existe", "error": "EVIDENCE_ALREADY_EXISTS"},
        )

    max_bytes = get_settings().MAX_EVIDENCE_MB * 1024 * 1024
    data = await file.read(max_bytes + 1)
    relative_path: str | None = None
    try:
        mime = validate_upload(declared_mime=file.content_type, data=data)
        relative_path = evidence_relative_path(damage.id, mime)
        write_evidence_file(relative_path, data)
    except EvidenceError as exc:
        detail: dict[str, Any] = {"message": str(exc)}
        if exc.code:
            detail["error"] = exc.code
        raise HTTPException(status_code=exc.status_code, detail=detail) from exc

    damage.evidence_path = relative_path
    audit_service.record(
        db,
        action=audit_service.DAMAGE_EVIDENCE_ADDED,
        actor_user_id=current.id,
        entity_type="inventory_damage",
        entity_id=damage.id,
        metadata={
            "session_id": str(session.id),
            "content_type": mime,
            "bytes": len(data),
        },
    )
    db.commit()
    return {
        "damage_id": str(damage.id),
        "has_evidence": True,
        "evidence_path": damage.evidence_path,
        "content_type": mime,
    }


@router.get("/damages/{damage_id}/evidence")
def download_damage_evidence(
    damage_id: uuid.UUID, current: RequireDamageEvidence, db: Database
) -> FileResponse:
    damage = _damage_or_404(db, damage_id)
    session = _session_of(db, damage)
    if not (_can_review(current) or session.user_id == current.id):
        raise HTTPException(status_code=403, detail={"message": "Sin permiso"})
    if damage.evidence_path is None:
        raise HTTPException(
            status_code=404, detail={"message": "Sin evidencia", "error": "EVIDENCE_NOT_FOUND"}
        )
    try:
        path = resolve_evidence_file(damage.evidence_path)
    except EvidenceError as exc:
        raise HTTPException(
            status_code=exc.status_code, detail={"message": str(exc), "error": exc.code}
        ) from exc
    return FileResponse(path, media_type=media_type_for(damage.evidence_path))


# --------------------------------- extras ------------------------------------


@router.get("/campaigns/{campaign_id}/extras")
def list_campaign_extras(
    campaign_id: uuid.UUID,
    current: RequireReconcile,
    db: Database,
    include_zero: Annotated[bool, Query()] = False,
) -> list[dict[str, Any]]:
    _campaign_or_404(db, campaign_id)
    return exception_service.campaign_extras(db, campaign_id, include_zero=include_zero)


# --------------------------- codigos desconocidos ----------------------------


@router.get("/campaigns/{campaign_id}/unknown-codes")
def list_campaign_unknown_codes(
    campaign_id: uuid.UUID, current: RequireReconcile, db: Database
) -> list[dict[str, Any]]:
    _campaign_or_404(db, campaign_id)
    return exception_service.campaign_unknown_codes(db, campaign_id)


@router.post("/unknown-codes/{unknown_id}/resolve")
def resolve_unknown_code(
    unknown_id: uuid.UUID,
    payload: ResolveRequest,
    current: RequireReconcile,
    db: Database,
) -> dict[str, Any]:
    try:
        unknown, already = exception_service.resolve_unknown(
            db, unknown_id=unknown_id, product_id=payload.product_id, actor_id=current.id
        )
    except ResolveError as exc:
        detail: dict[str, Any] = {"message": str(exc)}
        if exc.code:
            detail["error"] = exc.code
        raise HTTPException(status_code=exc.status_code, detail=detail) from exc
    return {
        "id": str(unknown.id),
        "scanned_code": unknown.scanned_code,
        "resolved_product_id": str(unknown.resolved_product_id),
        "resolved_at": unknown.resolved_at.isoformat() if unknown.resolved_at else None,
        "already_resolved": already,
    }


# --------------------------- resumen de excepciones --------------------------


@router.get("/campaigns/{campaign_id}/exceptions")
def campaign_exceptions(
    campaign_id: uuid.UUID, current: RequireReconcile, db: Database
) -> dict[str, Any]:
    _campaign_or_404(db, campaign_id)
    return exception_service.exceptions_summary(db, campaign_id)
