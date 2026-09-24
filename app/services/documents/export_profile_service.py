"""Perfiles configurables de exportacion ERP (F010E).

El perfil canonico ``CANONICAL_INVENTORY_ADJUSTMENT`` se materializa bajo
demanda. No existe plantilla oficial de vendor en el repositorio: ningun
perfil declara ``requires_vendor_template`` activo.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.documents.modules.inventory.erp_export import (
    CANONICAL_COLUMNS,
    CANONICAL_PROFILE_CODE,
)
from app.models import ExportProfile


class ExportProfileError(Exception):
    def __init__(self, message: str, status_code: int = 400, code: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def profile_payload(row: ExportProfile) -> dict[str, object]:
    return {
        "id": str(row.id),
        "code": row.code,
        "vendor": row.vendor,
        "description": row.description,
        "version": row.version,
        "active": row.active,
        "document_format": row.document_format.value,
        "column_order": list(row.column_order) if row.column_order else None,
        "column_mapping": dict(row.column_mapping) if row.column_mapping else None,
        "file_extension": row.file_extension,
        "field_separator": row.field_separator,
        "decimal_separator": row.decimal_separator,
        "thousands_separator": row.thousands_separator,
        "currency": row.currency,
        "date_format": row.date_format,
        "document_number_format": row.document_number_format,
        "encoding": row.encoding,
        "requires_vendor_template": row.requires_vendor_template,
    }


def _canonical_row(db: Session) -> ExportProfile:
    row = ExportProfile(
        id=uuid.uuid4(),
        code=CANONICAL_PROFILE_CODE,
        vendor=None,
        description="Ajuste de inventario canonico (sin plantilla de vendor)",
        version="1",
        active=True,
        column_order=list(CANONICAL_COLUMNS),
        column_mapping=None,
        requires_vendor_template=False,
        created_by=None,
    )
    db.add(row)
    db.flush()
    return row


def get_profile(db: Session, code: str | None = None) -> ExportProfile:
    """Devuelve el perfil activo solicitado (materializa el canonico)."""
    wanted = (code or CANONICAL_PROFILE_CODE).strip().upper()
    row = db.execute(
        select(ExportProfile).where(ExportProfile.code == wanted).limit(1)
    ).scalar_one_or_none()
    if row is None:
        if wanted != CANONICAL_PROFILE_CODE:
            raise ExportProfileError(
                "Perfil de exportacion no encontrado", 404, "EXPORT_PROFILE_NOT_FOUND"
            )
        row = _canonical_row(db)
        db.commit()
        return row
    if not row.active:
        raise ExportProfileError(
            "El perfil de exportacion esta inactivo", 409, "EXPORT_PROFILE_INACTIVE"
        )
    return row


def list_profiles(db: Session) -> list[ExportProfile]:
    return list(db.execute(select(ExportProfile).order_by(ExportProfile.code)).scalars())
