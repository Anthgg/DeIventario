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


class ReconciliationStatus(enum.StrEnum):
    """Estados de la conciliacion por producto."""

    PENDING = "PENDING"
    MATCHED = "MATCHED"
    DIFFERENCE = "DIFFERENCE"
    RECOUNT_REQUIRED = "RECOUNT_REQUIRED"
    UNDER_REVIEW = "UNDER_REVIEW"
    APPROVED = "APPROVED"
