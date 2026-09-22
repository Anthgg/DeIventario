"""Helpers para generar XLSX en memoria en los tests de importacion."""

from __future__ import annotations

import io

import openpyxl
from sqlalchemy import delete

from app.db.session import SessionLocal
from app.models import Contact, ImportBatch, Product


def make_xlsx(headers: list[str], rows: list[list[object]]) -> bytes:
    """Construye un archivo .xlsx en memoria y devuelve sus bytes."""
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def cleanup_test_data() -> None:
    """Elimina solo los datos de prueba (marcados con prefijos 'test'/'TEST-')."""
    with SessionLocal() as session:
        session.execute(delete(ImportBatch).where(ImportBatch.source_filename.like("test%")))
        session.execute(delete(Product).where(Product.internal_reference.like("TEST-%")))
        session.execute(delete(Contact).where(Contact.external_ref.like("TEST-%")))
        session.commit()
