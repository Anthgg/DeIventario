"""Informe de gestion de inventario en PDF (F010C).

Lee la valorizacion persistida (F009) como unica fuente de dinero: aqui NO se
recalculan costos ni cantidades.
"""

from __future__ import annotations

import decimal
from typing import Any

from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Flowable, Paragraph, Spacer, Table, TableStyle

from app.documents.engine.branding import BrandingSnapshot
from app.documents.engine.context import InventoryDocContext
from app.documents.engine.formatting import format_date, format_money, format_quantity, to_decimal
from app.documents.templates.base import brand_color, render_pdf

_TITLE_STYLE = ParagraphStyle(
    "docTitle", fontName="Helvetica-Bold", fontSize=16, alignment=1, leading=20
)
_SUBTITLE_STYLE = ParagraphStyle(
    "docSubtitle", fontName="Helvetica", fontSize=9, alignment=1, textColor=colors.grey
)
_HEADING_STYLE = ParagraphStyle(
    "docHeading", fontName="Helvetica-Bold", fontSize=11, spaceBefore=8, spaceAfter=4
)
_BODY_STYLE = ParagraphStyle("docBody", fontName="Helvetica", fontSize=9, leading=12)
_SMALL_STYLE = ParagraphStyle("docSmall", fontName="Helvetica", fontSize=8, leading=10)
_CELL_STYLE = ParagraphStyle("docCell", fontName="Helvetica", fontSize=7.5, leading=9)


def _money(value: Any, currency: str | None, branding: BrandingSnapshot) -> str:
    return format_money(value, currency, branding.currency_style)


def _heading(text: str) -> Paragraph:
    return Paragraph(text, _HEADING_STYLE)


def _cell(text: object) -> Paragraph:
    return Paragraph(str(text), _CELL_STYLE)


