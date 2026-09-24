"""Endpoints de configuracion organizacional y branding (F010A).

Requiere ``system.manage`` en GET, PATCH y logo. Ninguna marca vive en el
codigo: la identidad se persiste en ``organization_settings``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from app.auth.dependencies import CurrentUser, Database, require_permission
from app.auth.permissions import SYSTEM_MANAGE
from app.services.system import organization_service
from app.services.system.organization_service import OrganizationError

router = APIRouter(prefix="/system", tags=["system"])

RequireSystemManage = Annotated[CurrentUser, Depends(require_permission(SYSTEM_MANAGE))]


class SettingsPatch(BaseModel):
    expected_version: int | None = None
    legal_name: str | None = None
    tax_id: str | None = None
    tax_id_label: str | None = None
    address: str | None = None
    phone: str | None = None
    email: str | None = None
    website: str | None = None
    brand_color_primary: str | None = None
    brand_color_secondary: str | None = None
    brand_color_accent: str | None = None
    currency_style: str | None = None
    date_format: str | None = None
    footer_text: str | None = None
    footer_left: str | None = None
    footer_right: str | None = None
    document_number_prefix: str | None = None


def _handle(exc: OrganizationError) -> HTTPException:
    detail: dict[str, Any] = {"message": str(exc)}
    if exc.code:
        detail["error"] = exc.code
    return HTTPException(status_code=exc.status_code, detail=detail)


@router.get("/organization-settings")
def get_organization_settings(current: RequireSystemManage, db: Database) -> dict[str, Any]:
    try:
        row = organization_service.get_settings(db)
        return organization_service.settings_payload(row)
    except OrganizationError as exc:
        raise _handle(exc) from exc


@router.patch("/organization-settings")
def patch_organization_settings(
    payload: SettingsPatch, current: RequireSystemManage, db: Database
) -> dict[str, Any]:
    patch: dict[str, object] = {}
    for field_name in payload.model_fields_set:
        if field_name == "expected_version":
            continue
        patch[field_name] = getattr(payload, field_name)
    try:
        return organization_service.update_settings(
            db,
            actor_id=current.id,
            expected_version=payload.expected_version,
            patch=patch,
        )
    except OrganizationError as exc:
        raise _handle(exc) from exc


@router.post("/organization-settings/logo")
async def upload_organization_logo(
    current: RequireSystemManage, db: Database, file: Annotated[UploadFile, File()]
) -> dict[str, Any]:
    data = await file.read()
    try:
        return organization_service.update_logo(
            db,
            actor_id=current.id,
            declared_mime=file.content_type,
            data=data,
            original_name=file.filename,
        )
    except OrganizationError as exc:
        raise _handle(exc) from exc
