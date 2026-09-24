"""Enums del dominio Inventario Dedalo.

Se almacenan como VARCHAR + CHECK (ver ``app.db.base.varchar_enum``),
nunca como enums nativos de PostgreSQL, para poder evolucionarlos sin
migraciones pesadas de tipo ALTER TYPE.
"""

from __future__ import annotations

import enum


class CampaignStatus(enum.StrEnum):
    """Estados de una campana de inventario."""

    DRAFT = "DRAFT"
    ASSIGNED = "ASSIGNED"
    IN_PROGRESS = "IN_PROGRESS"
    SUBMITTED = "SUBMITTED"
    RECOUNT = "RECOUNT"
    UNDER_REVIEW = "UNDER_REVIEW"
    APPROVED = "APPROVED"
    CLOSED = "CLOSED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class AssignmentStatus(enum.StrEnum):
    """Estados de una asignacion de usuario a campana."""

    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"
    COMPLETED = "COMPLETED"


class RecountStatus(enum.StrEnum):
    """Estados de un reconteo ciego (F007)."""

    REQUESTED = "REQUESTED"
    ASSIGNED = "ASSIGNED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class CostSource(enum.StrEnum):
    """Origen del costo efectivo congelado en el snapshot."""

    COST = "COST"
    CONSIGNMENT = "CONSIGNMENT"
    ZERO = "ZERO"


class SessionType(enum.StrEnum):
    """Tipo de sesion de conteo."""

    INITIAL = "INITIAL"
    REASSIGNMENT = "REASSIGNMENT"
    RECOUNT = "RECOUNT"
    SUPERVISOR_CHECK = "SUPERVISOR_CHECK"


class SessionStatus(enum.StrEnum):
    """Estado de una sesion de conteo."""

    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    SUBMITTED = "SUBMITTED"
    CANCELLED = "CANCELLED"


class CountEventType(enum.StrEnum):
    """Tipos de evento del log inmutable de conteo."""

    QR_SCAN = "QR_SCAN"
    MULTI_QR_SCAN = "MULTI_QR_SCAN"
    MANUAL_ADD = "MANUAL_ADD"
    MANUAL_SUBTRACT = "MANUAL_SUBTRACT"
    MANUAL_SET = "MANUAL_SET"
    UNDO = "UNDO"
    DAMAGE_ADD = "DAMAGE_ADD"
    DAMAGE_SUBTRACT = "DAMAGE_SUBTRACT"


class EventSource(enum.StrEnum):
    """Origen de un evento de conteo."""

    CAMERA = "CAMERA"
    MANUAL = "MANUAL"
    OFFLINE_SYNC = "OFFLINE_SYNC"
    SYSTEM = "SYSTEM"


class DamageAction(enum.StrEnum):
    """Accion sobre la cantidad danada (el signo vive en la accion, no en quantity)."""

    ADD = "ADD"
    SUBTRACT = "SUBTRACT"


class ReconciliationStatus(enum.StrEnum):
    """Estados de la conciliacion por producto."""

    PENDING = "PENDING"
    MATCHED = "MATCHED"
    DIFFERENCE = "DIFFERENCE"
    RECOUNT_REQUIRED = "RECOUNT_REQUIRED"
    UNDER_REVIEW = "UNDER_REVIEW"
    APPROVED = "APPROVED"


class SelectionMode(enum.StrEnum):
    """Como se elegio el conteo oficial de un producto (F008)."""

    DEFAULT_SESSION = "DEFAULT_SESSION"
    PRODUCT_OVERRIDE = "PRODUCT_OVERRIDE"


class StockScope(enum.StrEnum):
    """Alcance del stock de origen de una campana."""

    AGGREGATE = "AGGREGATE"
    LOCATION = "LOCATION"


class ImportBatchType(enum.StrEnum):
    """Tipos de importacion soportados por el pipeline Odoo."""

    CONTACTS = "CONTACTS"
    PRODUCTS = "PRODUCTS"
    MIXED_PRODUCT_EXPORT = "MIXED_PRODUCT_EXPORT"


class ImportBatchStatus(enum.StrEnum):
    """Estados de un lote de importacion (pipeline Odoo)."""

    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_WARNINGS = "COMPLETED_WITH_WARNINGS"
    FAILED = "FAILED"


class DocumentStatus(enum.StrEnum):
    """Estados de un documento oficial generado (F010)."""

    GENERATED = "GENERATED"
    SUPERSEDED = "SUPERSEDED"
    FAILED = "FAILED"


class DocumentFormat(enum.StrEnum):
    """Formato de archivo de un documento oficial."""

    PDF = "PDF"
    XLSX = "XLSX"


class DocumentModule(enum.StrEnum):
    """Modulo dominio al que pertenece el documento oficial."""

    INVENTORY = "INVENTORY"


class CurrencyStyle(enum.StrEnum):
    """Estilo de formato monetario de la organizacion (F010)."""

    SYMBOL_BEFORE = "SYMBOL_BEFORE"
    SYMBOL_AFTER = "SYMBOL_AFTER"
    CODE_SUFFIX = "CODE_SUFFIX"
