"""Parsers del export de productos (maestro plano y mixto con movimientos)."""

from __future__ import annotations

import datetime as dt
import decimal

import openpyxl

from app.importers import mappings
from app.importers.base import ParsedMovement, ParsedProduct, RowError
from app.importers.excel_reader import detect_header, first_sheet, iter_data_rows
from app.importers.validators import clean_text, parse_datetime_aware, parse_decimal

_DEFAULT_CURRENCY = "PEN"


def _get(values: list[object], columns: dict[str, int], header: str) -> object:
    idx = columns.get(header)
    if idx is None or idx >= len(values):
        return None
    return values[idx]


def _decimal(
    values: list[object], columns: dict[str, int], header: str, row_number: int
) -> tuple[decimal.Decimal | None, RowError | None]:
    value = _get(values, columns, header)
    if value is None:
        return None, None
    parsed, error_code = parse_decimal(value)
    if error_code is not None:
        return None, RowError(
            row_number, header, error_code, f"Valor no numerico en '{header}'.",
            clean_text(value),
        )
    return parsed, None


def _product_common(
    values: list[object], columns: dict[str, int], row_number: int
) -> tuple[ParsedProduct, list[RowError]]:
    errors: list[RowError] = []
    internal = clean_text(_get(values, columns, mappings.PRODUCT_HEADER_INTERNAL_REF))
    name = clean_text(_get(values, columns, mappings.PRODUCT_HEADER_NAME))
    sale_price, err = _decimal(values, columns, mappings.PRODUCT_HEADER_SALE_PRICE, row_number)
    if err:
        errors.append(err)
    cost, err = _decimal(values, columns, mappings.PRODUCT_HEADER_COST, row_number)
    if err:
        errors.append(err)
    consignment, err = _decimal(values, columns, mappings.PRODUCT_HEADER_CONSIGNMENT, row_number)
    if err:
        errors.append(err)
    quantity, err = _decimal(values, columns, mappings.PRODUCT_HEADER_QTY, row_number)
    if err:
        errors.append(err)
    product = ParsedProduct(
        internal_reference=internal,
        name=name,
        sale_price=sale_price,
        cost=cost,
        consignment_cost=consignment,
        quantity=quantity,
    )
    return product, errors


def parse_product_master(
    workbook: openpyxl.Workbook,
) -> tuple[list[ParsedProduct], list[RowError], int, list[str]]:
    """Parsea el export plano de productos (con Costo).

    Devuelve (productos, errores, filas totales, referencias duplicadas).
    """
    sheet = first_sheet(workbook)
    header_row, headers = detect_header(sheet)
    columns = mappings.build_column_map(
        headers,
        required=(mappings.PRODUCT_HEADER_INTERNAL_REF, mappings.PRODUCT_HEADER_NAME),
        optional=(
            mappings.PRODUCT_HEADER_SALE_PRICE,
            mappings.PRODUCT_HEADER_COST,
            mappings.PRODUCT_HEADER_CONSIGNMENT,
            mappings.PRODUCT_HEADER_QTY,
        ),
    )
    products: list[ParsedProduct] = []
    errors: list[RowError] = []
    duplicates: list[str] = []
    seen: set[str] = set()
    total = 0
    for row_number, values in iter_data_rows(sheet, header_row):
        total += 1
        if not any(clean_text(v) for v in values):
            continue  # fila vacia
        internal = clean_text(_get(values, columns, mappings.PRODUCT_HEADER_INTERNAL_REF))
        if not internal:
            errors.append(
                RowError(
                    row_number,
                    mappings.PRODUCT_HEADER_INTERNAL_REF,
                    "MISSING_INTERNAL_REFERENCE",
                    "Producto sin referencia interna.",
                )
            )
            continue
        if internal in seen:
            duplicates.append(internal)
        seen.add(internal)
        product, product_errors = _product_common(values, columns, row_number)
        errors.extend(product_errors)
        products.append(product)
    return products, errors, total, duplicates


