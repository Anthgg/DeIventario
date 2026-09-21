"""Auditoria general del sistema (log append-only de acciones)."""

from __future__ import annotations

import datetime as dt
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin


class AuditEvent(UUIDPrimaryKeyMixin, Base):
    """Evento de auditoria: CREATE, ASSIGN, REASSIGN, SUBMIT, APPROVE, ..."""

    __tablename__ = "audit_events"

    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="SET NULL"), index=True
    )

    action: Mapped[str] = mapped_column(sa.String(50), nullable=False)
    entity_type: Mapped[str] = mapped_column(sa.String(50), nullable=False, index=True)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)

    inventory_campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("inventory_campaigns.id", ondelete="RESTRICT"), index=True
    )

    metadata_: Mapped[dict[str, object] | None] = mapped_column("metadata", postgresql.JSONB)

    occurred_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(), index=True
    )
