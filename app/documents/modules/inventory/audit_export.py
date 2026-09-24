"""Export de auditoria de inventario en XLSX con 10 hojas (F010D).

Anti formula-injection: todo texto pasa por ``excel_safe`` via ``write_sheet``.
"""

from __future__ import annotations

from typing import Any

from openpyxl import Workbook

from app.documents.engine.context import InventoryDocContext
from app.documents.engine.formatting import to_decimal
from app.documents.engine.xlsx import to_bytes, write_sheet

SHEET_NAMES = (
    "Resumen",
    "Valorizacion",
    "Diferencias",
    "Dannos",
    "Excedentes",
    "Conciliacion",
    "Sesiones",
    "Eventos_Conteo",
    "Rastreo",
    "Metadatos",
)


def _product_columns(item: dict[str, Any]) -> tuple[str, str]:
    product = item.get("product") or {}
    return (str(product.get("internal_reference") or ""), str(product.get("name") or ""))


def _valuation_rows(items: list[dict[str, Any]]) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for item in items:
        ref, name = _product_columns(item)
        rows.append(
            [
                ref,
                name,
                item.get("expected_quantity"),
                item.get("approved_physical_quantity"),
                item.get("missing_quantity"),
                item.get("surplus_quantity"),
                item.get("damaged_quantity"),
                item.get("effective_unit_cost"),
                item.get("currency"),
                item.get("missing_cost_value"),
                item.get("damage_cost_value"),
                item.get("surplus_cost_value"),
                item.get("affected_sale_value"),
            ]
        )
    return rows


def _summary_rows(ctx: InventoryDocContext) -> list[list[Any]]:
    valuation = ctx.valuation
    summary = valuation.get("summary") or {}
    campaign = valuation.get("campaign") or ctx.campaign
    return [
        ["campana_code", campaign.get("code")],
        ["campana_name", campaign.get("name")],
        ["campana_status", campaign.get("status")],
        ["campana_version", campaign.get("version")],
        ["approved_at", campaign.get("approved_at")],
        ["valuation_calculated_at", valuation.get("calculated_at")],
        ["currency", valuation.get("currency")],
        ["items_count", len(valuation.get("items") or [])],
        ["total_missing_units", summary.get("total_missing_units")],
        ["total_damaged_units", summary.get("total_damaged_units")],
        ["total_surplus_units", summary.get("total_surplus_units")],
        ["total_missing_cost", summary.get("total_missing_cost")],
        ["total_damage_cost", summary.get("total_damage_cost")],
        ["total_surplus_cost", summary.get("total_surplus_cost")],
        ["total_affected_sale_value", summary.get("total_affected_sale_value")],
        ["total_confirmed_loss_cost", summary.get("total_confirmed_loss_cost")],
        ["valuation_warnings", ", ".join(str(w) for w in valuation.get("warnings") or [])],
        ["document_warnings", ", ".join(ctx.warnings)],
    ]


def _difference_rows(items: list[dict[str, Any]]) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for item in items:
        missing = to_decimal(item.get("missing_quantity")) or 0
        surplus = to_decimal(item.get("surplus_quantity")) or 0
        if missing == 0 and surplus == 0:
            continue
        ref, name = _product_columns(item)
        rows.append(
            [
                ref,
                name,
                item.get("expected_quantity"),
                item.get("approved_physical_quantity"),
                item.get("missing_quantity"),
                item.get("surplus_quantity"),
                item.get("missing_cost_value"),
                item.get("surplus_cost_value"),
                item.get("currency"),
            ]
        )
    return rows


def _reconciliation_rows(rows: list[dict[str, Any]]) -> list[list[Any]]:
    output: list[list[Any]] = []
    for row in rows:
        product = row.get("product") or {}
        output.append(
            [
                product.get("internal_reference"),
                product.get("name"),
                row.get("expected_quantity"),
                row.get("approved_physical_quantity"),
                row.get("difference_quantity"),
                row.get("damaged_quantity"),
                row.get("missing_quantity"),
                row.get("surplus_quantity"),
                row.get("status"),
                row.get("reason"),
                row.get("observation"),
                row.get("effective_unit_cost"),
                row.get("missing_cost_value"),
                row.get("damage_cost_value"),
                row.get("surplus_cost_value"),
                row.get("affected_sale_value"),
            ]
        )
    return output


def _session_rows(sessions: list[dict[str, Any]]) -> list[list[Any]]:
    return [
        [
            session.get("session_number"),
            session.get("session_type"),
            session.get("status"),
            session.get("started_at"),
            session.get("submitted_at"),
            session.get("device_identifier"),
            session.get("user_display_name"),
        ]
        for session in sessions
    ]


def _event_rows(events: list[dict[str, Any]]) -> list[list[Any]]:
    return [
        [
            event.get("session_number"),
            event.get("server_sequence"),
            event.get("event_type"),
            event.get("product_reference"),
            event.get("scanned_code"),
            event.get("quantity"),
            event.get("damage_delta_quantity"),
            event.get("occurred_at"),
        ]
        for event in events
    ]


