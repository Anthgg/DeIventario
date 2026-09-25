"""Endpoints del dominio de inventario (F004)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select

from app.auth.dependencies import CurrentUser, Database, require_permission
from app.auth.permissions import (
    INVENTORY_ASSIGN,
    INVENTORY_CLOSE,
    INVENTORY_CREATE,
    INVENTORY_EXPECTED_READ,
    INVENTORY_MONITOR,
    INVENTORY_READ,
    INVENTORY_REOPEN,
)
from app.models import InventoryAssignment, InventoryCampaign
from app.models.enums import AssignmentStatus, CampaignStatus
from app.services.inventory import (
    assignment_service,
    campaign_service,
    location_service,
    snapshot_service,
)
from app.services.inventory.campaign_service import CampaignError
from app.services.inventory.location_service import LocationError
from app.services.inventory.snapshot_service import SnapshotError

router = APIRouter(prefix="/inventory", tags=["inventory"])

ServiceErrors = (CampaignError, LocationError, SnapshotError)

RequireInventoryRead = Annotated[CurrentUser, Depends(require_permission(INVENTORY_READ))]
RequireInventoryCreate = Annotated[CurrentUser, Depends(require_permission(INVENTORY_CREATE))]
RequireInventoryAssign = Annotated[CurrentUser, Depends(require_permission(INVENTORY_ASSIGN))]
RequireInventoryMonitor = Annotated[CurrentUser, Depends(require_permission(INVENTORY_MONITOR))]
RequireInventoryClose = Annotated[CurrentUser, Depends(require_permission(INVENTORY_CLOSE))]
RequireInventoryReopen = Annotated[CurrentUser, Depends(require_permission(INVENTORY_REOPEN))]
RequireExpectedRead = Annotated[
    CurrentUser, Depends(require_permission(INVENTORY_EXPECTED_READ))
]


class LocationCreate(BaseModel):
    name: str
    code: str | None = None
    external_ref: str | None = None
    active: bool = True


class LocationUpdate(BaseModel):
    name: str | None = None
    code: str | None = None
    external_ref: str | None = None
    active: bool | None = None


class CampaignCreate(BaseModel):
    name: str
    location_id: uuid.UUID | None = None
    source_import_batch_id: uuid.UUID | None = None
    deadline_at: dt.datetime | None = None


class CampaignUpdate(BaseModel):
    expected_version: int
    name: str | None = None
    location_id: uuid.UUID | None = None
    source_import_batch_id: uuid.UUID | None = None
    deadline_at: dt.datetime | None = None


class AssignRequest(BaseModel):
    user_id: uuid.UUID
    expected_version: int


class VersionRequest(BaseModel):
    expected_version: int


class StartRequest(BaseModel):
    expected_version: int
    confirm_aggregate_source: bool = False


class ReopenRequest(BaseModel):
    reason: str
    new_deadline_at: dt.datetime
    expected_version: int


class CancelRequest(BaseModel):
    reason: str
    expected_version: int


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


# ------------------------------- locations -----------------------------------


@router.get("/locations")
def list_locations(
    current: RequireInventoryRead,
    db: Database,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict[str, Any]]:
    return [
        {
            "id": str(item.id),
            "code": item.code,
            "name": item.name,
            "external_ref": item.external_ref,
            "active": item.active,
        }
        for item in location_service.list_locations(db, limit=limit, offset=offset)
    ]


@router.post("/locations")
def create_location(
    payload: LocationCreate, current: RequireInventoryCreate, db: Database
) -> dict[str, Any]:
    try:
        location = location_service.create_location(
            db,
            actor_id=current.id,
            name=payload.name,
            code=payload.code,
            external_ref=payload.external_ref,
            active=payload.active,
        )
    except ServiceErrors as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {"id": str(location.id), "code": location.code, "name": location.name,
            "external_ref": location.external_ref, "active": location.active}


@router.get("/locations/{location_id}")
def get_location(
    location_id: uuid.UUID, current: RequireInventoryRead, db: Database
) -> dict[str, Any]:
    try:
        location = location_service.get_location(db, location_id)
    except ServiceErrors as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {"id": str(location.id), "code": location.code, "name": location.name,
            "external_ref": location.external_ref, "active": location.active}


@router.patch("/locations/{location_id}")
def update_location(
    location_id: uuid.UUID, payload: LocationUpdate, current: RequireInventoryCreate, db: Database
) -> dict[str, Any]:
    try:
        location = location_service.update_location(
            db,
            actor_id=current.id,
            location_id=location_id,
            name=payload.name,
            code=payload.code,
            external_ref=payload.external_ref,
            active=payload.active,
        )
    except ServiceErrors as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {"id": str(location.id), "code": location.code, "name": location.name,
            "external_ref": location.external_ref, "active": location.active}


# --------------------------- snapshot sources --------------------------------


@router.get("/snapshot-sources")
def list_snapshot_sources(
    current: RequireInventoryCreate,
    db: Database,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict[str, Any]]:
    return snapshot_service.eligible_sources(db, limit=limit, offset=offset)


# ------------------------------ campaigns ------------------------------------


@router.get("/campaigns")
def list_campaigns(
    current: RequireInventoryRead,
    db: Database,
    status: Annotated[CampaignStatus | None, Query()] = None,
    location_id: Annotated[uuid.UUID | None, Query()] = None,
    assigned_user_id: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    rows, total = campaign_service.list_campaigns(
        db,
        status=status,
        location_id=location_id,
        assigned_user_id=assigned_user_id,
        limit=limit,
        offset=offset,
    )
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [campaign_service.campaign_summary(row) for row in rows],
    }


@router.post("/campaigns")
def create_campaign(
    payload: CampaignCreate, current: RequireInventoryCreate, db: Database
) -> dict[str, Any]:
    try:
        campaign = campaign_service.create_campaign(
            db,
            actor_id=current.id,
            name=payload.name,
            location_id=payload.location_id,
            source_import_batch_id=payload.source_import_batch_id,
            deadline_at=payload.deadline_at,
        )
    except ServiceErrors as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return campaign_service.campaign_detail(campaign)


@router.get("/campaigns/{campaign_id}")
def get_campaign(
    campaign_id: uuid.UUID, current: RequireInventoryRead, db: Database
) -> dict[str, Any]:
    try:
        campaign = campaign_service.get_campaign(db, campaign_id)
        campaign_service.refresh_expiry(db, campaign)
    except ServiceErrors as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return campaign_service.campaign_detail(campaign)


@router.patch("/campaigns/{campaign_id}")
def update_campaign(
    campaign_id: uuid.UUID, payload: CampaignUpdate, current: RequireInventoryCreate, db: Database
) -> dict[str, Any]:
    try:
        campaign = campaign_service.update_campaign(
            db,
            actor_id=current.id,
            campaign_id=campaign_id,
            expected_version=payload.expected_version,
            name=payload.name,
            location_id=payload.location_id,
            source_import_batch_id=payload.source_import_batch_id,
            deadline_at=payload.deadline_at,
        )
    except ServiceErrors as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return campaign_service.campaign_detail(campaign)


@router.get("/my-assignments")
def my_assignments(
    current: RequireInventoryRead,
    db: Database,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict[str, Any]]:
    rows = db.execute(
        select(InventoryAssignment, InventoryCampaign)
        .join(
            InventoryCampaign,
            InventoryCampaign.id == InventoryAssignment.inventory_campaign_id,
        )
        .where(
            InventoryAssignment.user_id == current.id,
            InventoryAssignment.status == AssignmentStatus.ACTIVE,
            InventoryAssignment.revoked_at.is_(None),
        )
        .order_by(InventoryAssignment.assigned_at.desc(), InventoryAssignment.id)
        .offset(offset)
        .limit(limit)
    ).all()
    result: list[dict[str, Any]] = []
    for assignment, campaign in rows:
        item = campaign_service.campaign_summary(campaign)
        item["assignment_id"] = str(assignment.id)
        item["assigned_at"] = _iso(assignment.assigned_at)
        result.append(item)
    return result


# ------------------------------ assignments ----------------------------------


@router.post("/campaigns/{campaign_id}/assign")
def assign_responsible(
    campaign_id: uuid.UUID, payload: AssignRequest, current: RequireInventoryAssign, db: Database
) -> dict[str, Any]:
    try:
        assignment, created, reassigned = assignment_service.assign(
            db,
            actor_id=current.id,
            campaign_id=campaign_id,
            user_id=payload.user_id,
            expected_version=payload.expected_version,
        )
    except ServiceErrors as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {
        "assignment_id": str(assignment.id),
        "user_id": str(assignment.user_id),
        "status": assignment.status.value,
        "created": created,
        "reassigned": reassigned,
    }


@router.post("/campaigns/{campaign_id}/unassign")
def unassign_responsible(
    campaign_id: uuid.UUID, payload: VersionRequest, current: RequireInventoryAssign, db: Database
) -> dict[str, Any]:
    try:
        assignment_service.unassign(
            db,
            actor_id=current.id,
            campaign_id=campaign_id,
            expected_version=payload.expected_version,
        )
    except ServiceErrors as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {"status": "ok"}


@router.get("/campaigns/{campaign_id}/assignments")
def assignment_history(
    campaign_id: uuid.UUID,
    current: RequireInventoryMonitor,
    db: Database,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict[str, Any]]:
    try:
        campaign_service.get_campaign(db, campaign_id)
    except ServiceErrors as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return assignment_service.history_payload(
        assignment_service.history(db, campaign_id, limit=limit, offset=offset)
    )


# ------------------------------- snapshot ------------------------------------


@router.post("/campaigns/{campaign_id}/snapshot/preview")
def snapshot_preview(
    campaign_id: uuid.UUID, current: RequireExpectedRead, db: Database
) -> dict[str, Any]:
    try:
        campaign = campaign_service.get_campaign(db, campaign_id)
        return snapshot_service.preview(db, campaign)
    except ServiceErrors as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.get("/campaigns/{campaign_id}/snapshot")
def get_snapshot(
    campaign_id: uuid.UUID,
    current: RequireExpectedRead,
    db: Database,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    try:
        campaign = campaign_service.get_campaign(db, campaign_id)
    except ServiceErrors as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    total = snapshot_service.snapshot_item_count(db, campaign.id)
    items = snapshot_service.list_snapshot_items(db, campaign.id, offset=offset, limit=limit)
    return {
        "campaign_id": str(campaign.id),
        "code": campaign.code,
        "snapshot_frozen_at": _iso(campaign.snapshot_frozen_at),
        "snapshot_sha256": campaign.snapshot_sha256,
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "product_id": str(item.product_id),
                "internal_reference": item.internal_reference_snapshot,
                "description": item.description_snapshot,
                "expected_quantity": str(item.expected_quantity),
                "sale_price": str(item.sale_price_snapshot),
                "cost": str(item.cost_snapshot),
                "consignment_cost": str(item.consignment_cost_snapshot),
                "effective_cost": str(item.effective_cost_snapshot),
                "cost_source": item.cost_source.value,
                "currency": item.currency_snapshot,
                "supplier_reference": item.supplier_reference_snapshot,
            }
            for item in items
        ],
    }


# -------------------------- lifecycle: start/reopen/cancel -------------------


@router.post("/campaigns/{campaign_id}/start")
def start_campaign(
    campaign_id: uuid.UUID, payload: StartRequest, current: RequireInventoryCreate, db: Database
) -> dict[str, Any]:
    try:
        campaign, already_started = campaign_service.start_campaign(
            db,
            campaign_id=campaign_id,
            expected_version=payload.expected_version,
            confirm_aggregate_source=payload.confirm_aggregate_source,
        )
    except ServiceErrors as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    data = campaign_service.campaign_detail(campaign)
    data["already_started"] = already_started
    return data


@router.post("/campaigns/{campaign_id}/reopen")
def reopen_campaign(
    campaign_id: uuid.UUID, payload: ReopenRequest, current: RequireInventoryReopen, db: Database
) -> dict[str, Any]:
    try:
        campaign = campaign_service.reopen_campaign(
            db,
            actor_id=current.id,
            campaign_id=campaign_id,
            expected_version=payload.expected_version,
            reason=payload.reason,
            new_deadline_at=payload.new_deadline_at,
        )
    except ServiceErrors as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return campaign_service.campaign_detail(campaign)


@router.post("/campaigns/{campaign_id}/cancel")
def cancel_campaign(
    campaign_id: uuid.UUID, payload: CancelRequest, current: RequireInventoryClose, db: Database
) -> dict[str, Any]:
    try:
        campaign = campaign_service.cancel_campaign(
            db,
            actor_id=current.id,
            campaign_id=campaign_id,
            expected_version=payload.expected_version,
            reason=payload.reason,
        )
    except ServiceErrors as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return campaign_service.campaign_detail(campaign)
