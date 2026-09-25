"""Export de ajuste de inventario para ERP via perfil configurable (F010E).

No existe plantilla oficial de vendor en el repositorio: el perfil canonico
``CANONICAL_INVENTORY_ADJUSTMENT`` es el unico soportado y su generacion
reporta siempre ``ERP_VENDOR_TEMPLATE_NOT_PROVIDED``.
"""

from __future__ import annotations

import csv
import io
from typing import Any

from openpyxl import Workbook

from app.documents.engine.context import InventoryDocContext
from app.documents.engine.formatting import excel_safe, format_date, format_number, to_decimal
from app.documents.engine.xlsx import to_bytes, write_sheet

CANONICAL_PROFILE_CODE = "CANONICAL_INVENTORY_ADJUSTMENT"
ERP_VENDOR_TEMPLATE_PROVIDED = False
VENDOR_TEMPLATE_WARNING = "ERP_VENDOR_TEMPLATE_NOT_PROVIDED"

CANONICAL_COLUMNS = (
    "campaign_code",
    "product_code",
    "product_name",
    "expected_quantity",
    "approved_physical_quantity",
    "adjustment_quantity",
    "reason",
)

_PROFILE_DEFAULTS: dict[str, Any] = {
    "code": CANONICAL_PROFILE_CODE,
    "version": "1",
    "document_format": "XLSX",
    "column_order": None,
    "column_mapping": None,
    "file_extension": "xlsx",
    "field_separator": None,
    "decimal_separator": ".",
    "thousands_separator": "",
    "currency": None,
    "date_format": None,
    "document_number_format": None,
    "encoding": None,
    "requires_vendor_template": False,
}


def _fields_for(
    row: dict[str, Any], ctx: InventoryDocContext, profile: dict[str, Any]
) -> dict[str, Any]:
    campaign = ctx.valuation.get("campaign") or ctx.campaign
    product = row.get("product") or {}
    expected = to_decimal(row.get("expected_quantity"))
    physical = to_decimal(row.get("approved_physical_quantity"))
    adjustment: int | str = ""
    if expected is not None and physical is not None:
        delta = physical - expected
        adjustment = int(delta) if delta == delta.to_integral_value() else str(delta)
    date_format = profile.get("date_format") or ctx.branding.date_format
    number_format = profile.get("document_number_format")
    document_number = ctx.document_number
    if number_format:
        document_number = number_format.replace("{document_number}", ctx.document_number)
    return {
        "campaign_code": campaign.get("code"),
        "product_code": product.get("internal_reference"),
        "product_name": product.get("name"),
        "expected_quantity": expected,
        "approved_physical_quantity": physical,
        "adjustment_quantity": adjustment,
        "reason": row.get("reason"),
        "observation": row.get("observation"),
        "document_number": document_number,
        "generated_date": format_date(ctx.generated_at.isoformat(), date_format),
    }


def _ordered_columns(profile: dict[str, Any]) -> list[str]:
    order = profile.get("column_order")
    if not order:
        return list(CANONICAL_COLUMNS)
    return [str(field) for field in order]


def _headers(columns: list[str], profile: dict[str, Any]) -> list[str]:
    mapping = profile.get("column_mapping") or {}
    return [str(mapping.get(column, column)) for column in columns]


def _format_number(value: Any, profile: dict[str, Any]) -> Any:
    number = to_decimal(value)
    if number is None:
        return value
    decimal_separator = profile.get("decimal_separator") or "."
    thousands_separator = profile.get("thousands_separator") or ""
    decimals = 4 if number != number.to_integral_value() else 0
    return format_number(
        number,
        decimals=decimals,
        decimal_separator=decimal_separator,
        thousands_separator=thousands_separator,
    )


def _cell_value(column: str, fields: dict[str, Any], profile: dict[str, Any]) -> Any:
    value = fields.get(column)
    if column in ("expected_quantity", "approved_physical_quantity"):
        return _format_number(value, profile)
    if column == "adjustment_quantity" and isinstance(value, int):
        return _format_number(value, profile)
    return value


def render(ctx: InventoryDocContext, *, profile: dict[str, Any]) -> bytes:
    """Renderiza el ajuste ERP (XLSX o CSV) segun el perfil activo."""
    merged = {**_PROFILE_DEFAULTS, **{k: v for k, v in profile.items() if v is not None}}
    columns = _ordered_columns(merged)
    headers = _headers(columns, merged)
    rows = [
        [_cell_value(column, _fields_for(row, ctx, merged), merged) for column in columns]
        for row in ctx.reconciliation_rows
    ]

    if str(merged.get("document_format", "XLSX")).upper() == "CSV":
        separator = merged.get("field_separator") or ","
        encoding = merged.get("encoding") or "utf-8"
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer, delimiter=separator, lineterminator="\r\n")
        writer.writerow([excel_safe(header) for header in headers])
        for row in rows:
            writer.writerow([excel_safe("" if value is None else value) for value in row])
        return buffer.getvalue().encode(encoding)

    workbook = Workbook()
    active_sheet = workbook.active
    if active_sheet is not None:
        workbook.remove(active_sheet)
    sheet_name = str(merged.get("code") or "Ajuste_ERP")
    write_sheet(workbook, sheet_name, headers, rows)
    return to_bytes(workbook)