def parse_mixed_product_export(
    workbook: openpyxl.Workbook,
) -> tuple[list[ParsedProduct], list[ParsedMovement], list[RowError], int, list[str]]:
    """Parsea el export mixto jerarquico (producto + filas hijas de movimientos)."""
    sheet = first_sheet(workbook)
    header_row, headers = detect_header(sheet)
    columns = mappings.build_column_map(
        headers,
        required=(mappings.PRODUCT_HEADER_INTERNAL_REF, mappings.PRODUCT_HEADER_NAME),
        optional=(
            mappings.PRODUCT_HEADER_SALE_PRICE,
            mappings.PRODUCT_HEADER_CONSIGNMENT,
            mappings.PRODUCT_HEADER_QTY,
            mappings.MIXED_HEADER_SUPPLIER_REF,
            mappings.MIXED_HEADER_SUPPLIER_NAME,
            mappings.MIXED_HEADER_MOVEMENT,
            mappings.MIXED_HEADER_MOVEMENT_QTY,
            mappings.MIXED_HEADER_MOVEMENT_SCHEDULED,
        ),
    )
    products: list[ParsedProduct] = []
    movements: list[ParsedMovement] = []
    errors: list[RowError] = []
    duplicates: list[str] = []
    seen: set[str] = set()
    current_ref: str | None = None
    total = 0
    for row_number, values in iter_data_rows(sheet, header_row):
        total += 1
        internal = clean_text(_get(values, columns, mappings.PRODUCT_HEADER_INTERNAL_REF))
        if internal:
            if internal in seen:
                duplicates.append(internal)
            seen.add(internal)
            product, product_errors = _product_common(values, columns, row_number)
            errors.extend(product_errors)
            supplier_ref = clean_text(_get(values, columns, mappings.MIXED_HEADER_SUPPLIER_REF))
            supplier_name = clean_text(_get(values, columns, mappings.MIXED_HEADER_SUPPLIER_NAME))
            products.append(
                ParsedProduct(
                    internal_reference=product.internal_reference,
                    name=product.name,
                    sale_price=product.sale_price,
                    cost=product.cost,
                    consignment_cost=product.consignment_cost,
                    quantity=product.quantity,
                    supplier_ref=supplier_ref or None,
                    supplier_name=supplier_name or None,
                )
            )
            current_ref = internal
            continue

        # Fila hija: movimiento asociado al ultimo producto.
        description = clean_text(_get(values, columns, mappings.MIXED_HEADER_MOVEMENT))
        quantity, qty_error = _decimal(
            values, columns, mappings.MIXED_HEADER_MOVEMENT_QTY, row_number
        )
        if qty_error:
            errors.append(qty_error)
        scheduled, dt_error = _parse_scheduled(values, columns, row_number)
        if dt_error:
            errors.append(dt_error)
        if description or quantity is not None:
            if current_ref is None:
                errors.append(
                    RowError(
                        row_number,
                        mappings.MIXED_HEADER_MOVEMENT,
                        "ORPHAN_MOVEMENT",
                        "Movimiento sin producto padre.",
                    )
                )
            else:
                movements.append(
                    ParsedMovement(
                        product_internal_ref=current_ref,
                        description=description or None,
                        quantity=quantity if quantity is not None else decimal.Decimal("0"),
                        scheduled_at=scheduled,
                        raw={
                            "movement": description,
                            "quantity": str(quantity),
                            "scheduled_at": str(scheduled),
                        },
                    )
                )
    return products, movements, errors, total, duplicates


def _parse_scheduled(
    values: list[object], columns: dict[str, int], row_number: int
) -> tuple[dt.datetime | None, RowError | None]:
    value = _get(values, columns, mappings.MIXED_HEADER_MOVEMENT_SCHEDULED)
    if value is None:
        return None, None
    parsed, error_code = parse_datetime_aware(value)
    if error_code is not None:
        return None, RowError(
            row_number,
            mappings.MIXED_HEADER_MOVEMENT_SCHEDULED,
            error_code,
            "Fecha programada no valida.",
            clean_text(value),
        )
    return parsed, None
