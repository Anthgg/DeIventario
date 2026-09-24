"""Contexto tipado compartido por los renderers de inventario (F010)."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from app.documents.engine.branding import BrandingSnapshot


@dataclass
class InventoryDocContext:
    """Datos ya validados que consume un renderer (sin acceso a BD)."""

    branding: BrandingSnapshot
    campaign: dict[str, Any]
    valuation: dict[str, Any]
    document_number: str
    title: str
    document_type: str
    generated_at: dt.datetime
    generated_by_label: str | None = None
    sessions: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    audit_events: list[dict[str, Any]] = field(default_factory=list)
    reconciliation_rows: list[dict[str, Any]] = field(default_factory=list)
    profile: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