def _table(header: list[str], rows: list[list[object]], widths: list[float]) -> Table:
    data: list[list[object]] = [[_cell(h) for h in header]]
    data.extend([[_cell(value) for value in row] for row in rows])
    table = Table(data, colWidths=widths, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EEEEEE")),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    return table


def _campaign_table(campaign: dict[str, Any], valuation: dict[str, Any]) -> Table:
    rows = [
        [_cell("Campana"), _cell(f"{campaign.get('code')} - {campaign.get('name')}")],
        [_cell("Estado"), _cell(f"{campaign.get('status')} (version {campaign.get('version')})")],
        [
            _cell("Aprobada / Valorizada"),
            _cell(
                f"{campaign.get('approved_at') or ''} / "
                f"{campaign.get('valuation_calculated_at') or valuation.get('calculated_at') or ''}"
            ),
        ],
        [_cell("Moneda"), _cell(valuation.get("currency") or "")],
    ]
    table = Table(rows, colWidths=[45 * mm, 125 * mm])
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F5F5F5")),
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    return table


def _summary_table(valuation: dict[str, Any], branding: BrandingSnapshot) -> Table:
    summary = valuation.get("summary") or {}
    currency = valuation.get("currency")
    rows: list[list[object]] = [
        ["Unidades faltantes", format_quantity(summary.get("total_missing_units"))],
        ["Unidades danadas", format_quantity(summary.get("total_damaged_units"))],
        ["Unidades excedentes", format_quantity(summary.get("total_surplus_units"))],
        ["Valor de faltantes", _money(summary.get("total_missing_cost"), currency, branding)],
        ["Valor de dano", _money(summary.get("total_damage_cost"), currency, branding)],
        ["Valor de excedentes", _money(summary.get("total_surplus_cost"), currency, branding)],
        [
            "Valor de venta afectado",
            _money(summary.get("total_affected_sale_value"), currency, branding),
        ],
        [
            "Perdida confirmada",
            _money(summary.get("total_confirmed_loss_cost"), currency, branding),
        ],
    ]
    return _table(["Concepto", "Valor"], rows, [90 * mm, 70 * mm])


def _top_differences(
    valuation: dict[str, Any], branding: BrandingSnapshot, limit: int = 20
) -> Table | None:
    items = [item for item in valuation.get("items") or [] if _negative_difference(item)]
    items.sort(key=_missing_cost_key, reverse=True)
    if not items:
        return None
    currency = valuation.get("currency")
    rows: list[list[object]] = []
    for item in items[:limit]:
        product = item.get("product") or {}
        rows.append(
            [
                product.get("internal_reference"),
                product.get("name"),
                format_quantity(item.get("expected_quantity")),
                format_quantity(item.get("approved_physical_quantity")),
                format_quantity(item.get("missing_quantity")),
                _money(item.get("missing_cost_value"), currency, branding),
            ]
        )
    return _table(
        ["Referencia", "Producto", "Esperado", "Fisico", "Faltante", "Valor faltante"],
        rows,
        [24 * mm, 52 * mm, 18 * mm, 18 * mm, 18 * mm, 30 * mm],
    )


def _negative_difference(item: dict[str, Any]) -> bool:
    missing = to_decimal(item.get("missing_quantity"))
    return missing is not None and missing > 0


def _missing_cost_key(item: dict[str, Any]) -> decimal.Decimal:
    return abs(to_decimal(item.get("missing_cost_value")) or decimal.Decimal(0))


def _damaged_table(valuation: dict[str, Any], branding: BrandingSnapshot) -> Table | None:
    items = [
        item
        for item in valuation.get("items") or []
        if (to_decimal(item.get("damaged_quantity")) or 0) > 0
    ]
    if not items:
        return None
    currency = valuation.get("currency")
    rows = []
    for item in items[:25]:
        product = item.get("product") or {}
        rows.append(
            [
                product.get("internal_reference"),
                product.get("name"),
                format_quantity(item.get("damaged_quantity")),
                _money(item.get("damage_cost_value"), currency, branding),
            ]
        )
    return _table(
        ["Referencia", "Producto", "Danadas", "Valor dano"],
        rows,
        [26 * mm, 66 * mm, 22 * mm, 36 * mm],
    )


def _surplus_table(valuation: dict[str, Any], branding: BrandingSnapshot) -> Table | None:
    items = [
        item
        for item in valuation.get("items") or []
        if (to_decimal(item.get("surplus_quantity")) or 0) > 0
    ]
    if not items:
        return None
    currency = valuation.get("currency")
    rows = []
    for item in items[:25]:
        product = item.get("product") or {}
        rows.append(
            [
                product.get("internal_reference"),
                product.get("name"),
                format_quantity(item.get("surplus_quantity")),
                _money(item.get("surplus_cost_value"), currency, branding),
            ]
        )
    return _table(
        ["Referencia", "Producto", "Excedente", "Valor excedente"],
        rows,
        [26 * mm, 66 * mm, 22 * mm, 36 * mm],
    )


def render(ctx: InventoryDocContext) -> bytes:
    """Renderiza el informe de gestion en PDF."""
    branding = ctx.branding
    valuation = ctx.valuation
    campaign = valuation.get("campaign") or ctx.campaign
    generated_date = format_date(ctx.generated_at.isoformat(), branding.date_format)
    flowables: list[Flowable] = [
        Paragraph(ctx.title, _TITLE_STYLE),
        Paragraph(
            f"{ctx.document_number} - {generated_date}",
            _SUBTITLE_STYLE,
        ),
        Spacer(1, 6 * mm),
        _campaign_table(campaign, valuation),
        _heading("Resumen ejecutivo"),
        _summary_table(valuation, branding),
    ]

    differences = _top_differences(valuation, branding)
    if differences is not None:
        flowables.append(_heading("Principales diferencias por valor de faltantes"))
        flowables.append(differences)

    damaged = _damaged_table(valuation, branding)
    if damaged is not None:
        flowables.append(_heading("Productos con dano registrado"))
        flowables.append(damaged)

    surplus = _surplus_table(valuation, branding)
    if surplus is not None:
        flowables.append(_heading("Productos con excedente"))
        flowables.append(surplus)

    warnings = list(valuation.get("warnings") or []) + list(ctx.warnings)
    if warnings:
        flowables.append(_heading("Advertencias"))
        for warning in warnings:
            flowables.append(Paragraph(f"- {warning}", _BODY_STYLE))

    summary = valuation.get("summary") or {}
    currency = valuation.get("currency")
    confirmed_loss = _money(
        summary.get("total_confirmed_loss_cost"), currency, branding
    )
    flowables.append(_heading("Conclusiones"))
    flowables.append(
        Paragraph(
            "Conforme a la valorizacion persistida de la conciliacion aprobada, la campana "
            f"{campaign.get('code')} registra "
            f"{format_quantity(summary.get('total_missing_units'))} unidades faltantes, "
            f"{format_quantity(summary.get('total_damaged_units'))} unidades danadas y "
            f"{format_quantity(summary.get('total_surplus_units'))} unidades excedentes, "
            f"por una perdida confirmada de {confirmed_loss}.",
            _BODY_STYLE,
        )
    )
    flowables.append(Spacer(1, 10 * mm))
    signers = [
        ctx.generated_by_label or "",
        "",
        "",
    ]
    signature_rows = [
        [_cell("Generado por"), _cell("Revisado por"), _cell("Visto bueno")],
        [
            _cell(signers[0]),
            _cell("________________________"),
            _cell("________________________"),
        ],
        [
            _cell(format_date(ctx.generated_at.isoformat(), branding.date_format)),
            _cell("Fecha: ______________"),
            _cell("Fecha: ______________"),
        ],
    ]
    signature_table = Table(signature_rows, colWidths=[55 * mm, 55 * mm, 55 * mm])
    signature_table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    flowables.append(signature_table)

    accent = brand_color(branding.brand_color_accent, colors.HexColor("#333333"))
    flowables.append(Spacer(1, 4 * mm))
    accent_text = f"<font color='#{accent.hexval()[2:]}'>"
    flowables.append(
        Paragraph(
            f"{accent_text}Documento oficial generado por Inventario Dedalo</font>",
            _SMALL_STYLE,
        )
    )

    return render_pdf(
        flowables=flowables,
        branding=branding,
        title=ctx.title,
        generated_at=ctx.generated_at,
        document_number=ctx.document_number,
    )
