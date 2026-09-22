"""Endpoints de importacion Odoo (preview / commit / consulta)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import CurrentUser, require_permission
from app.auth.permissions import IMPORTS_EXECUTE, IMPORTS_PREVIEW, IMPORTS_READ
from app.core.config import get_settings
from app.db.session import get_db
from app.importers.base import ImportError, ImportSummary, InvalidFileError
from app.models import ImportBatch, ImportErrorRecord
from app.services.imports.import_orchestrator import ImportOrchestrator

router = APIRouter(prefix="/imports", tags=["imports"])

_orchestrator = ImportOrchestrator()

Upload = Annotated[UploadFile, File()]
Database = Annotated[Session, Depends(get_db)]
RequireImportsPreview = Annotated[CurrentUser, Depends(require_permission(IMPORTS_PREVIEW))]
RequireImportsExecute = Annotated[CurrentUser, Depends(require_permission(IMPORTS_EXECUTE))]
RequireImportsRead = Annotated[CurrentUser, Depends(require_permission(IMPORTS_READ))]


def _read_xlsx(file: UploadFile) -> bytes:
    """Valida extension, contenido y tamano; devuelve los bytes del archivo."""
    filename = file.filename or ""
    if not filename.lower().endswith(".xlsx"):
        raise HTTPException(status_code=415, detail="Solo se aceptan archivos .xlsx.")
    data = file.file.read()
    if not data:
        raise HTTPException(status_code=400, detail="El archivo esta vacio.")
    max_bytes = get_settings().MAX_UPLOAD_MB * 1024 * 1024
    if len(data) > max_bytes:
        limit = get_settings().MAX_UPLOAD_MB
        raise HTTPException(status_code=413, detail=f"El archivo supera {limit} MB.")
    return data


def _summary_payload(summary: ImportSummary) -> dict[str, object]:
    return {
        "import_type": summary.import_type.value,
        "source_filename": summary.source_filename,
        "source_sha256": summary.source_sha256,
        "already_imported": summary.already_imported,
        "batch_id": summary.batch_id,
        "total_rows": summary.total_rows,
        "contacts_detected": summary.contacts_detected,
        "products_detected": summary.products_detected,
        "movements_detected": summary.movements_detected,
        "stock_snapshots_detected": summary.stock_snapshots_detected,
        "missing_contact_refs": summary.missing_contact_refs,
        "duplicate_product_codes": summary.duplicate_product_codes,
        "invalid_rows": summary.invalid_rows,
        "warnings": summary.warnings,
        "errors": [
            {
                "row_number": error.row_number,
                "column_name": error.column_name,
                "error_code": error.error_code,
                "message": error.message,
            }
            for error in summary.errors
        ],
        "created": summary.created,
        "updated": summary.updated,
        "skipped": summary.skipped,
        "products_with_sale_price": summary.products_with_sale_price,
        "products_with_cost": summary.products_with_cost,
        "products_with_consignment_cost": summary.products_with_consignment_cost,
        "products_using_cost": summary.products_using_cost,
        "products_using_consignment_cost": summary.products_using_consignment_cost,
        "products_with_zero_effective_cost": summary.products_with_zero_effective_cost,
    }


@router.post("/preview")
def preview_import(file: Upload, current: RequireImportsPreview) -> dict[str, object]:
    """Analiza el archivo sin persistir nada."""
    data = _read_xlsx(file)
    try:
        summary = _orchestrator.preview(data, file.filename or "upload.xlsx")
    except (InvalidFileError, ImportError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _summary_payload(summary)


@router.post("")
def commit_import(
    file: Upload,
    current: RequireImportsExecute,
    dry_run: Annotated[bool, Query()] = False,
) -> dict[str, object]:
    """Importa el archivo. Con ``dry_run=true`` no modifica datos de negocio."""
    data = _read_xlsx(file)
    try:
        summary = _orchestrator.run(data, file.filename or "upload.xlsx", dry_run=dry_run)
    except (InvalidFileError, ImportError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _summary_payload(summary)


@router.get("/{batch_id}")
def get_batch(batch_id: uuid.UUID, current: RequireImportsRead, db: Database) -> dict[str, object]:
    batch = db.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Lote no encontrado.")
    return {
        "id": str(batch.id),
        "import_type": batch.import_type.value,
        "source_filename": batch.source_filename,
        "source_sha256": batch.source_sha256,
        "status": batch.status.value,
        "started_at": batch.started_at.isoformat() if batch.started_at else None,
        "completed_at": batch.completed_at.isoformat() if batch.completed_at else None,
        "row_count": batch.row_count,
        "processed_count": batch.processed_count,
        "created_count": batch.created_count,
        "updated_count": batch.updated_count,
        "skipped_count": batch.skipped_count,
        "error_count": batch.error_count,
    }


@router.get("/{batch_id}/errors")
def get_batch_errors(
    batch_id: uuid.UUID, current: RequireImportsRead, db: Database
) -> list[dict[str, object]]:
    batch = db.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Lote no encontrado.")
    rows = db.execute(
        select(ImportErrorRecord)
        .where(ImportErrorRecord.import_batch_id == batch_id)
        .order_by(ImportErrorRecord.row_number)
    ).scalars()
    return [
        {
            "row_number": row.row_number,
            "column_name": row.column_name,
            "error_code": row.error_code,
            "message": row.message,
            "raw_value": row.raw_value,
        }
        for row in rows
    ]
