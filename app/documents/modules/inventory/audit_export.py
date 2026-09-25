"""Export de auditoria de inventario en XLSX con 10 hojas (F010D).

Anti formula-injection: todo texto pasa por ``excel_safe`` via ``write_sheet``.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from openpyxl import Workbook

from app.documents.engine.context import InventoryDocContext
from app.documents.engine.xlsx import to_bytes, write_sheet

SHEET_NAMES = (
    "Resumen",
    "Conciliacion",
    "Sesiones",
    "Conteos",
    "Eventos",
    "Danos",
    "Extras",
    "Unknowns",
    "Reconteos",
    "Auditoria",
)


def _product_columns(item: dict[str, Any]) -> tuple[str, str]:
    product = item.get("product") or {}
    return (str(product.get("internal_reference") or ""), str(product.get("name") or ""))


def _datetime_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value


def _summary_rows(ctx: InventoryDocContext, extra: dict[str, Any]) -> list[list[Any]]:
    valuation = ctx.valuation
    summary = valuation.get("summary") or {}
    campaign = valuation.get("campaign") or ctx.campaign
    rows: list[list[Any]] = [
        ["campana_code", campaign.get("code")],
        ["campana_name", campaign.get("name")],
        ["campana_status", campaign.get("status")],
        ["campana_version", campaign.get("version")],
        ["approved_at", _datetime_value(campaign.get("approved_at"))],
        ["valuation_calculated_at", _datetime_value(valuation.get("calculated_at"))],
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
        ["document_number", ctx.document_number],
        ["document_type", ctx.document_type],
        ["title", ctx.title],
        ["generated_at", ctx.generated_at],
        ["generated_by", ctx.generated_by_label],
        ["organization_legal_name", ctx.branding.legal_name],
        ["organization_tax_id", ctx.branding.tax_id],
        ["organization_tax_id_label", ctx.branding.tax_id_label],
        ["currency_style", ctx.branding.currency_style],
        ["date_format", ctx.branding.date_format],
        ["branding_settings_version", ctx.branding.settings_version],
        ["branding_logo_sha256", ctx.branding.logo_sha256],
        ["sessions_count", len(ctx.sessions)],
        ["counts_count", len(ctx.counts)],
        ["events_count", len(ctx.events)],
        ["damages_count", len(ctx.damages)],
        ["extras_count", len(ctx.extras)],
        ["unknowns_count", len(ctx.unknowns)],
        ["recounts_count", len(ctx.recounts)],
        ["audit_events_count", len(ctx.audit_events)],
        ["warnings", ", ".join(ctx.warnings)],
    ]
    rows.extend([[key, value] for key, value in extra.items()])
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


def _count_rows(counts: list[dict[str, Any]]) -> list[list[Any]]:
    return [
        [
            count.get("session_number"),
            count.get("product_reference"),
            count.get("product_name"),
            count.get("quantity"),
            count.get("damaged_quantity"),
            count.get("updated_at"),
        ]
        for count in counts
    ]


def _damage_rows(damages: list[dict[str, Any]]) -> list[list[Any]]:
    return [
        [
            damage.get("session_number"),
            damage.get("product_reference"),
            damage.get("scanned_code"),
            damage.get("action"),
            damage.get("quantity"),
            damage.get("reason"),
            damage.get("observation"),
            damage.get("event_id"),
            damage.get("created_by"),
            damage.get("created_at"),
            damage.get("has_evidence"),
        ]
        for damage in damages
    ]


def _extra_rows(extras: list[dict[str, Any]]) -> list[list[Any]]:
    return [
        [
            extra.get("session_number"),
            extra.get("product_reference"),
            extra.get("product_name"),
            extra.get("quantity"),
            extra.get("first_detected_at"),
        ]
        for extra in extras
    ]


def _unknown_rows(unknowns: list[dict[str, Any]]) -> list[list[Any]]:
    return [
        [
            unknown.get("session_number"),
            unknown.get("scanned_code"),
            unknown.get("quantity"),
            unknown.get("damaged_quantity"),
            unknown.get("resolved_product_reference"),
            unknown.get("resolved_by"),
            unknown.get("resolved_at"),
        ]
        for unknown in unknowns
    ]


def _recount_rows(recounts: list[dict[str, Any]]) -> list[list[Any]]:
    return [
        [
            recount.get("status"),
            recount.get("requested_by"),
            recount.get("assigned_to"),
            recount.get("source_session_number"),
            recount.get("resulting_session_number"),
            recount.get("reason"),
            recount.get("started_at"),
            recount.get("completed_at"),
            recount.get("cancelled_at"),
            recount.get("cancel_reason"),
        ]
        for recount in recounts
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


def render(ctx: InventoryDocContext, *, extra_metadata: dict[str, Any]) -> bytes:
    """Renderiza el workbook de auditoria con las 10 hojas requeridas."""
    workbook = Workbook()
    active_sheet = workbook.active
    if active_sheet is not None:
        workbook.remove(active_sheet)
    write_sheet(workbook, "Resumen", ["Campo", "Valor"], _summary_rows(ctx, extra_metadata))
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
        ["Numero", "Tipo", "Estado", "Inicio", "Envio", "Dispositivo", "Responsable"],
        _session_rows(ctx.sessions),
    )
    write_sheet(
        workbook,
        "Conteos",
        ["Sesion", "Referencia", "Producto", "Cantidad fisica", "Danadas", "Actualizado"],
        _count_rows(ctx.counts),
    )
    write_sheet(
        workbook,
        "Eventos",
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
        "Danos",
        [
            "Sesion",
            "Referencia",
            "Codigo",
            "Accion",
            "Cantidad",
            "Motivo",
            "Observacion",
            "Evento ID",
            "Registrado por",
            "Creado",
            "Tiene evidencia",
        ],
        _damage_rows(ctx.damages),
    )
    write_sheet(
        workbook,
        "Extras",
        ["Sesion", "Referencia", "Producto", "Cantidad", "Detectado"],
        _extra_rows(ctx.extras),
    )
    write_sheet(
        workbook,
        "Unknowns",
        [
            "Sesion",
            "Codigo",
            "Cantidad",
            "Danadas",
            "Producto resuelto",
            "Resuelto por",
            "Resuelto",
        ],
        _unknown_rows(ctx.unknowns),
    )
    write_sheet(
        workbook,
        "Reconteos",
        [
            "Estado",
            "Solicitado por",
            "Asignado a",
            "Sesion origen",
            "Sesion resultado",
            "Motivo",
            "Inicio",
            "Completado",
            "Cancelado",
            "Motivo cancelacion",
        ],
        _recount_rows(ctx.recounts),
    )
    write_sheet(
        workbook,
        "Auditoria",
        ["Accion", "Actor", "Entidad", "Entidad ID", "Momento", "Detalle"],
        _audit_rows(ctx.audit_events),
    )
    return to_bytes(workbook)
