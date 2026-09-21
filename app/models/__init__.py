"""Modelos del dominio. Importarlos todos aqui asegura el registro en Base."""

from __future__ import annotations

from app.models.audit import AuditEvent
from app.models.contact import Contact
from app.models.counting import (
    InventoryDamage,
    InventoryExtraItem,
    InventoryRecount,
    InventoryUnknownCode,
)
from app.models.enums import (
    AssignmentStatus,
    CampaignStatus,
    CostSource,
    CountEventType,
    EventSource,
    ReconciliationStatus,
    SessionStatus,
    SessionType,
)
from app.models.inventory import (
    InventoryAssignment,
    InventoryCampaign,
    InventoryCountEvent,
    InventoryCountSession,
    InventoryCountTotal,
    InventorySnapshotItem,
)
from app.models.location import Location
from app.models.product import Product, ProductSupplierRef
from app.models.reconciliation import InventoryReconciliation
from app.models.role import Role, UserRole
from app.models.user import User

__all__ = [
    "AssignmentStatus",
    "AuditEvent",
    "CampaignStatus",
    "Contact",
    "CostSource",
    "CountEventType",
    "EventSource",
    "InventoryAssignment",
    "InventoryCampaign",
    "InventoryCountEvent",
    "InventoryCountSession",
    "InventoryCountTotal",
    "InventoryDamage",
    "InventoryExtraItem",
    "InventoryRecount",
    "InventoryReconciliation",
    "InventorySnapshotItem",
    "InventoryUnknownCode",
    "Location",
    "Product",
    "ProductSupplierRef",
    "ReconciliationStatus",
    "Role",
    "SessionStatus",
    "SessionType",
    "User",
    "UserRole",
]
