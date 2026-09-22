"""Pruebas de los servicios/orquestador de importacion (contra PostgreSQL)."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.session import SessionLocal
from app.importers.base import ParsedContact, ParsedProduct
from app.models import Contact, ImportBatch, Product
from app.models.enums import ImportBatchStatus, ImportBatchType
from app.services.imports.contact_import_service import ContactImportService
from app.services.imports.import_orchestrator import ImportOrchestrator, sha256_of
from app.services.imports.product_import_service import ProductImportService
from tests.import_helpers import cleanup_test_data, make_xlsx
from tests.test_importers import CONTACT_HEADERS, MIXED_HEADERS


def test_contact_upsert_create_then_update() -> None:
    service = ContactImportService()
    with SessionLocal() as session:
        transaction = session.begin()
        try:
            created = service.upsert(
                session,
                ParsedContact(
                    external_ref="TEST-UP-C1",
                    name="Proveedor A",
                    raw_external_id="TEST-UP-C1",
                ),
            )
            assert created is True
            updated = service.upsert(
                session,
                ParsedContact(
                    external_ref="TEST-UP-C1",
                    name="Proveedor A Renombrado",
                    raw_external_id="TEST-UP-C1",
                ),
            )
            assert updated is False
            row = session.execute(
                select(Contact).where(Contact.external_ref == "TEST-UP-C1")
            ).scalar_one()
            assert row.name == "Proveedor A Renombrado"
        finally:
            transaction.rollback()


def test_product_upsert_preserves_cost_when_absent() -> None:
    service = ProductImportService()
    with SessionLocal() as session:
        transaction = session.begin()
        try:
            created_id, created = service.upsert(
                session,
                ParsedProduct(
                    internal_reference="TEST-UP-P1",
                    name="Producto A",
                    sale_price=None,
                    cost=None,
                    consignment_cost=None,
                    quantity=None,
                ),
            )
            assert created is True
            # Actualiza solo nombre: el costo inexistente no debe tocarse.
            _pid, updated = service.upsert(
                session,
                ParsedProduct(
                    internal_reference="TEST-UP-P1",
                    name="Producto A v2",
                    sale_price=None,
                    cost=None,
                    consignment_cost=None,
                    quantity=None,
                ),
            )
            assert updated is False
            row = session.execute(
                select(Product).where(Product.id == created_id)
            ).scalar_one()
            assert row.name == "Producto A v2"
            assert row.cost is None
        finally:
            transaction.rollback()


def test_dry_run_does_not_write() -> None:
    orchestrator = ImportOrchestrator()
    data = make_xlsx(
        CONTACT_HEADERS,
        [["", "", "", "", "", "Proveedor Dry", "", "Perú", "", "", "", "TEST-DRY"]],
    )
    summary = orchestrator.run(data, "test-dry.xlsx", dry_run=True)
    assert summary.created == 0
    with SessionLocal() as session:
        found = session.execute(
            select(Contact).where(Contact.external_ref == "TEST-DRY")
        ).scalar_one_or_none()
        assert found is None


def test_preview_reports_missing_contact_refs() -> None:
    orchestrator = ImportOrchestrator()
    data = make_xlsx(
        MIXED_HEADERS,
        [["NOPROV", "No existe", "REFX", "Producto X", "10", "0", "5", "", "", "", ""]],
    )
    summary = orchestrator.preview(data, "test-mixed.xlsx")
    assert summary.import_type is ImportBatchType.MIXED_PRODUCT_EXPORT
    assert "NOPROV" in summary.missing_contact_refs
    assert summary.products_detected == 1


def test_duplicate_file_returns_already_imported() -> None:
    cleanup_test_data()
    orchestrator = ImportOrchestrator()
    data = make_xlsx(
        CONTACT_HEADERS,
        [["", "", "", "", "", "Proveedor Dup", "", "Perú", "", "", "", "TEST-DUP"]],
    )
    first = orchestrator.run(data, "test-dup.xlsx")
    assert first.already_imported is False
    assert first.created == 1
    second = orchestrator.run(data, "test-dup.xlsx")
    assert second.already_imported is True
    cleanup_test_data()


def test_fatal_error_marks_batch_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    cleanup_test_data()
    orchestrator = ImportOrchestrator()

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(orchestrator, "_persist", boom)
    data = make_xlsx(
        CONTACT_HEADERS,
        [["", "", "", "", "", "Proveedor Fail", "", "Perú", "", "", "", "TEST-FAIL"]],
    )
    with pytest.raises(RuntimeError):
        orchestrator.run(data, "test-fail.xlsx")

    with SessionLocal() as session:
        batch = session.execute(
            select(ImportBatch).where(ImportBatch.source_filename == "test-fail.xlsx")
        ).scalar_one()
        assert batch.status is ImportBatchStatus.FAILED
    cleanup_test_data()


def test_partial_unique_index_blocks_duplicate_active_batch() -> None:
    cleanup_test_data()
    orchestrator = ImportOrchestrator()
    data = make_xlsx(
        CONTACT_HEADERS,
        [["", "", "", "", "", "Proveedor Conc", "", "Perú", "", "", "", "TEST-CONC"]],
    )
    orchestrator.run(data, "test-conc.xlsx")
    sha = sha256_of(data)
    with SessionLocal() as session:
        session.add(
            ImportBatch(
                import_type=ImportBatchType.CONTACTS,
                source_filename="test-conc-2.xlsx",
                source_sha256=sha,
                status=ImportBatchStatus.PROCESSING,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
    cleanup_test_data()
