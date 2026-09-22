"""Orquestador del pipeline de importacion (preview / dry-run / commit)."""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.importers import contacts as contacts_parser
from app.importers import mappings
from app.importers import products as products_parser
from app.importers.base import ImportSummary, ParsedData, ParsedProduct
from app.importers.cost import resolve_effective_cost
from app.importers.excel_reader import detect_header, first_sheet, load_workbook
from app.models import Contact, ImportBatch, ImportErrorRecord, StockMovement, StockSnapshot
from app.models.enums import CostSource, ImportBatchStatus, ImportBatchType
from app.services.imports.contact_import_service import ContactImportService
from app.services.imports.product_import_service import ProductImportService

_ACTIVE_STATUSES = (
    ImportBatchStatus.PROCESSING,
    ImportBatchStatus.COMPLETED,
    ImportBatchStatus.COMPLETED_WITH_WARNINGS,
)


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclasses.dataclass
class _PersistResult:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    error_count: int = 0
    missing_refs: list[str] = dataclasses.field(default_factory=list)


class ImportOrchestrator:
    """Coordina parseo, validacion y persistencia de los exports Odoo."""

    def __init__(
        self,
        contact_service: ContactImportService | None = None,
        product_service: ProductImportService | None = None,
    ) -> None:
        self._contact_service = contact_service or ContactImportService()
        self._product_service = product_service or ProductImportService()

    def preview(self, source: bytes, filename: str) -> ImportSummary:
        sha = sha256_of(source)
        parsed = self._parse(source)
        summary = self._build_summary(parsed, filename, sha)
        with SessionLocal() as session:
            summary.missing_contact_refs = self._missing_refs(session, parsed)
        return summary

    def run(self, source: bytes, filename: str, *, dry_run: bool = False) -> ImportSummary:
        sha = sha256_of(source)
        parsed = self._parse(source)
        summary = self._build_summary(parsed, filename, sha)
        if dry_run:
            with SessionLocal() as session:
                summary.missing_contact_refs = self._missing_refs(session, parsed)
            return summary

        with SessionLocal() as session:
            existing = self._find_active_batch(session, parsed.import_type, sha)
            if existing is not None:
                summary.already_imported = True
                summary.batch_id = str(existing)
                return summary
            batch = ImportBatch(
                import_type=parsed.import_type,
                source_filename=filename,
                source_sha256=sha,
                status=ImportBatchStatus.PROCESSING,
                started_at=dt.datetime.now(dt.UTC),
            )
            session.add(batch)
            try:
                session.commit()
            except IntegrityError:
                # Otro proceso creo el lote en paralelo: proteccion a nivel de base.
                session.rollback()
                winner = self._find_active_batch(session, parsed.import_type, sha)
                summary.already_imported = True
                summary.batch_id = str(winner) if winner is not None else None
                return summary
            batch_id = batch.id

        try:
            with SessionLocal() as session:
                loaded = session.execute(
                    select(ImportBatch).where(ImportBatch.id == batch_id)
                ).scalar_one()
                result = self._persist(session, loaded, parsed)
                loaded.status = (
                    ImportBatchStatus.COMPLETED_WITH_WARNINGS
                    if result.error_count
                    else ImportBatchStatus.COMPLETED
                )
                loaded.completed_at = dt.datetime.now(dt.UTC)
                loaded.row_count = parsed.total_rows
                loaded.processed_count = result.created + result.updated
                loaded.created_count = result.created
                loaded.updated_count = result.updated
                loaded.skipped_count = result.skipped
                loaded.error_count = result.error_count
                session.commit()
        except Exception:
            with SessionLocal() as session:
                failed = session.execute(
                    select(ImportBatch).where(ImportBatch.id == batch_id)
                ).scalar_one()
                failed.status = ImportBatchStatus.FAILED
                failed.completed_at = dt.datetime.now(dt.UTC)
                session.commit()
            raise

        summary.created = result.created
        summary.updated = result.updated
        summary.skipped = result.skipped
        summary.missing_contact_refs = result.missing_refs
        summary.batch_id = str(batch_id)
        return summary

    # -- internos --

    def _parse(self, source: bytes) -> ParsedData:
        workbook = load_workbook(source)
        sheet = first_sheet(workbook)
        _header_row, headers = detect_header(sheet)
        import_type = mappings.detect_import_type(headers)
        if import_type is ImportBatchType.CONTACTS:
            parsed_contacts, errors, total = contacts_parser.parse_contacts(workbook)
            return ParsedData(
                import_type=import_type,
                contacts=parsed_contacts,
                errors=errors,
                total_rows=total,
            )
        if import_type is ImportBatchType.MIXED_PRODUCT_EXPORT:
            parsed_products, movements, errors, total, duplicates = (
                products_parser.parse_mixed_product_export(workbook)
            )
            return ParsedData(
                import_type=import_type,
                products=parsed_products,
                movements=movements,
                errors=errors,
                duplicates=duplicates,
                total_rows=total,
            )
        parsed_products, errors, total, duplicates = products_parser.parse_product_master(workbook)
        return ParsedData(
            import_type=import_type,
            products=parsed_products,
            errors=errors,
            duplicates=duplicates,
            total_rows=total,
        )

    def _build_summary(self, parsed: ParsedData, filename: str, sha: str) -> ImportSummary:
        summary = ImportSummary(
            import_type=parsed.import_type,
            source_filename=filename,
            source_sha256=sha,
        )
        summary.total_rows = parsed.total_rows
        summary.contacts_detected = len(parsed.contacts)
        summary.products_detected = len(parsed.products)
        summary.movements_detected = len(parsed.movements)
        summary.stock_snapshots_detected = sum(1 for p in parsed.products if p.quantity is not None)
        summary.duplicate_product_codes = parsed.duplicates
        summary.errors = parsed.errors
        summary.invalid_rows = len(parsed.errors)
        for product in parsed.products:
            if product.sale_price is not None:
                summary.products_with_sale_price += 1
            if product.cost is not None and product.cost > 0:
                summary.products_with_cost += 1
            if product.consignment_cost is not None and product.consignment_cost > 0:
                summary.products_with_consignment_cost += 1
            _effective, source = resolve_effective_cost(product.cost, product.consignment_cost)
            if source is CostSource.COST:
                summary.products_using_cost += 1
            elif source is CostSource.CONSIGNMENT:
                summary.products_using_consignment_cost += 1
            else:
                summary.products_with_zero_effective_cost += 1
        return summary

    def _missing_refs(self, session: Session, parsed: ParsedData) -> list[str]:
        refs = {p.supplier_ref for p in parsed.products if p.supplier_ref}
        if not refs:
            return []
        existing = set(
            session.execute(select(Contact.external_ref).where(Contact.external_ref.in_(refs))).scalars()
        )
        return sorted(refs - existing)

    def _find_active_batch(
        self, session: Session, import_type: ImportBatchType, sha: str
    ) -> uuid.UUID | None:
        return session.execute(
            select(ImportBatch.id)
            .where(
                ImportBatch.import_type == import_type,
                ImportBatch.source_sha256 == sha,
                ImportBatch.status.in_(_ACTIVE_STATUSES),
            )
            .limit(1)
        ).scalar_one_or_none()

    def _persist(self, session: Session, batch: ImportBatch, parsed: ParsedData) -> _PersistResult:
        result = _PersistResult()
        if parsed.import_type is ImportBatchType.CONTACTS:
            created, updated = self._contact_service.upsert_batch(session, parsed.contacts)
            result.created = created
            result.updated = updated
        else:
            product_ids: dict[str, uuid.UUID] = {}
            for product in parsed.products:
                product_id, created = self._product_service.upsert(session, product)
                product_ids[product.internal_reference] = product_id
                if created:
                    result.created += 1
                else:
                    result.updated += 1
                if product.quantity is not None:
                    session.add(
                        StockSnapshot(
                            import_batch_id=batch.id,
                            product_id=product_id,
                            quantity=product.quantity,
                            source="ODOO_EXPORT",
                        )
                    )
            self._link_suppliers(session, parsed.products, product_ids, result)
            if parsed.import_type is ImportBatchType.MIXED_PRODUCT_EXPORT:
                for movement in parsed.movements:
                    movement_product_id = product_ids.get(movement.product_internal_ref)
                    if movement_product_id is None:
                        continue
                    session.add(
                        StockMovement(
                            import_batch_id=batch.id,
                            product_id=movement_product_id,
                            movement_description=movement.description,
                            quantity=movement.quantity,
                            scheduled_at=movement.scheduled_at,
                            raw_data=movement.raw,
                        )
                    )
        self._record_errors(session, batch, parsed, result)
        result.error_count += len(result.missing_refs)
        return result

    def _link_suppliers(
        self,
        session: Session,
        products: list[ParsedProduct],
        product_ids: dict[str, uuid.UUID],
        result: _PersistResult,
    ) -> None:
        refs = {p.supplier_ref for p in products if p.supplier_ref}
        contact_ids: dict[str, uuid.UUID] = {}
        for ref in refs:
            contact_id = self._product_service.resolve_contact_id(session, ref)
            if contact_id is not None:
                contact_ids[ref] = contact_id
        result.missing_refs = sorted(refs - set(contact_ids))
        for product in products:
            if product.supplier_ref and product.supplier_ref in contact_ids:
                self._product_service.link_supplier(
                    session,
                    product_ids[product.internal_reference],
                    contact_ids[product.supplier_ref],
                    product.supplier_ref,
                )

    def _record_errors(
        self, session: Session, batch: ImportBatch, parsed: ParsedData, result: _PersistResult
    ) -> None:
        for error in parsed.errors:
            session.add(
                ImportErrorRecord(
                    import_batch_id=batch.id,
                    sheet_name=None,
                    row_number=error.row_number,
                    column_name=error.column_name,
                    error_code=error.error_code,
                    message=error.message,
                    raw_value=error.raw_value,
                )
            )
            result.error_count += 1
        for ref in result.missing_refs:
            session.add(
                ImportErrorRecord(
                    import_batch_id=batch.id,
                    sheet_name=None,
                    row_number=None,
                    column_name="Proveedores/Proveedor/Referencia",
                    error_code="MISSING_CONTACT_REFERENCE",
                    message=f"Referencia de proveedor '{ref}' no existe en contactos.",
                )
            )
