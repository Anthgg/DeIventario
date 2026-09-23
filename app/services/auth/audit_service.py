"""Registro de eventos de auditoria (sin secretos ni tokens)."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.models import AuditEvent

LOGIN_SUCCESS = "LOGIN_SUCCESS"
LOGIN_FAILED = "LOGIN_FAILED"
LOGOUT = "LOGOUT"
USER_AUTO_PROVISIONED = "USER_AUTO_PROVISIONED"
USER_ACTIVATED = "USER_ACTIVATED"
USER_DEACTIVATED = "USER_DEACTIVATED"
ROLE_ASSIGNED = "ROLE_ASSIGNED"
ROLE_REVOKED = "ROLE_REVOKED"
ADMIN_BOOTSTRAPPED = "ADMIN_BOOTSTRAPPED"

# F004 — ciclo de vida de inventario
LOCATION_CREATED = "LOCATION_CREATED"
LOCATION_UPDATED = "LOCATION_UPDATED"
CAMPAIGN_CREATED = "CAMPAIGN_CREATED"
CAMPAIGN_UPDATED = "CAMPAIGN_UPDATED"
CAMPAIGN_ASSIGNED = "CAMPAIGN_ASSIGNED"
CAMPAIGN_REASSIGNED = "CAMPAIGN_REASSIGNED"
CAMPAIGN_UNASSIGNED = "CAMPAIGN_UNASSIGNED"
SNAPSHOT_FROZEN = "SNAPSHOT_FROZEN"
CAMPAIGN_STARTED = "CAMPAIGN_STARTED"
CAMPAIGN_EXPIRED = "CAMPAIGN_EXPIRED"
CAMPAIGN_REOPENED = "CAMPAIGN_REOPENED"
CAMPAIGN_CANCELLED = "CAMPAIGN_CANCELLED"

# F005 — motor de conteo
COUNT_SESSION_STARTED = "COUNT_SESSION_STARTED"
COUNT_SESSION_CANCELLED_BY_REASSIGNMENT = "COUNT_SESSION_CANCELLED_BY_REASSIGNMENT"
COUNT_SESSION_SUBMITTED = "COUNT_SESSION_SUBMITTED"
CAMPAIGN_SUBMITTED = "CAMPAIGN_SUBMITTED"


def record(
    db: Session,
    *,
    action: str,
    actor_user_id: uuid.UUID | None = None,
    entity_type: str,
    entity_id: uuid.UUID | None = None,
    metadata: dict[str, object] | None = None,
) -> AuditEvent:
    """Agrega un evento de auditoria a la sesion (commit lo hace el caller)."""
    event = AuditEvent(
        actor_user_id=actor_user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        metadata_=metadata,
    )
    db.add(event)
    return event
