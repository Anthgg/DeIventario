"""Regla de costo efectivo (F002, seccion 14)."""

from __future__ import annotations

import decimal

from app.models.enums import CostSource


def resolve_effective_cost(
    cost: decimal.Decimal | None, consignment_cost: decimal.Decimal | None
) -> tuple[decimal.Decimal, CostSource]:
    """Resuelve el costo efectivo y su origen.

    - ``cost > 0`` -> COST
    - ``cost <= 0`` y ``consignment_cost > 0`` -> CONSIGNMENT
    - en otro caso -> ZERO
    """
    c = cost if cost is not None else decimal.Decimal("0")
    cc = consignment_cost if consignment_cost is not None else decimal.Decimal("0")
    if c > 0:
        return c, CostSource.COST
    if cc > 0:
        return cc, CostSource.CONSIGNMENT
    return decimal.Decimal("0"), CostSource.ZERO
