"""Plantillas compartidas de PDF: encabezado, pie y paginacion (F010C).

Todo el branding (razon social, identificacion, colores, logo, footer) viene
del snapshot configurable; aqui NO hay literales de marca.
"""

from __future__ import annotations

import datetime as dt
import io
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as pdf_canvas
from reportlab.platypus import Flowable, SimpleDocTemplate

from app.documents.engine.branding import BrandingSnapshot

_PAGE_WIDTH, _PAGE_HEIGHT = A4
_LEFT = 16 * mm
_RIGHT = 16 * mm
_TOP = 30 * mm
_BOTTOM = 24 * mm


def brand_color(hex_value: str | None, fallback: colors.Color) -> colors.Color:
    if not hex_value:
        return fallback
    text = hex_value.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        return fallback
    try:
        return colors.HexColor(f"#{text}")
    except ValueError:
        return fallback


class _NumberedCanvas(pdf_canvas.Canvas):
    """Repinta cada pagina para emitir 'Pagina X / Y' con el total real."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._saved_states: list[dict[str, Any]] = []

    def showPage(self) -> None:  # noqa: N802 (API de reportlab)
        self._saved_states.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:  # noqa: N802 (API de reportlab)
        total = len(self._saved_states)
        for state in self._saved_states:
            self.__dict__.update(state)
            self._draw_page_number(total)
            super().showPage()
        super().save()

    def _draw_page_number(self, total: int) -> None:
        self.setFont("Helvetica", 8)
        self.setFillColor(colors.grey)
        label = f"Pagina {self._pageNumber} / {total}"
        self.drawRightString(_PAGE_WIDTH - _RIGHT, 12 * mm, label)


def _draw_logo(canvas: pdf_canvas.Canvas, data: bytes, max_height: float) -> float:
    try:
        from reportlab.lib.utils import ImageReader

        reader = ImageReader(io.BytesIO(data))
        width, height = reader.getSize()
        if height <= 0 or width <= 0:
            return 0.0
        scale = min(max_height / height, (60 * mm) / width)
        draw_width = width * scale
        draw_height = height * scale
        canvas.drawImage(
            reader,
            _LEFT,
            _PAGE_HEIGHT - 14 * mm - draw_height,
            width=draw_width,
            height=draw_height,
            mask="auto",
        )
        return draw_width
    except Exception:  # noqa: BLE001 - un logo corrupto no debe tumbar el documento
        return 0.0


def _draw_header(canvas: pdf_canvas.Canvas, branding: BrandingSnapshot) -> None:
    canvas.saveState()
    text_x = _LEFT
    if branding.logo_bytes:
        logo_width = _draw_logo(canvas, branding.logo_bytes, 14 * mm)
        text_x = _LEFT + logo_width + (4 * mm if logo_width else 0)

    canvas.setFillColor(colors.black)
    canvas.setFont("Helvetica-Bold", 13)
    canvas.drawString(text_x, _PAGE_HEIGHT - 16 * mm, branding.legal_name or "Organizacion")

    canvas.setFont("Helvetica", 8.5)
    y = _PAGE_HEIGHT - 21 * mm
    identity_bits: list[str] = []
    if branding.tax_id_label and branding.tax_id:
        identity_bits.append(f"{branding.tax_id_label}: {branding.tax_id}")
    elif branding.tax_id:
        identity_bits.append(branding.tax_id)
    if identity_bits:
        canvas.drawString(text_x, y, " | ".join(identity_bits))
        y -= 4 * mm
    if branding.address:
        canvas.drawString(text_x, y, branding.address)
        y -= 4 * mm
    contact = " | ".join(
        bit for bit in (branding.phone, branding.email, branding.website) if bit
    )
    if contact:
        canvas.drawString(text_x, y, contact)

    line_color = brand_color(branding.brand_color_primary, colors.HexColor("#333333"))
    canvas.setStrokeColor(line_color)
    canvas.setLineWidth(1.2)
    canvas.line(_LEFT, _PAGE_HEIGHT - 27 * mm, _PAGE_WIDTH - _RIGHT, _PAGE_HEIGHT - 27 * mm)
    canvas.restoreState()


def _draw_footer(canvas: pdf_canvas.Canvas, branding: BrandingSnapshot) -> None:
    canvas.saveState()
    accent = brand_color(branding.brand_color_secondary, colors.HexColor("#666666"))
    canvas.setStrokeColor(accent)
    canvas.setLineWidth(0.6)
    canvas.line(_LEFT, _BOTTOM - 4 * mm, _PAGE_WIDTH - _RIGHT, _BOTTOM - 4 * mm)

    canvas.setFillColor(colors.grey)
    canvas.setFont("Helvetica", 7.5)
    left = branding.footer_left or ""
    center = branding.footer_text or ""
    right = branding.footer_right or ""
    canvas.drawString(_LEFT, _BOTTOM - 8.5 * mm, left)
    canvas.drawCentredString(_PAGE_WIDTH / 2, _BOTTOM - 8.5 * mm, center)
    canvas.drawRightString(_PAGE_WIDTH - _RIGHT, _BOTTOM - 8.5 * mm, right)
    canvas.restoreState()


def render_pdf(
    *,
    flowables: list[Flowable],
    branding: BrandingSnapshot,
    title: str,
    generated_at: dt.datetime,
    document_number: str,
) -> bytes:
    """Construye el PDF con encabezado/pie configurables y 'Pagina X / Y'."""
    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=_LEFT,
        rightMargin=_RIGHT,
        topMargin=_TOP,
        bottomMargin=_BOTTOM,
        title=title,
        author=branding.legal_name or "",
        creator="Inventario Dedalo",
    )

    def _page_decor(canvas: pdf_canvas.Canvas, _doc: Any) -> None:
        _draw_header(canvas, branding)
        _draw_footer(canvas, branding)
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.grey)
        stamp = f"{document_number} - Generado: {generated_at.isoformat()}"
        canvas.drawString(_LEFT, _BOTTOM - 12 * mm, stamp)
        canvas.restoreState()

    document.build(
        flowables,
        onFirstPage=_page_decor,
        onLaterPages=_page_decor,
        canvasmaker=_NumberedCanvas,
    )
    return buffer.getvalue()
