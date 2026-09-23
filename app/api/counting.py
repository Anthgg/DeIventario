"""Endpoints del motor de conteo (F005). Blind-safe por diseno."""

from __future__ import annotations

import datetime as dt
import decimal
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.dependencies import CurrentUser, Database, require_any_permission, require_permission
from app.auth.permissions import INVENTORY_COUNT, INVENTORY_MONITOR
from app.models import InventoryCountSession
from app.models.enums import CountEventType, EventSource
from app.services.counting import event_service, session_service
from app.services.counting.errors import CountError
from app.services.counting.qr_service import QrCodeError

router = APIRouter(prefix="/inventory", tags=["inventory-count"])

RequireCount = Annotated[CurrentUser, Depends(require_permission(INVENTORY_COUNT))]
RequireMonitor = Annotated[CurrentUser, Depends(require_permission(INVENTORY_MONITOR))]
RequireCountOrMonitor = Annotated[
    CurrentUser, Depends(require_any_permission(INVENTORY_COUNT, INVENTORY_MONITOR))
]


class EventRequest(BaseModel):
    client_event_uuid: uuid.UUID
    event_type: CountEventType
    scanned_code: str | None = None
    product_id: uuid.UUID | None = None
    quantity: decimal.Decimal | None = None
    occurred_at: dt.datetime | None = None
    source: EventSource | None = None


class BatchRequest(BaseModel):
    events: list[EventRequest]


class UndoRequest(BaseModel):
    client_event_uuid: uuid.UUID | None = None


class SubmitRequest(BaseModel):
    expected_version: int
    confirm_missing: bool = False


def _handle(exc: Exception) -> HTTPException:
    if isinstance(exc, CountError):
        detail: dict[str, Any] = {"message": str(exc)}
        if exc.code:
            detail["error"] = exc.code
        detail.update(exc.payload)
        return HTTPException(status_code=exc.status_code, detail=detail)
    if isinstance(exc, QrCodeError):
        return HTTPException(status_code=exc.status_code, detail={"message": str(exc)})
    return HTTPException(status_code=400, detail={"message": "Error de conteo"})


def _to_input(payload: EventRequest) -> event_service.EventInput:
    occurred = payload.occurred_at
    if occurred is not None and occurred.tzinfo is None:
        occurred = occurred.replace(tzinfo=dt.UTC)
    return event_service.EventInput(
        client_event_uuid=payload.client_event_uuid,
        event_type=payload.event_type,
        scanned_code=payload.scanned_code,
        product_id=payload.product_id,
        quantity=payload.quantity,
        occurred_at=occurred,
        source=payload.source,
    )


def _readable_session(
    db: Session, session_id: uuid.UUID, current: CurrentUser
) -> InventoryCountSession:
    session = session_service.get_session(db, session_id)
    if session.user_id != current.id and not current.has_permission(INVENTORY_MONITOR):
        raise HTTPException(status_code=403, detail={"message": "Sin permiso"})
    return session


# --------------------------------- sesiones ----------------------------------


@router.post("/campaigns/{campaign_id}/count-sessions/start")
def start_count_session(
    campaign_id: uuid.UUID, current: RequireCount, db: Database
) -> dict[str, Any]:
    try:
        session, already = session_service.start_session(
            db, campaign_id=campaign_id, actor_id=current.id
        )
    except CountError as exc:
        raise _handle(exc) from exc
    payload = session_service.session_payload(db, session)
    payload["already_started"] = already
    return payload


@router.get("/count-sessions/{session_id}")
def get_count_session(
    session_id: uuid.UUID, current: RequireCountOrMonitor, db: Database
) -> dict[str, Any]:
    session = _readable_session(db, session_id, current)
    return session_service.session_payload(db, session)


@router.get("/count-sessions/{session_id}/items")
def get_count_items(
    session_id: uuid.UUID, current: RequireCountOrMonitor, db: Database
) -> list[dict[str, Any]]:
    session = _readable_session(db, session_id, current)
    return session_service.session_items(db, session.id)


@router.get("/count-sessions/{session_id}/events")
def get_count_events(
    session_id: uuid.UUID,
    current: RequireCountOrMonitor,
    db: Database,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    session = _readable_session(db, session_id, current)
    events = event_service.event_history(db, session.id, offset=offset, limit=limit)
    return {
        "session_id": str(session.id),
        "limit": limit,
        "offset": offset,
        "items": [event_service.event_payload(event, already_processed=False) for event in events],
    }


# ---------------------------------- eventos ----------------------------------


@router.post("/count-sessions/{session_id}/events")
def post_count_event(
    session_id: uuid.UUID, payload: EventRequest, current: RequireCount, db: Database
) -> dict[str, Any]:
    try:
        result = event_service.process_event(
            db, session_id=session_id, actor_id=current.id, payload=_to_input(payload)
        )
    except (CountError, QrCodeError) as exc:
        raise _handle(exc) from exc
    return event_service.event_payload(result.event, already_processed=result.already_processed)


@router.post("/count-sessions/{session_id}/events/batch")
def post_count_events_batch(
    session_id: uuid.UUID, payload: BatchRequest, current: RequireCount, db: Database
) -> dict[str, Any]:
    try:
        results = event_service.process_batch(
            db,
            session_id=session_id,
            actor_id=current.id,
            events=[_to_input(item) for item in payload.events],
        )
    except (CountError, QrCodeError) as exc:
        raise _handle(exc) from exc
    return {
        "processed": len(results),
        "items": [
            event_service.event_payload(result.event, already_processed=result.already_processed)
            for result in results
        ],
    }


@router.post("/count-sessions/{session_id}/events/{event_id}/undo")
def post_undo_event(
    session_id: uuid.UUID,
    event_id: uuid.UUID,
    payload: UndoRequest,
    current: RequireCount,
    db: Database,
) -> dict[str, Any]:
    try:
        result = event_service.undo_event(
            db,
            session_id=session_id,
            target_event_id=event_id,
            actor_id=current.id,
            client_event_uuid=payload.client_event_uuid,
        )
    except CountError as exc:
        raise _handle(exc) from exc
    return event_service.event_payload(result.event, already_processed=result.already_processed)


# --------------------------- finish-check y submit ---------------------------


@router.get("/count-sessions/{session_id}/finish-check")
def get_finish_check(
    session_id: uuid.UUID, current: RequireCountOrMonitor, db: Database
) -> dict[str, Any]:
    session = _readable_session(db, session_id, current)
    return session_service.finish_check(db, session)


@router.post("/count-sessions/{session_id}/submit")
def post_submit(
    session_id: uuid.UUID, payload: SubmitRequest, current: RequireCount, db: Database
) -> dict[str, Any]:
    try:
        session, already = session_service.submit_session(
            db,
            session_id=session_id,
            actor_id=current.id,
            expected_version=payload.expected_version,
            confirm_missing=payload.confirm_missing,
        )
    except CountError as exc:
        raise _handle(exc) from exc
    result = session_service.session_payload(db, session)
    result["already_submitted"] = already
    return result


# -------------------------------- monitoring ---------------------------------


@router.get("/campaigns/{campaign_id}/count-sessions")
def list_campaign_count_sessions(
    campaign_id: uuid.UUID, current: RequireMonitor, db: Database
) -> list[dict[str, Any]]:
    return session_service.list_campaign_sessions(db, campaign_id)