def _audit_rows(audit_events: list[dict[str, Any]]) -> list[list[Any]]:
    return [
        [
            event.get("action"),
            event.get("actor"),
            event.get("entity_type"),
            event.get("entity_id"),
            event.get("occurred_at"),
            event.get("metadata"),
        ]
        for event in audit_events
    ]


def _metadata_rows(ctx: InventoryDocContext, extra: dict[str, Any]) -> list[list[Any]]:
    branding = ctx.branding
    rows: list[list[Any]] = [
        ["document_number", ctx.document_number],
        ["document_type", ctx.document_type],
        ["title", ctx.title],
        ["generated_at", ctx.generated_at.isoformat()],
        ["generated_by", ctx.generated_by_label],
        ["organization_legal_name", branding.legal_name],
        ["organization_tax_id", branding.tax_id],
        ["organization_tax_id_label", branding.tax_id_label],
        ["currency_style", branding.currency_style],
        ["date_format", branding.date_format],
        ["branding_settings_version", branding.settings_version],
        ["branding_logo_sha256", branding.logo_sha256],
        ["items_count", len(ctx.valuation.get("items") or [])],
        ["sessions_count", len(ctx.sessions)],
        ["events_count", len(ctx.events)],
        ["audit_events_count", len(ctx.audit_events)],
        ["warnings", ", ".join(ctx.warnings)],
    ]
    for key, value in extra.items():
        rows.append([key, value])
    return rows


def render(ctx: InventoryDocContext, *, extra_metadata: dict[str, Any]) -> bytes:
    """Renderiza el workbook de auditoria con las 10 hojas requeridas."""
    workbook = Workbook()
    workbook.remove(workbook.active)
    items = list(ctx.valuation.get("items") or [])

    write_sheet(workbook, "Resumen", ["Campo", "Valor"], _summary_rows(ctx))
    write_sheet(
        workbook,
        "Valorizacion",
        [
            "Referencia",
            "Producto",
            "Esperado",
            "Fisico aprobado",
            "Faltante",
            "Excedente",
            "Danadas",
            "Costo unitario efectivo",
            "Moneda",
            "Valor faltante",
            "Valor dano",
            "Valor excedente",
            "Valor venta afectada",
        ],
        _valuation_rows(items),
    )
    write_sheet(
        workbook,
        "Diferencias",
        [
            "Referencia",
            "Producto",
            "Esperado",
            "Fisico aprobado",
            "Faltante",
            "Excedente",
            "Valor faltante",
            "Valor excedente",
            "Moneda",
        ],
        _difference_rows(items),
    )
    write_sheet(
        workbook,
        "Dannos",
        ["Referencia", "Producto", "Danadas", "Costo unitario", "Valor dano", "Moneda"],
        [
            [
                *_product_columns(item),
                item.get("damaged_quantity"),
                item.get("effective_unit_cost"),
                item.get("damage_cost_value"),
                item.get("currency"),
            ]
            for item in items
            if (to_decimal(item.get("damaged_quantity")) or 0) > 0
        ],
    )
    write_sheet(
        workbook,
        "Excedentes",
        ["Referencia", "Producto", "Excedente", "Valor excedente", "Moneda"],
        [
            [
                *_product_columns(item),
                item.get("surplus_quantity"),
                item.get("surplus_cost_value"),
                item.get("currency"),
            ]
            for item in items
            if (to_decimal(item.get("surplus_quantity")) or 0) > 0
        ],
    )
    write_sheet(
        workbook,
        "Conciliacion",
        [
            "Referencia",
            "Producto",
            "Esperado",
            "Fisico aprobado",
            "Diferencia",
            "Danadas",
            "Faltantes",
            "Excedentes",
            "Estado",
            "Motivo",
            "Observacion",
            "Costo unitario",
            "Valor faltante",
            "Valor dano",
            "Valor excedente",
            "Valor venta afectada",
        ],
        _reconciliation_rows(ctx.reconciliation_rows),
    )
    write_sheet(
        workbook,
        "Sesiones",
        [
            "Numero",
            "Tipo",
            "Estado",
            "Inicio",
            "Envio",
            "Dispositivo",
            "Responsable",
        ],
        _session_rows(ctx.sessions),
    )
    write_sheet(
        workbook,
        "Eventos_Conteo",
        [
            "Sesion",
            "Secuencia",
            "Tipo",
            "Referencia",
            "Codigo",
            "Cantidad",
            "Delta dano",
            "Ocurrido",
        ],
        _event_rows(ctx.events),
    )
    write_sheet(
        workbook,
        "Rastreo",
        ["Accion", "Actor", "Entidad", "Entidad ID", "Momento", "Detalle"],
        _audit_rows(ctx.audit_events),
    )
    write_sheet(
        workbook,
        "Metadatos",
        ["Campo", "Valor"],
        _metadata_rows(ctx, extra_metadata),
    )
    return to_bytes(workbook)
