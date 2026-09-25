"""Conciliacion F008: preview, prepare, listado y seleccion de conteo oficial.

Solo LEE sesiones/totals/damages/extras/unknowns/snapshot y escribe
inventory_reconciliations + metadatos de la campana. Nunca modifica las
fuentes fisicas y nunca calcula dinero (F009).

Seleccion de conteo oficial: NO existe auto-winner (prohibido ultimo,
mayor, promedio, mayoria, cercania a Odoo). El ganador siempre es explicito:
DEFAULT_SESSION (elegida para todos) o PRODUCT_OVERRIDE (por producto).
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    InventoryCampaign,
    InventoryCountSession,
    InventoryReconciliation,
    Product,
)
from app.models.enums import CampaignStatus, ReconciliationStatus, SelectionMode, SessionStatus
from app.services.auth import audit_service
from app.services.inventory import campaign_service
from app.services.reconciliation import comparison_service
from app.services.reconciliation.errors import ReconciliationError

ZERO = decimal.Decimal("0")

PREPARE_STATUSES = (CampaignStatus.SUBMITTED, CampaignStatus.UNDER_REVIEW)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _ref(value: uuid.UUID | None) -> str | None:
    return str(value) if value is not None else None


def _q(value: decimal.Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _locked_rows(db: Session, campaign_id: uuid.UUID) -> list[InventoryReconciliation]:
    return list(
        db.execute(
            select(InventoryReconciliation)
            .where(InventoryReconciliation.inventory_campaign_id == campaign_id)
            .order_by(InventoryReconciliation.product_id)
            .with_for_update()
        ).scalars()
    )


def _read_rows(db: Session, campaign_id: uuid.UUID) -> list[InventoryReconciliation]:
    return list(
        db.execute(
            select(InventoryReconciliation)
            .where(InventoryReconciliation.inventory_campaign_id == campaign_id)
            .order_by(InventoryReconciliation.product_id)
        ).scalars()
    )


def _eligible_session(
    db: Session, campaign_id: uuid.UUID, session_id: uuid.UUID
) -> InventoryCountSession:
    session = db.get(InventoryCountSession, session_id)
    if session is None:
        raise ReconciliationError("Sesion no encontrada", 404, "SESSION_NOT_FOUND")
    if (
        session.inventory_campaign_id != campaign_id
        or session.status is not SessionStatus.SUBMITTED
    ):
        raise ReconciliationError(
            "La sesion debe ser SUBMITTED de esta campana", 409, "SESSION_NOT_ELIGIBLE"
        )
    return session


def _apply_selection(
    row: InventoryReconciliation,
    *,
    session_id: uuid.UUID,
    physical: decimal.Decimal,
    damaged: decimal.Decimal,
    actor_id: uuid.UUID,
    mode: SelectionMode,
    now: dt.datetime,
) -> None:
    row.selected_session_id = session_id
    row.selection_mode = mode
    row.selected_by = actor_id
    row.selected_at = now
    row.approved_physical_quantity = physical
    row.damaged_quantity = damaged
    row.version += 1
    if row.expected_quantity < 0:
        row.status = ReconciliationStatus.UNDER_REVIEW
        row.difference_quantity = None
        row.missing_quantity = ZERO
        row.surplus_quantity = ZERO
        return
    difference = physical - row.expected_quantity
    row.difference_quantity = difference
    row.missing_quantity = max(-difference, ZERO)
    row.surplus_quantity = max(difference, ZERO)
    row.status = (
        ReconciliationStatus.MATCHED if difference == 0 else ReconciliationStatus.DIFFERENCE
    )


def _product_ref(product: Product | None, product_id: uuid.UUID) -> dict[str, object]:
    if product is None:
        return {"id": str(product_id), "internal_reference": None, "name": None}
    return {
        "id": str(product.id),
        "internal_reference": product.internal_reference,
        "name": product.name,
    }


def row_payload(
    row: InventoryReconciliation,
    *,
    product: Product | None,
    session_results: list[dict[str, object]],
) -> dict[str, object]:
    """Fila de conciliacion SIN campos monetarios (F008 no calcula dinero)."""
    return {
        "product": _product_ref(product, row.product_id),
        "expected_quantity": _q(row.expected_quantity),
        "session_results": session_results,
        "selected_session_id": _ref(row.selected_session_id),
        "selection_mode": row.selection_mode.value if row.selection_mode else None,
        "selected_by": _ref(row.selected_by),
        "selected_at": _iso(row.selected_at),
        "approved_physical_quantity": _q(row.approved_physical_quantity),
        "difference_quantity": _q(row.difference_quantity),
        "missing_quantity": _q(row.missing_quantity),
        "surplus_quantity": _q(row.surplus_quantity),
        "damaged_quantity": _q(row.damaged_quantity),
        "status": row.status.value,
        "reason": row.reason,
        "observation": row.observation,
        "approved_by": _ref(row.approved_by),
        "approved_at": _iso(row.approved_at),
        "version": row.version,
    }


def _session_results(
    sessions: list[InventoryCountSession],
    effective: comparison_service.EffectiveMap,
    product_id: uuid.UUID,
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for session in sessions:
        physical, damaged = effective.get(session.id, {}).get(product_id, (ZERO, ZERO))
        results.append(
            {
                "session_id": str(session.id),
                "session_number": session.session_number,
                "session_type": session.session_type.value,
                "physical_quantity": str(physical),
                "damaged_quantity": str(damaged),
            }
        )
    return results


def _campaign_block(campaign: InventoryCampaign) -> dict[str, object]:
    return {
        "id": str(campaign.id),
        "code": campaign.code,
        "name": campaign.name,
        "status": campaign.status.value,
        "version": campaign.version,
        "snapshot_frozen_at": _iso(campaign.snapshot_frozen_at),
        "snapshot_sha256": campaign.snapshot_sha256,
        "reconciliation_prepared_at": _iso(campaign.reconciliation_prepared_at),
        "reconciliation_source_sha256": campaign.reconciliation_source_sha256,
    }


def preview(db: Session, campaign_id: uuid.UUID) -> dict[str, object]:
    """Vista previa completa SIN persistir y SIN costos (§17)."""
    campaign = campaign_service.get_campaign(db, campaign_id)
    sessions = comparison_service.submitted_sessions(db, campaign_id)
    universe = comparison_service.product_universe(db, campaign_id)
    snapshot = comparison_service.snapshot_expected(db, campaign_id)
    effective = comparison_service.effective_map(db, campaign_id)
    unresolved = comparison_service.unresolved_unknowns(db, campaign_id)
    negatives = comparison_service.negative_expected(universe)

    warnings: list[str] = []
    if negatives:
        warnings.append("NEGATIVE_EXPECTED_QUANTITY")
    if not sessions:
        warnings.append("NO_SUBMITTED_SESSIONS")

    products: list[dict[str, object]] = []
    for product_id, expected, product in comparison_service.products_sorted(db, universe):
        products.append(
            {
                "kind": "PRODUCT",
                "product": _product_ref(product, product_id),
                "expected_quantity": str(expected),
                "is_extra": product_id not in snapshot,
                "negative_expected": expected < 0,
                "sessions": [
                    {
                        "session_id": str(session.id),
                        "physical_quantity": str(
                            effective.get(session.id, {}).get(product_id, (ZERO, ZERO))[0]
                        ),
                        "damaged_quantity": str(
                            effective.get(session.id, {}).get(product_id, (ZERO, ZERO))[1]
                        ),
                    }
                    for session in sessions
                ],
            }
        )

    return {
        "campaign": _campaign_block(campaign),
        "submitted_sessions": comparison_service.session_summaries(db, sessions),
        "unresolved_unknown_count": len(unresolved),
        "warnings": warnings,
        "products": products,
        "unresolved_unknowns": [
            {
                "kind": "UNRESOLVED_UNKNOWN",
                "session_id": str(unknown.session_id),
                "scanned_code": unknown.scanned_code,
                "quantity": str(unknown.quantity),
            }
            for unknown in unresolved
        ],
    }


def prepare(
    db: Session,
    *,
    actor_id: uuid.UUID,
    campaign_id: uuid.UUID,
    expected_version: int,
    refresh: bool = False,
) -> dict[str, object]:
    """Prepara (o refresca) la conciliacion de la campana (§20-§25)."""
    campaign = campaign_service.lock_campaign(db, campaign_id)
    campaign_service.require_not_closed(campaign)
    fingerprint = comparison_service.source_fingerprint(db, campaign_id)
    already_prepared = (
        campaign.reconciliation_prepared_at is not None
        and campaign.reconciliation_source_sha256 == fingerprint
    )
    if already_prepared and not refresh:
        return {
            "already_prepared": True,
            "refreshed": False,
            **_campaign_block(campaign),
            "submitted_session_count": len(
                comparison_service.submitted_sessions(db, campaign_id)
            ),
            "product_count": int(
                db.execute(
                    select(func.count())
                    .select_from(InventoryReconciliation)
                    .where(InventoryReconciliation.inventory_campaign_id == campaign_id)
                ).scalar_one()
            ),
        }
    if campaign.status is CampaignStatus.APPROVED:
        raise ReconciliationError(
            "La conciliacion esta aprobada: operacion no permitida",
            409,
            "RECONCILIATION_APPROVED",
        )
    campaign_service.check_version(campaign, expected_version)
    if campaign.status not in PREPARE_STATUSES:
        raise ReconciliationError(
            "La campana no admite conciliacion en su estado actual",
            409,
            "CAMPAIGN_NOT_RECONCILABLE",
        )
    if not comparison_service.submitted_sessions(db, campaign_id):
        raise ReconciliationError(
            "La campana no tiene sesiones SUBMITTED", 409, "NO_SUBMITTED_SESSIONS"
        )
    source_changed = (
        campaign.reconciliation_prepared_at is not None and fingerprint != (
            campaign.reconciliation_source_sha256 or ""
        )
    )
    if source_changed and not refresh:
        raise ReconciliationError(
            "La fuente de conciliacion cambio: use refresh=true",
            409,
            "RECONCILIATION_SOURCE_CHANGED",
        )

    is_refresh = campaign.reconciliation_prepared_at is not None
    universe = comparison_service.product_universe(db, campaign_id)
    existing = {row.product_id: row for row in _locked_rows(db, campaign_id)}
    for product_id, expected in universe.items():
        row = existing.get(product_id)
        if row is None:
            db.add(
                InventoryReconciliation(
                    inventory_campaign_id=campaign_id,
                    product_id=product_id,
                    expected_quantity=expected,
                    status=ReconciliationStatus.PENDING,
                    version=1,
                )
            )
            continue
        row.expected_quantity = expected
        if is_refresh:
            row.approved_physical_quantity = None
            row.difference_quantity = None
            row.damaged_quantity = ZERO
            row.missing_quantity = ZERO
            row.surplus_quantity = ZERO
            row.selected_session_id = None
            row.selection_mode = None
            row.selected_by = None
            row.selected_at = None
            row.status = ReconciliationStatus.PENDING
            row.version += 1
    db.flush()

    now = _now()
    campaign.reconciliation_prepared_at = now
    campaign.reconciliation_source_sha256 = fingerprint
    if campaign.status is CampaignStatus.SUBMITTED:
        campaign.status = CampaignStatus.UNDER_REVIEW
    campaign.version += 1
    audit_service.record(
        db,
        actor_user_id=actor_id,
        action=(
            audit_service.RECONCILIATION_REFRESHED
            if is_refresh
            else audit_service.RECONCILIATION_PREPARED
        ),
        entity_type="inventory_campaign",
        entity_id=campaign.id,
        metadata={
            "campaign_id": str(campaign.id),
            "source_sha256": fingerprint,
            "product_count": len(universe),
            "submitted_session_count": len(
                comparison_service.submitted_sessions(db, campaign_id)
            ),
            "campaign_version": campaign.version,
            "refresh": is_refresh,
        },
    )
    db.commit()
    return {
        "already_prepared": False,
        "refreshed": is_refresh,
        **_campaign_block(campaign),
        "submitted_session_count": len(comparison_service.submitted_sessions(db, campaign_id)),
        "product_count": len(universe),
    }


def list_reconciliation(
    db: Session, campaign_id: uuid.UUID, *, limit: int = 50, offset: int = 0
) -> dict[str, object]:
    campaign = campaign_service.get_campaign(db, campaign_id)
    total = int(
        db.execute(
            select(func.count())
            .select_from(InventoryReconciliation)
            .where(InventoryReconciliation.inventory_campaign_id == campaign_id)
        ).scalar_one()
    )
    rows = _read_rows(db, campaign_id)
    sessions = comparison_service.submitted_sessions(db, campaign_id)
    effective = comparison_service.effective_map(db, campaign_id)
    product_ids = [row.product_id for row in rows]
    products: dict[uuid.UUID, Product] = {}
    if product_ids:
        products = {
            row.id: row
            for row in db.execute(select(Product).where(Product.id.in_(product_ids))).scalars()
        }
    page = rows[offset : offset + limit]
    items = [
        row_payload(
            row,
            product=products.get(row.product_id),
            session_results=_session_results(sessions, effective, row.product_id),
        )
        for row in page
    ]
    return {
        "campaign": _campaign_block(campaign),
        "submitted_sessions": comparison_service.session_summaries(db, sessions),
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": items,
    }


def list_sessions(
    db: Session, campaign_id: uuid.UUID, *, limit: int = 100, offset: int = 0
) -> list[dict[str, object]]:
    campaign_service.get_campaign(db, campaign_id)
    return comparison_service.session_summaries(
        db,
        comparison_service.submitted_sessions(
            db, campaign_id, limit=limit, offset=offset
        ),
    )


def select_default_session(
    db: Session,
    *,
    actor_id: uuid.UUID,
    campaign_id: uuid.UUID,
    session_id: uuid.UUID,
    expected_version: int,
) -> dict[str, object]:
    """Elige la sesion SUBMITTED como conteo oficial de TODOS los productos."""
    campaign = campaign_service.lock_campaign(db, campaign_id)
    campaign_service.require_not_closed(campaign)
    campaign_service.check_version(campaign, expected_version)
    if campaign.status is CampaignStatus.APPROVED:
        raise ReconciliationError(
            "La conciliacion esta aprobada: operacion no permitida",
            409,
            "RECONCILIATION_APPROVED",
        )
    if campaign.status is not CampaignStatus.UNDER_REVIEW:
        raise ReconciliationError(
            "Prepare la conciliacion antes de elegir sesion",
            409,
            "RECONCILIATION_NOT_READY",
        )
    _eligible_session(db, campaign_id, session_id)
    rows = _locked_rows(db, campaign_id)
    if not rows:
        raise ReconciliationError(
            "La conciliacion no esta preparada", 409, "RECONCILIATION_NOT_PREPARED"
        )
    effective = comparison_service.effective_map(db, campaign_id)
    session_effective = effective.get(session_id, {})
    now = _now()
    for row in rows:
        physical, damaged = session_effective.get(row.product_id, (ZERO, ZERO))
        _apply_selection(
            row,
            session_id=session_id,
            physical=physical,
            damaged=damaged,
            actor_id=actor_id,
            mode=SelectionMode.DEFAULT_SESSION,
            now=now,
        )
    campaign.version += 1
    audit_service.record(
        db,
        actor_user_id=actor_id,
        action=audit_service.RECONCILIATION_DEFAULT_SESSION_SELECTED,
        entity_type="inventory_campaign",
        entity_id=campaign.id,
        metadata={
            "campaign_id": str(campaign.id),
            "session_id": str(session_id),
            "products_updated": len(rows),
            "campaign_version": campaign.version,
            "selection_mode": SelectionMode.DEFAULT_SESSION.value,
        },
    )
    db.commit()
    return {
        "session_id": str(session_id),
        "products_updated": len(rows),
        "campaign_version": campaign.version,
        "selection_mode": SelectionMode.DEFAULT_SESSION.value,
    }


def override_product(
    db: Session,
    *,
    actor_id: uuid.UUID,
    campaign_id: uuid.UUID,
    product_id: uuid.UUID,
    selected_session_id: uuid.UUID,
    expected_version: int,
    reason: str | None = None,
    observation: str | None = None,
) -> dict[str, object]:
    """Selecciona el conteo oficial de UN producto (o edita reason/observation)."""
    campaign = campaign_service.lock_campaign(db, campaign_id)
    campaign_service.require_not_closed(campaign)
    if campaign.status is CampaignStatus.APPROVED:
        raise ReconciliationError(
            "La conciliacion esta aprobada: operacion no permitida",
            409,
            "RECONCILIATION_APPROVED",
        )
    if campaign.status is not CampaignStatus.UNDER_REVIEW:
        raise ReconciliationError(
            "Prepare la conciliacion antes de editar productos",
            409,
            "RECONCILIATION_NOT_READY",
        )
    _eligible_session(db, campaign_id, selected_session_id)
    row = db.execute(
        select(InventoryReconciliation)
        .where(
            InventoryReconciliation.inventory_campaign_id == campaign_id,
            InventoryReconciliation.product_id == product_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise ReconciliationError(
            "Producto sin conciliacion en esta campana",
            404,
            "RECONCILIATION_PRODUCT_NOT_FOUND",
        )
    if row.version != expected_version:
        raise ReconciliationError("Conflicto de version", 409, "VERSION_CONFLICT")

    new_reason = row.reason
    if reason is not None:
        stripped = reason.strip()
        new_reason = stripped or None
    new_observation = row.observation
    if observation is not None:
        stripped_obs = observation.strip()
        new_observation = stripped_obs or None

    effective = comparison_service.effective_map(db, campaign_id)
    physical, damaged = effective.get(selected_session_id, {}).get(product_id, (ZERO, ZERO))
    if (
        row.expected_quantity >= 0
        and (physical - row.expected_quantity) != 0
        and new_reason is None
    ):
        raise ReconciliationError(
            "Toda diferencia requiere un reason justificado",
            409,
            "DIFFERENCE_REASON_REQUIRED",
            {"product_id": str(product_id)},
        )

    row.reason = new_reason
    row.observation = new_observation
    _apply_selection(
        row,
        session_id=selected_session_id,
        physical=physical,
        damaged=damaged,
        actor_id=actor_id,
        mode=SelectionMode.PRODUCT_OVERRIDE,
        now=_now(),
    )
    db.flush()
    audit_service.record(
        db,
        actor_user_id=actor_id,
        action=audit_service.RECONCILIATION_PRODUCT_OVERRIDDEN,
        entity_type="inventory_campaign",
        entity_id=campaign.id,
        metadata={
            "campaign_id": str(campaign.id),
            "product_id": str(product_id),
            "session_id": str(selected_session_id),
            "selection_mode": SelectionMode.PRODUCT_OVERRIDE.value,
            "reconciliation_row_version": row.version,
        },
    )
    db.commit()
    product = db.get(Product, product_id)
    sessions = comparison_service.submitted_sessions(db, campaign_id)
    return row_payload(
        row,
        product=product,
        session_results=_session_results(sessions, effective, product_id),
    )
