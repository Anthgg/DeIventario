"""Endpoints de valorizacion economica F009 (conciliacion aprobada -> impacto).

preview y calculate requieren inventory.approve; lectura requiere
inventory.reconcile. Ningun endpoint es accesible solo con inventory.count
(blind F009). No se crean permisos nuevos.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.dependencies import CurrentUser, Database, require_permission
from app.auth.permissions import INVENTORY_APPROVE, INVENTORY_RECONCILE
from app.services.inventory.campaign_service import CampaignError
from app.services.valuation import valuation_service
from app.services.valuation.errors import ValuationError

router = APIRouter(prefix="/inventory", tags=["inventory-valuation"])

RequireReconcile = Annotated[CurrentUser, Depends(require_permission(INVENTORY_RECONCILE))]
RequireApprove = Annotated[CurrentUser, Depends(require_permission(INVENTORY_APPROVE))]


class CalculateRequest(BaseModel):
    expected_campaign_version: int


def _handle(exc: Exception) -> HTTPException:
    if isinstance(exc, ValuationError):
        detail: dict[str, Any] = {"message": str(exc)}
        if exc.code:
            detail["error"] = exc.code
        detail.update(exc.payload)
        return HTTPException(status_code=exc.status_code, detail=detail)
    if isinstance(exc, CampaignError):
        return HTTPException(
            status_code=exc.status_code, detail={"message": str(exc)}
        )
    return HTTPException(status_code=400, detail={"message": "Error de valorizacion"})


@router.get("/campaigns/{campaign_id}/valuation/preview")
def valuation_preview(
    campaign_id: uuid.UUID, current: RequireApprove, db: Database
) -> dict[str, Any]:
    try:
        return valuation_service.preview(db, campaign_id)
    except (ValuationError, CampaignError) as exc:
        raise _handle(exc) from exc


@router.post("/campaigns/{campaign_id}/valuation/calculate")
def valuation_calculate(
    campaign_id: uuid.UUID,
    current: RequireApprove,
    payload: CalculateRequest,
    db: Database,
) -> dict[str, Any]:
    try:
        return valuation_service.calculate(
            db,
            actor_id=current.id,
            campaign_id=campaign_id,
            expected_version=payload.expected_campaign_version,
        )
    except (ValuationError, CampaignError) as exc:
        raise _handle(exc) from exc


@router.get("/campaigns/{campaign_id}/valuation")
def valuation_read(
    campaign_id: uuid.UUID, current: RequireReconcile, db: Database
) -> dict[str, Any]:
    try:
        return valuation_service.read_valuation(db, campaign_id)
    except (ValuationError, CampaignError) as exc:
        raise _handle(exc) from exc
