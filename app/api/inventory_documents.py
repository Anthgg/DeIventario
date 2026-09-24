"""Generacion de documentos oficiales y cierre de campana (F010F/G).

Endpoints bajo ``/inventory``: generacion por tipo, historial por campana y
cierre oficial. Reutiliza ``exports.create``/``exports.read``/``inventory.close``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.auth.dependencies import CurrentUser, Database, require_permission
from app.auth.permissions import EXPORTS_CREATE, EXPORTS_READ, INVENTORY_CLOSE
from app.services.documents import document_service
from app.services.documents.document_service import DocumentError
from app.services.documents.export_profile_service import ExportProfileError
from app.services.inventory.campaign_service import CampaignError
from app.services.system.organization_service import OrganizationError
from app.services.valuation.errors import ValuationError

router = APIRouter(prefix="/inventory", tags=["inventory-documents"])

RequireExportsCreate = Annotated[CurrentUser, Depends(require_permission(EXPORTS_CREATE))]
RequireExportsRead = Annotated[CurrentUser, Depends(require_permission(EXPORTS_READ))]
RequireClose = Annotated[CurrentUser, Depends(require_permission(INVENTORY_CLOSE))]


class GenerateRequest(BaseModel):
    expected_version: int | None = None
    profile_code: str | None = None


class CloseRequest(BaseModel):
    expected_version: int | None = None


def _handle(exc: Exception) -> HTTPException:
    if isinstance(
        exc,
        (DocumentError, ExportProfileError, CampaignError, OrganizationError, ValuationError),
    ):
        detail: dict[str, Any] = {"message": str(exc)}
        if getattr(exc, "code", None):
            detail["error"] = exc.code
        payload = getattr(exc, "payload", None)
        if isinstance(payload, dict) and payload:
            detail.update(payload)
        return HTTPException(status_code=exc.status_code, detail=detail)
    return HTTPException(status_code=400, detail={"message": "Error de documentos"})


@router.post("/campaigns/{campaign_id}/documents/management-report")
def generate_management_report(
    campaign_id: uuid.UUID,
    payload: GenerateRequest,
    current: RequireExportsCreate,
    db: Database,
) -> dict[str, Any]:
    try:
        return document_service.generate_document(
            db,
            actor_id=current.id,
            campaign_id=campaign_id,
            document_type="management-report",
            expected_version=payload.expected_version,
            actor_label=current.user.display_name,
        )
    except (
        DocumentError,
        ExportProfileError,
        CampaignError,
        OrganizationError,
        ValuationError,
    ) as exc:
        raise _handle(exc) from exc


@router.post("/campaigns/{campaign_id}/documents/audit-export")
def generate_audit_export(
    campaign_id: uuid.UUID,
    payload: GenerateRequest,
    current: RequireExportsCreate,
    db: Database,
) -> dict[str, Any]:
    try:
        return document_service.generate_document(
            db,
            actor_id=current.id,
            campaign_id=campaign_id,
            document_type="audit-export",
            expected_version=payload.expected_version,
            actor_label=current.user.display_name,
        )
    except (
        DocumentError,
        ExportProfileError,
        CampaignError,
        OrganizationError,
        ValuationError,
    ) as exc:
        raise _handle(exc) from exc


@router.post("/campaigns/{campaign_id}/documents/erp-adjustment")
def generate_erp_adjustment(
    campaign_id: uuid.UUID,
    payload: GenerateRequest,
    current: RequireExportsCreate,
    db: Database,
) -> dict[str, Any]:
    try:
        return document_service.generate_document(
            db,
            actor_id=current.id,
            campaign_id=campaign_id,
            document_type="erp-adjustment",
            expected_version=payload.expected_version,
            profile_code=payload.profile_code,
            actor_label=current.user.display_name,
        )
    except (
        DocumentError,
        ExportProfileError,
        CampaignError,
        OrganizationError,
        ValuationError,
    ) as exc:
        raise _handle(exc) from exc


@router.get("/campaigns/{campaign_id}/documents")
def list_campaign_documents(
    campaign_id: uuid.UUID,
    current: RequireExportsRead,
    db: Database,
    document_type: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    result = document_service.list_documents(
        db,
        document_type=document_type,
        entity_id=campaign_id,
        limit=limit,
        offset=offset,
    )
    return result


@router.post("/campaigns/{campaign_id}/close")
def close_inventory_campaign(
    campaign_id: uuid.UUID,
    payload: CloseRequest,
    current: RequireClose,
    db: Database,
) -> dict[str, Any]:
    try:
        return document_service.close_campaign(
            db,
            actor_id=current.id,
            campaign_id=campaign_id,
            expected_version=payload.expected_version,
        )
    except (DocumentError, CampaignError, OrganizationError) as exc:
        raise _handle(exc) from exc
