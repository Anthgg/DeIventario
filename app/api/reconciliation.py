"""Endpoints de conciliacion F008 (Odoo vs conteos, seleccion de conteo oficial).

Requieren inventory.reconcile salvo approve (inventory.approve). Ningun
endpoint es accesible solo con inventory.count (blind F008).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.auth.dependencies import CurrentUser, Database, require_permission
from app.auth.permissions import INVENTORY_APPROVE, INVENTORY_RECONCILE
from app.services.inventory.campaign_service import CampaignError
from app.services.reconciliation import approval_service, reconciliation_service
from app.services.reconciliation.errors import ReconciliationError

router = APIRouter(prefix="/inventory", tags=["inventory-reconciliation"])

RequireReconcile = Annotated[CurrentUser, Depends(require_permission(INVENTORY_RECONCILE))]
RequireApprove = Annotated[CurrentUser, Depends(require_permission(INVENTORY_APPROVE))]


class PrepareRequest(BaseModel):
    expected_campaign_version: int
    refresh: bool = False


class SelectSessionRequest(BaseModel):
    session_id: uuid.UUID
    expected_campaign_version: int


class OverrideRequest(BaseModel):
    selected_session_id: uuid.UUID
    expected_version: int
    reason: str | None = None
    observation: str | None = None


class ApproveRequest(BaseModel):
    expected_campaign_version: int


def _handle(exc: Exception) -> HTTPException:
    if isinstance(exc, ReconciliationError):
        detail: dict[str, Any] = {"message": str(exc)}
        if exc.code:
            detail["error"] = exc.code
        detail.update(exc.payload)
        return HTTPException(status_code=exc.status_code, detail=detail)
    if isinstance(exc, CampaignError):
        campaign_detail: dict[str, Any] = {"message": str(exc)}
        if exc.code:
            campaign_detail["error"] = exc.code
        return HTTPException(status_code=exc.status_code, detail=campaign_detail)
    return HTTPException(status_code=400, detail={"message": "Error de conciliacion"})


@router.get("/campaigns/{campaign_id}/reconciliation/preview")
def reconciliation_preview(
    campaign_id: uuid.UUID, current: RequireReconcile, db: Database
) -> dict[str, Any]:
    try:
        return reconciliation_service.preview(db, campaign_id)
    except (ReconciliationError, CampaignError) as exc:
        raise _handle(exc) from exc


@router.post("/campaigns/{campaign_id}/reconciliation/prepare")
def reconciliation_prepare(
    campaign_id: uuid.UUID, payload: PrepareRequest, current: RequireReconcile, db: Database
) -> dict[str, Any]:
    try:
        return reconciliation_service.prepare(
            db,
            actor_id=current.id,
            campaign_id=campaign_id,
            expected_version=payload.expected_campaign_version,
            refresh=payload.refresh,
        )
    except (ReconciliationError, CampaignError) as exc:
        raise _handle(exc) from exc


@router.get("/campaigns/{campaign_id}/reconciliation")
def reconciliation_list(
    campaign_id: uuid.UUID,
    current: RequireReconcile,
    db: Database,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    try:
        return reconciliation_service.list_reconciliation(
            db, campaign_id, limit=limit, offset=offset
        )
    except (ReconciliationError, CampaignError) as exc:
        raise _handle(exc) from exc


@router.get("/campaigns/{campaign_id}/reconciliation/sessions")
def reconciliation_sessions(
    campaign_id: uuid.UUID, current: RequireReconcile, db: Database
) -> list[dict[str, Any]]:
    try:
        return reconciliation_service.list_sessions(db, campaign_id)
    except (ReconciliationError, CampaignError) as exc:
        raise _handle(exc) from exc


@router.post("/campaigns/{campaign_id}/reconciliation/select-session")
def reconciliation_select_session(
    campaign_id: uuid.UUID,
    payload: SelectSessionRequest,
    current: RequireReconcile,
    db: Database,
) -> dict[str, Any]:
    try:
        return reconciliation_service.select_default_session(
            db,
            actor_id=current.id,
            campaign_id=campaign_id,
            session_id=payload.session_id,
            expected_version=payload.expected_campaign_version,
        )
    except (ReconciliationError, CampaignError) as exc:
        raise _handle(exc) from exc


@router.patch("/campaigns/{campaign_id}/reconciliation/products/{product_id}")
def reconciliation_override_product(
    campaign_id: uuid.UUID,
    product_id: uuid.UUID,
    payload: OverrideRequest,
    current: RequireReconcile,
    db: Database,
) -> dict[str, Any]:
    try:
        return reconciliation_service.override_product(
            db,
            actor_id=current.id,
            campaign_id=campaign_id,
            product_id=product_id,
            selected_session_id=payload.selected_session_id,
            expected_version=payload.expected_version,
            reason=payload.reason,
            observation=payload.observation,
        )
    except (ReconciliationError, CampaignError) as exc:
        raise _handle(exc) from exc


@router.post("/campaigns/{campaign_id}/reconciliation/approve")
def reconciliation_approve(
    campaign_id: uuid.UUID,
    payload: ApproveRequest,
    current: RequireApprove,
    db: Database,
) -> dict[str, Any]:
    try:
        return approval_service.approve(
            db,
            actor_id=current.id,
            campaign_id=campaign_id,
            expected_version=payload.expected_campaign_version,
        )
    except (ReconciliationError, CampaignError) as exc:
        raise _handle(exc) from exc
