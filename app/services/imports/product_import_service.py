"""Servicio de importacion de productos (upsert + referencias de proveedor)."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.importers.base import ParsedProduct
from app.models import Contact, Product, ProductSupplierRef

_DEFAULT_CURRENCY = "PEN"


class ProductImportService:
    """Upsert de productos por ``internal_reference`` y vinculo producto/proveedor."""

    def upsert(self, session: Session, product: ParsedProduct) -> tuple[uuid.UUID, bool]:
        """Crea o actualiza un producto. Devuelve (id, creado)."""
        existing = session.execute(
            select(Product)
            .where(Product.internal_reference == product.internal_reference)
            .limit(1)
        ).scalar_one_or_none()
        if existing is None:
            created = Product(
                internal_reference=product.internal_reference,
                name=product.name,
                sale_price=product.sale_price,
                cost=product.cost,
                consignment_cost=product.consignment_cost,
                currency=_DEFAULT_CURRENCY,
                active=True,
            )
            session.add(created)
            session.flush()
            return created.id, True

        existing.name = product.name
        existing.active = True
        if product.sale_price is not None:
            existing.sale_price = product.sale_price
        if product.cost is not None:
            existing.cost = product.cost
        if product.consignment_cost is not None:
            existing.consignment_cost = product.consignment_cost
        return existing.id, False

    def resolve_contact_id(self, session: Session, external_ref: str) -> uuid.UUID | None:
        """Devuelve el id del contacto por su referencia externa, o None."""
        return session.execute(
            select(Contact.id).where(Contact.external_ref == external_ref).limit(1)
        ).scalar_one_or_none()

    def link_supplier(
        self,
        session: Session,
        product_id: uuid.UUID,
        contact_id: uuid.UUID,
        supplier_reference: str,
    ) -> None:
        """Crea el vinculo producto/proveedor si no existe."""
        existing = session.execute(
            select(ProductSupplierRef.id)
            .where(
                ProductSupplierRef.product_id == product_id,
                ProductSupplierRef.contact_id == contact_id,
            )
            .limit(1)
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                ProductSupplierRef(
                    product_id=product_id,
                    contact_id=contact_id,
                    supplier_reference=supplier_reference,
                )
            )
