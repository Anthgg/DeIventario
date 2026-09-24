"""Endpoints de reconteos ciegos (F007). Blind-safe por diseno."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.dependencies import CurrentUser, Database, require_permission
from app.auth.permissions import INVENTORY_MONITOR, INVENTORY_READ, INVENTORY_RECOUNT
from app.services.inventory import campaign_service, recount_service
from app.services.inventory.campaign_service import CampaignError
from app.services.inventory.recount_service import RecountError

router = APIRouter(prefix="/inventory", tags=["inventory-recount"])

RequireRecount = Annotated[CurrentUser, Depends(require_permission(INVENTORY_RECOUNT))]
RequireRead = Annotated[CurrentUser, Depends(require_permission(INVENTORY_READ))]
RequireMonitor = Annotated[CurrentUser, Depends(require_permission(INVENTORY_MONITOR))]


class RecountCreate(BaseModel):
    assigned_user_id: uuid.UUID
    reason: str | None = None
    source_session_id: uuid.UUID | None = None
    expected_version: int | None = None


class RecountCancel(BaseModel):
    reason: str
    expected_version: int


class RecountReassign(BaseModel):
    user_id: uuid.UUID
    expected_version: int


def _handle(exc: Exception) -> HTTPException:
    if isinstance(exc, RecountError):
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
    return HTTPException(status_code=400, detail={"message": "Error de reconteo"})


@router.post("/campaigns/{campaign_id}/recounts")
def request_recount(
    campaign_id: uuid.UUID, payload: RecountCreate, current: RequireRecount, db: Database
) -> dict[str, Any]:
    try:
        recount = recount_service.request_recount(
            db,
            actor_id=current.id,
            campaign_id=campaign_id,
            assigned_user_id=payload.assigned_user_id,
            reason=payload.reason,
            source_session_id=payload.source_session_id,
            expected_version=payload.expected_version,
        )
        campaign = campaign_service.get_campaign(db, campaign_id)
    except (RecountError, CampaignError) as exc:
        raise _handle(exc) from exc
    data = recount_service.admin_payload(db, recount)
    data["campaign_version"] = campaign.version
    return data


@router.get("/campaigns/{campaign_id}/recounts")
def list_campaign_recounts(
    campaign_id: uuid.UUID, current: RequireMonitor, db: Database
) -> list[dict[str, Any]]:
    try:
        campaign_service.get_campaign(db, campaign_id)
    except CampaignError as exc:
        raise _handle(exc) from exc
    return [
        recount_service.admin_payload(db, recount)
        for recount in recount_service.list_campaign_recounts(db, campaign_id)
    ]


@router.get("/recounts/{recount_id}")
def get_recount(recount_id: uuid.UUID, current: RequireRead, db: Database) -> dict[str, Any]:
    try:
        recount = recount_service.get_recount(db, recount_id)
    except RecountError as exc:
        raise _handle(exc) from exc
    if recount.assigned_user_id == current.id:
        return recount_service.owner_payload(db, recount)
    if current.has_permission(INVENTORY_MONITOR):
        return recount_service.admin_payload(db, recount)
    raise HTTPException(status_code=403, detail={"message": "Sin permiso"})


@router.get("/my-recounts")
def my_recounts(current: RequireRead, db: Database) -> list[dict[str, Any]]:
    return [
        recount_service.owner_payload(db, recount)
        for recount in recount_service.list_my_recounts(db, current.id)
    ]


@router.post("/recounts/{recount_id}/cancel")
def cancel_recount(
    recount_id: uuid.UUID, payload: RecountCancel, current: RequireRecount, db: Database
) -> dict[str, Any]:
    try:
        recount = recount_service.cancel_recount(
            db,
            actor_id=current.id,
            recount_id=recount_id,
            reason=payload.reason,
            expected_version=payload.expected_version,
        )
    except RecountError as exc:
        raise _handle(exc) from exc
    return recount_service.admin_payload(db, recount)


@router.post("/recounts/{recount_id}/reassign")
def reassign_recount(
    recount_id: uuid.UUID, payload: RecountReassign, current: RequireRecount, db: Database
) -> dict[str, Any]:
    try:
        recount = recount_service.reassign_recount(
            db,
            actor_id=current.id,
            recount_id=recount_id,
            new_user_id=payload.user_id,
            expected_version=payload.expected_version,
        )
    except RecountError as exc:
        raise _handle(exc) from exc
    return recount_service.admin_payload(db, recount)
