"""Tipos base del pipeline de importacion (sin persistencia)."""

from __future__ import annotations

import dataclasses
import datetime as dt
import decimal

from app.models.enums import ImportBatchType


@dataclasses.dataclass(frozen=True)
class RowError:
    """Error recuperable asociado a una fila/columna."""

    row_number: int | None
    column_name: str | None
    error_code: str
    message: str
    raw_value: str | None = None


@dataclasses.dataclass(frozen=True)
class ParsedContact:
    external_ref: str | None
    name: str
    raw_external_id: str | None


@dataclasses.dataclass(frozen=True)
class ParsedProduct:
    internal_reference: str
    name: str
    sale_price: decimal.Decimal | None
    cost: decimal.Decimal | None
    consignment_cost: decimal.Decimal | None
    quantity: decimal.Decimal | None
    supplier_ref: str | None = None
    supplier_name: str | None = None


@dataclasses.dataclass(frozen=True)
class ParsedMovement:
    product_internal_ref: str
    description: str | None
    quantity: decimal.Decimal
    scheduled_at: dt.datetime | None
    raw: dict[str, object]


@dataclasses.dataclass
class ParsedData:
    """Resultado de parsear un archivo (todas las entidades + errores)."""

    import_type: ImportBatchType
    contacts: list[ParsedContact] = dataclasses.field(default_factory=list)
    products: list[ParsedProduct] = dataclasses.field(default_factory=list)
    movements: list[ParsedMovement] = dataclasses.field(default_factory=list)
    errors: list[RowError] = dataclasses.field(default_factory=list)
    duplicates: list[str] = dataclasses.field(default_factory=list)
    total_rows: int = 0


@dataclasses.dataclass
class ImportSummary:
    """Resumen de un preview / dry-run / import real."""

    import_type: ImportBatchType
    source_filename: str
    source_sha256: str
    total_rows: int = 0
    contacts_detected: int = 0
    products_detected: int = 0
    movements_detected: int = 0
    stock_snapshots_detected: int = 0
    missing_contact_refs: list[str] = dataclasses.field(default_factory=list)
    duplicate_product_codes: list[str] = dataclasses.field(default_factory=list)
    invalid_rows: int = 0
    warnings: list[str] = dataclasses.field(default_factory=list)
    errors: list[RowError] = dataclasses.field(default_factory=list)
    created: int = 0
    updated: int = 0
    skipped: int = 0
    products_with_sale_price: int = 0
    products_with_cost: int = 0
    products_with_consignment_cost: int = 0
    products_using_cost: int = 0
    products_using_consignment_cost: int = 0
    products_with_zero_effective_cost: int = 0
    already_imported: bool = False
    batch_id: str | None = None


class ImportError(Exception):
    """Error fatal de importacion (transaccion completa en ROLLBACK)."""


class UnknownImportTypeError(ImportError):
    """No se pudo determinar el tipo de archivo a partir de los encabezados."""


class MissingHeaderError(ImportError):
    """Falta una columna obligatoria en el archivo."""


class InvalidFileError(ImportError):
    """El archivo no es un XLSX valido o no se puede leer."""
