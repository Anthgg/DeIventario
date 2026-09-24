"""Documentos oficiales globales: listado, detalle y descarga (F010G).

Requiere ``exports.read``. La descarga sigue permitida en campana CLOSED.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from app.auth.dependencies import CurrentUser, Database, require_permission
from app.auth.permissions import EXPORTS_READ
from app.models.enums import DocumentStatus
from app.services.documents import document_service
from app.services.documents.document_service import DocumentError
from app.services.documents.export_profile_service import ExportProfileError
from app.services.inventory.campaign_service import CampaignError
from app.services.valuation.errors import ValuationError

router = APIRouter(prefix="/documents", tags=["documents"])

RequireExportsRead = Annotated[CurrentUser, Depends(require_permission(EXPORTS_READ))]


def _handle(exc: Exception) -> HTTPException:
    if isinstance(
        exc,
        (DocumentError, ExportProfileError, CampaignError, ValuationError),
    ):
        detail: dict[str, Any] = {"message": str(exc)}
        if getattr(exc, "code", None):
            detail["error"] = exc.code
        payload = getattr(exc, "payload", None)
        if isinstance(payload, dict) and payload:
            detail.update(payload)
        return HTTPException(status_code=exc.status_code, detail=detail)
    return HTTPException(status_code=400, detail={"message": "Error de documentos"})


@router.get("")
def list_official_documents(
    current: RequireExportsRead,
    db: Database,
    document_type: Annotated[str | None, Query()] = None,
    entity_type: Annotated[str | None, Query()] = None,
    entity_id: Annotated[uuid.UUID | None, Query()] = None,
    status: Annotated[DocumentStatus | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    result = document_service.list_documents(
        db,
        document_type=document_type,
        entity_type=entity_type,
        entity_id=entity_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    return result


@router.get("/{document_id}")
def get_official_document(
    document_id: uuid.UUID, current: RequireExportsRead, db: Database
) -> dict[str, Any]:
    try:
        row = document_service.get_document(db, document_id)
    except DocumentError as exc:
        raise _handle(exc) from exc
    return document_service.document_payload(row)


@router.get("/{document_id}/download")
def download_official_document(
    document_id: uuid.UUID, current: RequireExportsRead, db: Database
) -> FileResponse:
    try:
        path, content_type, filename = document_service.document_file(db, document_id)
    except DocumentError as exc:
        raise _handle(exc) from exc
    return FileResponse(path, media_type=content_type, filename=filename)
