"""Generacion, historial y descarga de documentos oficiales (F010).

Reglas duras de F010:
- Fuente de dinero: ``valuation_service.read_valuation`` (F009). Aqui NO se
  recalculan costos ni cantidades.
- Identidad de marca: ``organization_settings`` (nunca hardcodeada).
- Idempotencia por (module, document_type, entity, source_sha256,
  branding_sha256, template_version) con estado GENERATED.
- Campana CLOSED: sin documentos nuevos; la descarga sigue permitida.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from app.core.config import get_settings
from app.documents.engine import storage as document_storage
from app.documents.engine.branding import BrandingSnapshot, branding_hash, snapshot_from_settings
from app.documents.engine.context import InventoryDocContext
from app.documents.engine.hashing import canonical_json, hash_payload, sha256_hex
from app.documents.modules.inventory import audit_export, erp_export, management_report
from app.models import (
    AuditEvent,
    DocumentExport,
    ExportProfile,
    InventoryCampaign,
    InventoryCountEvent,
    InventoryCountSession,
    InventoryCountTotal,
    InventoryDamage,
    InventoryExtraItem,
    InventoryReconciliation,
    InventoryRecount,
    InventorySnapshotItem,
    InventoryUnknownCode,
    Product,
    User,
)
from app.models.enums import (
    CampaignStatus,
    DocumentFormat,
    DocumentModule,
    DocumentStatus,
)
from app.services.auth import audit_service
from app.services.documents import export_profile_service
from app.services.inventory import campaign_service
from app.services.system import organization_service
from app.services.valuation import valuation_service

TEMPLATE_VERSION = "1"
MODULE_VERSION = "1"
MODULE = DocumentModule.INVENTORY
ENTITY_TYPE = "inventory_campaign"

TITLES: dict[str, str] = {
    "management-report": "Informe de Gestion de Inventario",
    "audit-export": "Exportacion de Auditoria de Inventario",
    "erp-adjustment": "Ajuste de Inventario para ERP",
}


class DocumentError(Exception):
    def __init__(
        self,
        message: str,
        status_code: int = 400,
        code: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.payload = payload or {}


@dataclass(frozen=True)
class DocumentSpec:
    document_type: str
    format: DocumentFormat
    content_type: str
    extension: str
    renderer: Callable[..., bytes]
    needs_audit_trail: bool = False
    needs_profile: bool = False


def _render_management(ctx: InventoryDocContext, **_: Any) -> bytes:
    return management_report.render(ctx)


def _render_audit(ctx: InventoryDocContext, **kwargs: Any) -> bytes:
    return audit_export.render(ctx, extra_metadata=kwargs.get("extra_metadata") or {})


def _render_erp(ctx: InventoryDocContext, **kwargs: Any) -> bytes:
    profile = kwargs.get("profile") or {}
    return erp_export.render(ctx, profile=profile)


SPECS: dict[str, DocumentSpec] = {
    "management-report": DocumentSpec(
        document_type="management-report",
        format=DocumentFormat.PDF,
        content_type="application/pdf",
        extension="pdf",
        renderer=_render_management,
    ),
    "audit-export": DocumentSpec(
        document_type="audit-export",
        format=DocumentFormat.XLSX,
        content_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        extension="xlsx",
        needs_audit_trail=True,
        renderer=_render_audit,
    ),
    "erp-adjustment": DocumentSpec(
        document_type="erp-adjustment",
        format=DocumentFormat.XLSX,
        content_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        extension="xlsx",
        needs_audit_trail=False,
        needs_profile=True,
        renderer=_render_erp,
    ),
}

REQUIRED_DOCUMENT_TYPES = tuple(SPECS.keys())


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


# ------------------------------ recoleccion ---------------------------------


def _reconciliation_payloads(db: Session, campaign_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = db.execute(
        select(InventoryReconciliation, Product, InventorySnapshotItem)
        .join(Product, Product.id == InventoryReconciliation.product_id, isouter=True)
        .join(
            InventorySnapshotItem,
            (InventorySnapshotItem.inventory_campaign_id == campaign_id)
            & (InventorySnapshotItem.product_id == InventoryReconciliation.product_id),
            isouter=True,
        )
        .where(InventoryReconciliation.inventory_campaign_id == campaign_id)
        .order_by(InventoryReconciliation.product_id)
    ).all()
    payloads: list[dict[str, Any]] = []
    for row, product, snapshot in rows:
        reference = (
            snapshot.internal_reference_snapshot if snapshot is not None
            else (product.internal_reference if product is not None else "")
        )
        name = (
            snapshot.description_snapshot if snapshot is not None
            else (product.name if product is not None else None)
        )
        payloads.append(
            {
                "product": {
                    "id": str(row.product_id),
                    "internal_reference": reference,
                    "name": name,
                },
                "expected_quantity": row.expected_quantity,
                "approved_physical_quantity": row.approved_physical_quantity,
                "difference_quantity": row.difference_quantity,
                "damaged_quantity": row.damaged_quantity,
                "missing_quantity": row.missing_quantity,
                "surplus_quantity": row.surplus_quantity,
                "effective_unit_cost": row.effective_unit_cost,
                "missing_cost_value": row.missing_cost_value,
                "damage_cost_value": row.damage_cost_value,
                "surplus_cost_value": row.surplus_cost_value,
                "affected_sale_value": row.affected_sale_value,
                "status": row.status.value,
                "reason": row.reason,
                "observation": row.observation,
                "approved_at": _iso(row.approved_at),
            }
        )
    return payloads


def _session_payloads(db: Session, campaign_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = db.execute(
        select(InventoryCountSession, User)
        .join(User, User.id == InventoryCountSession.user_id, isouter=True)
        .where(InventoryCountSession.inventory_campaign_id == campaign_id)
        .order_by(InventoryCountSession.session_number)
    ).all()
    return [
        {
            "session_number": session.session_number,
            "session_type": session.session_type.value,
            "status": session.status.value,
            "started_at": session.started_at,
            "submitted_at": session.submitted_at,
            "device_identifier": session.device_identifier,
            "user_display_name": user.display_name if user is not None else None,
        }
        for session, user in rows
    ]


def _event_payloads(db: Session, campaign_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = db.execute(
        select(
            InventoryCountEvent,
            InventoryCountSession.session_number,
            Product.internal_reference,
        )
        .join(
            InventoryCountSession,
            InventoryCountSession.id == InventoryCountEvent.session_id,
        )
        .join(Product, Product.id == InventoryCountEvent.product_id, isouter=True)
        .where(InventoryCountSession.inventory_campaign_id == campaign_id)
        .order_by(InventoryCountSession.session_number, InventoryCountEvent.server_sequence)
    ).all()
    return [
        {
            "session_number": session_number,
            "server_sequence": event.server_sequence,
            "event_type": event.event_type.value,
            "product_reference": reference,
            "scanned_code": event.scanned_code,
            "quantity": event.resulting_quantity,
            "damage_delta_quantity": event.damage_delta_quantity,
            "occurred_at": event.occurred_at,
        }
        for event, session_number, reference in rows
    ]


def _count_payloads(db: Session, campaign_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = db.execute(
        select(
            InventoryCountSession.session_number,
            Product.internal_reference,
            Product.name,
            InventoryCountTotal.quantity,
            InventoryCountTotal.damaged_quantity,
            InventoryCountTotal.updated_at,
        )
        .join(
            InventoryCountSession,
            InventoryCountSession.id == InventoryCountTotal.session_id,
        )
        .join(Product, Product.id == InventoryCountTotal.product_id)
        .where(InventoryCountSession.inventory_campaign_id == campaign_id)
        .order_by(InventoryCountSession.session_number, Product.internal_reference)
    ).all()
    return [
        {
            "session_number": session_number,
            "product_reference": reference,
            "product_name": name,
            "quantity": quantity,
            "damaged_quantity": damaged_quantity,
            "updated_at": updated_at,
        }
        for session_number, reference, name, quantity, damaged_quantity, updated_at in rows
    ]


def _damage_payloads(db: Session, campaign_id: uuid.UUID) -> list[dict[str, Any]]:
    creator = aliased(User)
    rows = db.execute(
        select(
            InventoryDamage,
            InventoryCountSession.session_number,
            Product.internal_reference,
            creator.display_name,
        )
        .join(
            InventoryCountSession,
            InventoryCountSession.id == InventoryDamage.session_id,
        )
        .join(Product, Product.id == InventoryDamage.product_id, isouter=True)
        .join(creator, creator.id == InventoryDamage.created_by, isouter=True)
        .where(InventoryCountSession.inventory_campaign_id == campaign_id)
        .order_by(InventoryCountSession.session_number, InventoryDamage.created_at)
    ).all()
    return [
        {
            "session_number": session_number,
            "product_reference": reference,
            "scanned_code": damage.scanned_code,
            "action": damage.action.value,
            "quantity": damage.quantity,
            "reason": damage.reason,
            "observation": damage.observation,
            "event_id": str(damage.event_id) if damage.event_id else None,
            "created_by": creator_name,
            "created_at": damage.created_at,
            "has_evidence": damage.evidence_path is not None,
        }
        for damage, session_number, reference, creator_name in rows
    ]


def _extra_payloads(db: Session, campaign_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = db.execute(
        select(
            InventoryCountSession.session_number,
            Product.internal_reference,
            Product.name,
            InventoryExtraItem.quantity,
            InventoryExtraItem.first_detected_at,
        )
        .join(
            InventoryCountSession,
            InventoryCountSession.id == InventoryExtraItem.session_id,
        )
        .join(Product, Product.id == InventoryExtraItem.product_id)
        .where(InventoryCountSession.inventory_campaign_id == campaign_id)
        .order_by(InventoryCountSession.session_number, Product.internal_reference)
    ).all()
    return [
        {
            "session_number": session_number,
            "product_reference": reference,
            "product_name": name,
            "quantity": quantity,
            "first_detected_at": first_detected_at,
        }
        for session_number, reference, name, quantity, first_detected_at in rows
    ]


def _unknown_payloads(db: Session, campaign_id: uuid.UUID) -> list[dict[str, Any]]:
    resolver = aliased(User)
    rows = db.execute(
        select(
            InventoryCountSession.session_number,
            InventoryUnknownCode.scanned_code,
            InventoryUnknownCode.quantity,
            InventoryUnknownCode.damaged_quantity,
            Product.internal_reference,
            resolver.display_name,
            InventoryUnknownCode.resolved_at,
        )
        .join(
            InventoryCountSession,
            InventoryCountSession.id == InventoryUnknownCode.session_id,
        )
        .join(Product, Product.id == InventoryUnknownCode.resolved_product_id, isouter=True)
        .join(resolver, resolver.id == InventoryUnknownCode.resolved_by, isouter=True)
        .where(InventoryCountSession.inventory_campaign_id == campaign_id)
        .order_by(InventoryCountSession.session_number, InventoryUnknownCode.scanned_code)
    ).all()
    return [
        {
            "session_number": session_number,
            "scanned_code": scanned_code,
            "quantity": quantity,
            "damaged_quantity": damaged_quantity,
            "resolved_product_reference": reference,
            "resolved_by": resolver_name,
            "resolved_at": resolved_at,
        }
        for (
            session_number,
            scanned_code,
            quantity,
            damaged_quantity,
            reference,
            resolver_name,
            resolved_at,
        ) in rows
    ]


def _recount_payloads(db: Session, campaign_id: uuid.UUID) -> list[dict[str, Any]]:
    requester = aliased(User)
    assignee = aliased(User)
    source_session = aliased(InventoryCountSession)
    result_session = aliased(InventoryCountSession)
    rows = db.execute(
        select(
            InventoryRecount,
            requester.display_name,
            assignee.display_name,
            source_session.session_number,
            result_session.session_number,
        )
        .join(requester, requester.id == InventoryRecount.requested_by, isouter=True)
        .join(assignee, assignee.id == InventoryRecount.assigned_user_id, isouter=True)
        .join(
            source_session,
            source_session.id == InventoryRecount.source_session_id,
            isouter=True,
        )
        .join(
            result_session,
            result_session.id == InventoryRecount.resulting_session_id,
            isouter=True,
        )
        .where(InventoryRecount.inventory_campaign_id == campaign_id)
        .order_by(InventoryRecount.created_at, InventoryRecount.id)
    ).all()
    return [
        {
            "status": recount.status.value,
            "requested_by": requested_by,
            "assigned_to": assigned_to,
            "source_session_number": source_number,
            "resulting_session_number": result_number,
            "reason": recount.reason,
            "started_at": recount.started_at,
            "completed_at": recount.completed_at,
            "cancelled_at": recount.cancelled_at,
            "cancel_reason": recount.cancel_reason,
        }
        for recount, requested_by, assigned_to, source_number, result_number in rows
    ]


def _audit_payloads(db: Session, campaign_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = db.execute(
        select(AuditEvent, User)
        .join(User, User.id == AuditEvent.actor_user_id, isouter=True)
        .where(
            (
                (AuditEvent.entity_type == ENTITY_TYPE)
                & (AuditEvent.entity_id == campaign_id)
            )
            | (AuditEvent.inventory_campaign_id == campaign_id)
        )
        .order_by(AuditEvent.occurred_at, AuditEvent.id)
    ).all()
    return [
        {
            "action": event.action,
            "actor": user.display_name if user is not None else None,
            "entity_type": event.entity_type,
            "entity_id": str(event.entity_id) if event.entity_id else None,
            "occurred_at": event.occurred_at,
            "metadata": canonical_json(event.metadata_) if event.metadata_ else None,
        }
        for event, user in rows
    ]


# ------------------------------ generacion ----------------------------------


def _spec(document_type: str) -> DocumentSpec:
    spec = SPECS.get(document_type)
    if spec is None:
        raise DocumentError(
            "Tipo de documento no soportado", 422, "DOCUMENT_TYPE_UNKNOWN"
        )
    return spec


def _source_payload(
    *,
    campaign: InventoryCampaign,
    document_type: str,
    branding_sha: str,
    profile: ExportProfile | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "module": MODULE.value,
        "document_type": document_type,
        "entity_type": ENTITY_TYPE,
        "entity_id": str(campaign.id),
        "campaign": {
            "id": str(campaign.id),
            "code": campaign.code,
            "version": campaign.version,
            "snapshot_sha256": campaign.snapshot_sha256,
            "reconciliation_source_sha256": campaign.reconciliation_source_sha256,
            "valuation_source_sha256": campaign.valuation_source_sha256,
            "valuation_calculated_at": _iso(campaign.valuation_calculated_at),
        },
        "template_version": TEMPLATE_VERSION,
        "module_version": MODULE_VERSION,
        "branding_sha256": branding_sha,
        "export_profile": (
            {"code": profile.code, "version": profile.version} if profile is not None else None
        ),
    }
    return payload


def _find_existing(
    db: Session,
    *,
    document_type: str,
    entity_id: uuid.UUID,
    source_sha: str,
    branding_sha: str,
) -> DocumentExport | None:
    return db.execute(
        select(DocumentExport)
        .where(
            DocumentExport.module == MODULE,
            DocumentExport.document_type == document_type,
            DocumentExport.entity_type == ENTITY_TYPE,
            DocumentExport.entity_id == entity_id,
            DocumentExport.source_sha256 == source_sha,
            DocumentExport.branding_sha256 == branding_sha,
            DocumentExport.template_version == TEMPLATE_VERSION,
            DocumentExport.status == DocumentStatus.GENERATED,
        )
        .limit(1)
    ).scalar_one_or_none()


def _allocate_document_number(db: Session) -> str:
    settings_row = organization_service.get_settings(db, lock=True)
    number = f"{settings_row.document_number_prefix}-{settings_row.next_document_number:06d}"
    settings_row.next_document_number += 1
    return number


def document_payload(
    row: DocumentExport,
    *,
    already_generated: bool = False,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    filename = Path(row.file_path).name
    return {
        "id": str(row.id),
        "module": row.module.value,
        "document_type": row.document_type,
        "title": row.title,
        "document_number": row.document_number,
        "status": row.status.value,
        "format": row.format.value,
        "template_version": row.template_version,
        "module_version": row.module_version,
        "locale": row.locale,
        "entity_type": row.entity_type,
        "entity_id": str(row.entity_id),
        "campaign_id": str(row.inventory_campaign_id)
        if row.inventory_campaign_id is not None
        else None,
        "source_sha256": row.source_sha256,
        "source_snapshot": row.source_snapshot,
        "branding_sha256": row.branding_sha256,
        "file": {
            "filename": filename,
            "content_type": row.content_type,
            "size": row.file_size,
            "sha256": row.file_sha256,
        },
        "generated_at": _iso(row.generated_at),
        "generated_by": str(row.generated_by) if row.generated_by else None,
        "superseded_at": _iso(row.superseded_at),
        "superseded_by": str(row.superseded_by) if row.superseded_by else None,
        "error_message": row.error_message,
        "already_generated": already_generated,
        "download_path": f"{settings.API_PREFIX}/documents/{row.id}/download",
        "warnings": warnings or [],
    }


def _supersede_previous(
    db: Session,
    *,
    document_type: str,
    entity_id: uuid.UUID,
    new_row: DocumentExport,
    actor_id: uuid.UUID | None,
) -> int:
    previous = list(
        db.execute(
            select(DocumentExport)
            .where(
                DocumentExport.module == MODULE,
                DocumentExport.document_type == document_type,
                DocumentExport.entity_type == ENTITY_TYPE,
                DocumentExport.entity_id == entity_id,
                DocumentExport.status == DocumentStatus.GENERATED,
                DocumentExport.id != new_row.id,
            )
            .with_for_update()
        ).scalars()
    )
    now = _now()
    for row in previous:
        row.status = DocumentStatus.SUPERSEDED
        row.superseded_at = now
        row.superseded_by = new_row.id
        audit_service.record(
            db,
            action=audit_service.DOCUMENT_SUPERSEDED,
            actor_user_id=actor_id,
            entity_type=ENTITY_TYPE,
            entity_id=entity_id,
            metadata={
                "document_id": str(row.id),
                "document_number": row.document_number,
                "superseded_by": str(new_row.id),
                "superseded_by_number": new_row.document_number,
            },
        )
    return len(previous)


def generate_document(
    db: Session,
    *,
    actor_id: uuid.UUID,
    campaign_id: uuid.UUID,
    document_type: str,
    expected_version: int | None = None,
    profile_code: str | None = None,
    actor_label: str | None = None,
) -> dict[str, Any]:
    """Genera (o reutiliza) un documento oficial de la campana."""
    spec = _spec(document_type)
    campaign = campaign_service.lock_campaign(db, campaign_id)
    campaign_service.require_not_closed(campaign)
    campaign_service.check_version(campaign, expected_version)
    if campaign.status is not CampaignStatus.APPROVED:
        raise DocumentError(
            "La campana no esta aprobada", 409, "CAMPAIGN_NOT_APPROVED"
        )

    settings_row = organization_service.get_settings(db)
    organization_service.require_configured(settings_row)
    branding: BrandingSnapshot = snapshot_from_settings(settings_row)
    source_branding_sha = branding_hash(branding)

    profile: ExportProfile | None = None
    if spec.needs_profile:
        profile = export_profile_service.get_profile(db, profile_code)

    source = _source_payload(
        campaign=campaign,
        document_type=document_type,
        branding_sha=source_branding_sha,
        profile=profile,
    )
    source_sha = hash_payload(source)

    warnings: list[str] = []
    if spec.needs_profile:
        warnings.append(erp_export.VENDOR_TEMPLATE_WARNING)

    existing = _find_existing(
        db,
        document_type=document_type,
        entity_id=campaign.id,
        source_sha=source_sha,
        branding_sha=source_branding_sha,
    )
    if existing is not None:
        db.commit()
        return document_payload(existing, already_generated=True, warnings=warnings)

    valuation: dict[str, Any] = valuation_service.read_valuation(db, campaign_id)

    reconciliation_rows: list[dict[str, Any]] = []
    sessions: list[dict[str, Any]] = []
    counts: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    damages: list[dict[str, Any]] = []
    extras: list[dict[str, Any]] = []
    unknowns: list[dict[str, Any]] = []
    recounts: list[dict[str, Any]] = []
    audit_events: list[dict[str, Any]] = []
    if document_type in ("audit-export", "erp-adjustment"):
        reconciliation_rows = _reconciliation_payloads(db, campaign_id)
    if spec.needs_audit_trail:
        sessions = _session_payloads(db, campaign_id)
        events = _event_payloads(db, campaign_id)
        audit_events = _audit_payloads(db, campaign_id)
    if document_type == "audit-export":
        counts = _count_payloads(db, campaign_id)
        damages = _damage_payloads(db, campaign_id)
        extras = _extra_payloads(db, campaign_id)
        unknowns = _unknown_payloads(db, campaign_id)
        recounts = _recount_payloads(db, campaign_id)

    document_number = _allocate_document_number(db)
    generated_at = _now()
    ctx = InventoryDocContext(
        branding=branding,
        campaign=dict(valuation.get("campaign") or {}),
        valuation=valuation,
        document_number=document_number,
        title=TITLES[document_type],
        document_type=document_type,
        generated_at=generated_at,
        generated_by_label=actor_label,
        sessions=sessions,
        counts=counts,
        events=events,
        damages=damages,
        extras=extras,
        unknowns=unknowns,
        recounts=recounts,
        audit_events=audit_events,
        reconciliation_rows=reconciliation_rows,
        profile=export_profile_service.profile_payload(profile) if profile else None,
        warnings=list(valuation.get("warnings") or []),
    )

    render_kwargs: dict[str, Any] = {}
    if spec.needs_audit_trail:
        render_kwargs["extra_metadata"] = {
            "source_sha256": source_sha,
            "template_version": TEMPLATE_VERSION,
            "module_version": MODULE_VERSION,
        }
        if document_type == "audit-export":
            render_kwargs["extra_metadata"].update(
                {
                    "snapshot_sha256": campaign.snapshot_sha256,
                    "reconciliation_source_sha256": campaign.reconciliation_source_sha256,
                    "valuation_source_sha256": campaign.valuation_source_sha256,
                    "branding_sha256": source_branding_sha,
                }
            )
    if profile is not None:
        render_kwargs["profile"] = export_profile_service.profile_payload(profile)

    try:
        data = spec.renderer(ctx, **render_kwargs)
    except Exception as exc:  # noqa: BLE001 - se persiste el intento fallido
        failed = _record_failure(
            db,
            actor_id=actor_id,
            campaign=campaign,
            document_type=document_type,
            document_number=document_number,
            source_sha=source_sha,
            branding_sha=source_branding_sha,
            error=str(exc),
        )
        db.commit()
        raise DocumentError(
            "No se pudo generar el documento",
            500,
            "DOCUMENT_RENDER_FAILED",
            {"document_id": str(failed.id)},
        ) from exc

    file_sha = sha256_hex(data)
    relative_path = f"{MODULE.value}/{document_number}.{spec.extension}"
    written_path = document_storage.write_document(relative_path, data)

    row = DocumentExport(
        id=uuid.uuid4(),
        module=MODULE,
        document_type=document_type,
        title=TITLES[document_type],
        document_number=document_number,
        status=DocumentStatus.GENERATED,
        format=spec.format,
        template_version=TEMPLATE_VERSION,
        module_version=MODULE_VERSION,
        locale="es",
        entity_type=ENTITY_TYPE,
        entity_id=campaign.id,
        inventory_campaign_id=campaign.id,
        source_sha256=source_sha,
        branding_sha256=source_branding_sha,
        source_snapshot={
            "campaign": source["campaign"],
            "valuation": {
                "currency": valuation.get("currency"),
                "calculated_at": valuation.get("calculated_at"),
                "summary": valuation.get("summary"),
                "items_count": len(valuation.get("items") or []),
            },
            "export_profile": source["export_profile"],
            "warnings": ctx.warnings,
        },
        file_path=relative_path,
        file_sha256=file_sha,
        file_size=len(data),
        content_type=spec.content_type,
        generated_by=actor_id,
        generated_at=generated_at,
    )
    try:
        db.add(row)
        db.flush()
        _supersede_previous(
            db,
            document_type=document_type,
            entity_id=campaign.id,
            new_row=row,
            actor_id=actor_id,
        )
        audit_service.record(
            db,
            action=audit_service.DOCUMENT_GENERATED,
            actor_user_id=actor_id,
            entity_type=ENTITY_TYPE,
            entity_id=campaign.id,
            metadata={
                "document_id": str(row.id),
                "document_number": document_number,
                "document_type": document_type,
                "format": spec.format.value,
                "source_sha256": source_sha,
                "branding_sha256": source_branding_sha,
                "file_sha256": file_sha,
                "campaign_version": campaign.version,
            },
        )
        db.commit()
    except Exception:
        db.rollback()
        try:
            file_is_referenced = (
                db.execute(
                    select(DocumentExport.id).where(
                        DocumentExport.file_path == relative_path,
                        DocumentExport.status.in_(
                            (DocumentStatus.GENERATED, DocumentStatus.SUPERSEDED)
                        ),
                    )
                ).scalar_one_or_none()
                is not None
            )
        except Exception:
            # If the commit outcome cannot be read, preserve the file rather
            # than risk deleting a document whose metadata did commit.
            file_is_referenced = True
        if not file_is_referenced:
            written_path.unlink(missing_ok=True)
        raise
    return document_payload(row, already_generated=False, warnings=warnings)


def _record_failure(
    db: Session,
    *,
    actor_id: uuid.UUID,
    campaign: InventoryCampaign,
    document_type: str,
    document_number: str,
    source_sha: str,
    branding_sha: str,
    error: str,
) -> DocumentExport:
    spec = SPECS[document_type]
    failed = DocumentExport(
        id=uuid.uuid4(),
        module=MODULE,
        document_type=document_type,
        title=TITLES[document_type],
        document_number=document_number,
        status=DocumentStatus.FAILED,
        format=spec.format,
        template_version=TEMPLATE_VERSION,
        module_version=MODULE_VERSION,
        locale="es",
        entity_type=ENTITY_TYPE,
        entity_id=campaign.id,
        inventory_campaign_id=campaign.id,
        source_sha256=source_sha,
        branding_sha256=branding_sha,
        source_snapshot=None,
        file_path=f"{MODULE.value}/{document_number}.{spec.extension}",
        file_sha256="",
        file_size=0,
        content_type=spec.content_type,
        generated_by=actor_id,
        generated_at=_now(),
        error_message=error[:2000],
    )
    db.add(failed)
    db.flush()
    return failed


# --------------------------- historial y descarga ---------------------------


def list_documents(
    db: Session,
    *,
    document_type: str | None = None,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    status: DocumentStatus | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    query = select(DocumentExport)
    count_query = select(func.count()).select_from(DocumentExport)
    if document_type is not None:
        query = query.where(DocumentExport.document_type == document_type)
        count_query = count_query.where(DocumentExport.document_type == document_type)
    if entity_type is not None:
        query = query.where(DocumentExport.entity_type == entity_type)
        count_query = count_query.where(DocumentExport.entity_type == entity_type)
    if entity_id is not None:
        query = query.where(DocumentExport.entity_id == entity_id)
        count_query = count_query.where(DocumentExport.entity_id == entity_id)
    if status is not None:
        query = query.where(DocumentExport.status == status)
        count_query = count_query.where(DocumentExport.status == status)
    total = db.execute(count_query).scalar_one()
    rows = list(
        db.execute(
            query.order_by(DocumentExport.generated_at.desc()).limit(limit).offset(offset)
        ).scalars()
    )
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [document_payload(row) for row in rows],
    }


def get_document(db: Session, document_id: uuid.UUID) -> DocumentExport:
    row = db.get(DocumentExport, document_id)
    if row is None:
        raise DocumentError("Documento no encontrado", 404, "DOCUMENT_NOT_FOUND")
    return row


def document_file(db: Session, document_id: uuid.UUID) -> tuple[Path, str, str]:
    """Resuelve el archivo para descarga (permitido incluso en campana CLOSED)."""
    row = get_document(db, document_id)
    if row.status is DocumentStatus.FAILED:
        raise DocumentError("El documento no esta disponible", 409, "DOCUMENT_NOT_READY")
    path = document_storage.resolve_document(row.file_path)
    return path, row.content_type, Path(row.file_path).name


def campaign_documents(
    db: Session, campaign_id: uuid.UUID, *, limit: int = 50, offset: int = 0
) -> dict[str, Any]:
    return list_documents(
        db, entity_id=campaign_id, limit=limit, offset=offset
    )


def required_documents_for(db: Session, campaign_id: uuid.UUID) -> dict[str, str | None]:
    rows = db.execute(
        select(DocumentExport.document_type, DocumentExport.document_number)
        .where(
            DocumentExport.module == MODULE,
            DocumentExport.entity_type == ENTITY_TYPE,
            DocumentExport.entity_id == campaign_id,
            DocumentExport.status == DocumentStatus.GENERATED,
        )
    ).all()
    found: dict[str, str | None] = {row[0]: row[1] for row in rows}
    return {required: found.get(required) for required in REQUIRED_DOCUMENT_TYPES}


def close_campaign(
    db: Session,
    *,
    actor_id: uuid.UUID,
    campaign_id: uuid.UUID,
    expected_version: int | None = None,
) -> dict[str, Any]:
    """Cierre oficial (F010): exige valorizacion + 3 documentos oficiales."""
    campaign = campaign_service.lock_campaign(db, campaign_id)
    if campaign.status is CampaignStatus.CLOSED:
        db.commit()
        return {
            "already_closed": True,
            "campaign_id": str(campaign.id),
            "status": campaign.status.value,
            "campaign_version": campaign.version,
            "closed_at": _iso(campaign.closed_at),
        }
    campaign_service.check_version(campaign, expected_version)
    if campaign.status is not CampaignStatus.APPROVED:
        raise DocumentError(
            "Solo se puede cerrar una campana aprobada", 409, "CAMPAIGN_NOT_APPROVED"
        )
    if campaign.valuation_calculated_at is None:
        raise DocumentError(
            "La valorizacion no esta calculada", 409, "VALUATION_NOT_CALCULATED"
        )
    documents = required_documents_for(db, campaign_id)
    missing = [doc_type for doc_type, number in documents.items() if number is None]
    if missing:
        raise DocumentError(
            "Faltan documentos oficiales para cerrar la campana",
            409,
            "DOCUMENTS_REQUIRED",
            {"missing_documents": missing},
        )
    now = _now()
    campaign.status = CampaignStatus.CLOSED
    campaign.closed_at = now
    campaign.closed_by = actor_id
    previous_version = campaign.version
    campaign.version += 1
    audit_service.record(
        db,
        action=audit_service.CAMPAIGN_CLOSED,
        actor_user_id=actor_id,
        entity_type=ENTITY_TYPE,
        entity_id=campaign.id,
        metadata={
            "code": campaign.code,
            "previous_version": previous_version,
            "version": campaign.version,
            "documents": {key: value for key, value in documents.items() if value is not None},
        },
    )
    db.commit()
    return {
        "already_closed": False,
        "campaign_id": str(campaign.id),
        "status": campaign.status.value,
        "campaign_version": campaign.version,
        "closed_at": _iso(campaign.closed_at),
        "documents": {key: value for key, value in documents.items() if value is not None},
    }
