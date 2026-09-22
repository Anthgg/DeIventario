"""Mapping de columnas reales de los exports Odoo (detectados en F002)."""

from __future__ import annotations

from app.importers.base import MissingHeaderError, UnknownImportTypeError
from app.models.enums import ImportBatchType

# Encabezados reales (export "Contacto (res.partner)").
CONTACT_HEADER_NAME = "Nombre completo"
CONTACT_HEADER_EXTERNAL_REF = "Referencia"

# Encabezados reales (export "Producto (product.template)").
PRODUCT_HEADER_INTERNAL_REF = "Referencia interna"
PRODUCT_HEADER_NAME = "Nombre"
PRODUCT_HEADER_SALE_PRICE = "Precio de venta"
PRODUCT_HEADER_COST = "Costo"
PRODUCT_HEADER_CONSIGNMENT = "Costo de consignación"
PRODUCT_HEADER_QTY = "Cantidad a la mano"

# Encabezados reales del export mixto (productos + movimientos).
MIXED_HEADER_SUPPLIER_REF = "Proveedores/Proveedor/Referencia"
MIXED_HEADER_SUPPLIER_NAME = "Proveedores/Proveedor"
MIXED_HEADER_MOVEMENT = "Producto/Movimiento de stock"
MIXED_HEADER_MOVEMENT_QTY = "Producto/Movimiento de stock/Cantidad"
MIXED_HEADER_MOVEMENT_SCHEDULED = "Producto/Movimiento de stock/Fecha programada"


def detect_import_type(headers: list[str]) -> ImportBatchType:
    """Determina el tipo de importacion por los encabezados reales."""
    normalized = {header for header in headers if header}
    if CONTACT_HEADER_NAME in normalized and CONTACT_HEADER_EXTERNAL_REF in normalized:
        return ImportBatchType.CONTACTS
    if PRODUCT_HEADER_INTERNAL_REF in normalized and MIXED_HEADER_MOVEMENT in normalized:
        return ImportBatchType.MIXED_PRODUCT_EXPORT
    if PRODUCT_HEADER_INTERNAL_REF in normalized and PRODUCT_HEADER_COST in normalized:
        return ImportBatchType.PRODUCTS
    raise UnknownImportTypeError("No se reconoce el tipo de archivo por sus encabezados.")


def build_column_map(
    headers: list[str], required: tuple[str, ...], optional: tuple[str, ...] = ()
) -> dict[str, int]:
    """Construye {encabezado: indice} validando que existan las columnas obligatorias."""
    index = {header: i for i, header in enumerate(headers) if header}
    missing = [name for name in required if name not in index]
    if missing:
        raise MissingHeaderError(f"Faltan columnas obligatorias: {', '.join(missing)}")
    return {name: index[name] for name in required + optional if name in index}
