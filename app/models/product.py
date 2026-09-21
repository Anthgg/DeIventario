"""Productos del maestro y sus referencias por proveedor."""

from __future__ import annotations

import decimal
import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.contact import Contact


class Product(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Producto del maestro. Precios SIEMPRE Numeric (nunca float)."""

    __tablename__ = "products"

    internal_reference: Mapped[str] = mapped_column(sa.String(100), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False, index=True)
    sale_price: Mapped[decimal.Decimal | None] = mapped_column(sa.Numeric(18, 4))
    cost: Mapped[decimal.Decimal | None] = mapped_column(sa.Numeric(18, 4))
    consignment_cost: Mapped[decimal.Decimal | None] = mapped_column(sa.Numeric(18, 4))
    currency: Mapped[str] = mapped_column(
        sa.String(3), nullable=False, default="PEN", server_default=sa.text("'PEN'")
    )
    raw_external_id: Mapped[str | None] = mapped_column(sa.String(255))
    active: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=True, server_default=sa.text("true"), index=True
    )

    supplier_refs: Mapped[list[ProductSupplierRef]] = relationship(back_populates="product")


class ProductSupplierRef(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Referencia producto-proveedor. Un producto puede tener varios proveedores."""

    __tablename__ = "product_supplier_refs"
    __table_args__ = (sa.UniqueConstraint("product_id", "contact_id"),)

    product_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("contacts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    supplier_reference: Mapped[str | None] = mapped_column(sa.String(255))
    raw_external_id: Mapped[str | None] = mapped_column(sa.String(255))

    product: Mapped[Product] = relationship(back_populates="supplier_refs")
    contact: Mapped[Contact] = relationship()
