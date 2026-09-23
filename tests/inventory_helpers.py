"""Helpers para los tests de inventario (F004)."""

from __future__ import annotations

import contextlib
import datetime as dt
import decimal
import uuid
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import delete, or_, select
from sqlalchemy import false as sa_false

from app.db.session import SessionLocal
from app.main import app
from app.models import (
    AuditEvent,
    ImportBatch,
    InventoryAssignment,
    InventoryCampaign,
    InventoryCountEvent,
    InventoryCountSession,
    InventoryCountTotal,
    InventoryDamage,
    InventoryExtraItem,
    InventoryRecount,
    InventorySnapshotItem,
    InventoryUnknownCode,
    Location,
    Product,
    StockSnapshot,
    User,
    UserRole,
)
from app.models.enums import ImportBatchStatus, ImportBatchType
from app.services.auth import rbac_service
from tests.auth_helpers import override_auth

client = TestClient(app)

PERMS_READ = {"inventory.read"}
PERMS_CREATE = {"inventory.create"}
PERMS_ASSIGN = {"inventory.assign"}
PERMS_MONITOR = {"inventory.monitor"}
PERMS_CLOSE = {"inventory.close"}
PERMS_REOPEN = {"inventory.reopen"}
PERMS_EXPECTED = {"inventory.expected.read"}
PERMS_RECONCILE = {"inventory.reconcile"}
PERMS_DAMAGE_REVIEW = {"damage.review"}
PERMS_DAMAGE_REPORT = {"damage.report"}
PERMS_COUNT = {"inventory.count"}
PERMS_RECOUNT = {"inventory.recount"}
PERMS_READ_RECOUNT = {"inventory.read", "inventory.recount"}


def as_user(permissions: set[str], *, roles: tuple[str, ...] = ()) -> uuid.UUID:
    """Crea un usuario real (para auditoria) y lo usa como identidad autenticada."""
    user_id = create_test_user(roles=roles)
    override_auth(permissions, user_id=user_id)
    return user_id


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def create_test_user(*, roles: tuple[str, ...] = (), active: bool = True) -> uuid.UUID:
    with SessionLocal() as db:
        user = User(
            auth_user_id=uuid.uuid4(),
            email=f"test-auth-{uuid.uuid4()}@example.invalid",
            display_name="Usuario Test",
            is_active=active,
        )
        db.add(user)
        db.flush()
        for role in roles:
            rbac_service.assign_role(db, user.id, role)
        db.commit()
        return user.id


def create_operator_user() -> uuid.UUID:
    return create_test_user(roles=("OPERATOR",))


def create_inactive_user() -> uuid.UUID:
    return create_test_user(active=False)


def create_test_location(*, name: str | None = None, code: str | None = None) -> uuid.UUID:
    with SessionLocal() as db:
        location = Location(
            name=name or f"test-loc-{uuid.uuid4()}",
            code=code,
            active=True,
        )
        db.add(location)
        db.commit()
        return location.id


def create_test_source_batch(
    *,
    quantities: tuple[tuple[str, str], ...] = (("P1", "5"), ("P2", "0"), ("P3", "3")),
    with_location: bool = False,
    import_type: ImportBatchType = ImportBatchType.PRODUCTS,
    status: ImportBatchStatus = ImportBatchStatus.COMPLETED,
    with_stock: bool = True,
) -> uuid.UUID:
    """Crea un lote de importacion de prueba con productos y stock_snapshots."""
    location_id = create_test_location() if with_location else None
    with SessionLocal() as db:
        batch = ImportBatch(
            import_type=import_type,
            source_filename=f"test-batch-{uuid.uuid4()}.xlsx",
            source_sha256=uuid.uuid4().hex + uuid.uuid4().hex,
            status=status,
            completed_at=_now(),
        )
        db.add(batch)
        db.flush()
        for reference, quantity in quantities:
            product = Product(
                internal_reference=f"TEST-{reference}-{uuid.uuid4().hex[:6]}",
                name=f"Producto {reference}",
                sale_price=decimal.Decimal("10.0000"),
                cost=decimal.Decimal("2.0000"),
                consignment_cost=decimal.Decimal("1.0000"),
                currency="PEN",
                active=True,
            )
            db.add(product)
            db.flush()
            if with_stock:
                db.add(
                    StockSnapshot(
                        import_batch_id=batch.id,
                        product_id=product.id,
                        location_id=location_id,
                        quantity=decimal.Decimal(quantity),
                        source="TEST",
                    )
                )
        db.commit()
        return batch.id


def cleanup_inventory_test_data() -> None:
    """Elimina los datos creados por los tests (nombre/email con prefijo test-).

    Orden seguro ante FKs RESTRICT: hijos antes que padres, undo antes que
    eventos referenciados, y asignaciones por campana o por usuario de prueba.
    """
    with SessionLocal() as db:
        campaign_ids = list(
            db.execute(
                select(InventoryCampaign.id).where(InventoryCampaign.name.like("test-%"))
            ).scalars()
        )
        user_ids = list(
            db.execute(select(User.id).where(User.email.like("test-auth-%"))).scalars()
        )
        batch_ids = list(
            db.execute(
                select(ImportBatch.id).where(ImportBatch.source_filename.like("test-%"))
            ).scalars()
        )
        location_ids = list(
            db.execute(select(Location.id).where(Location.name.like("test-%"))).scalars()
        )
        session_query = select(InventoryCountSession.id)
        if campaign_ids and user_ids:
            session_query = session_query.where(
                or_(
                    InventoryCountSession.inventory_campaign_id.in_(campaign_ids),
                    InventoryCountSession.user_id.in_(user_ids),
                )
            )
        elif campaign_ids:
            session_query = session_query.where(
                InventoryCountSession.inventory_campaign_id.in_(campaign_ids)
            )
        elif user_ids:
            session_query = session_query.where(InventoryCountSession.user_id.in_(user_ids))
        else:
            session_query = session_query.where(sa_false())
        session_ids = list(db.execute(session_query).scalars())

        # F007: los reconteos referencian campanas, usuarios y sesiones (RESTRICT).
        if campaign_ids:
            recount_ids = list(
                db.execute(
                    select(InventoryRecount.id).where(
                        InventoryRecount.inventory_campaign_id.in_(campaign_ids)
                    )
                ).scalars()
            )
            if recount_ids:
                db.execute(delete(AuditEvent).where(AuditEvent.entity_id.in_(recount_ids)))
                db.execute(
                    delete(InventoryRecount).where(InventoryRecount.id.in_(recount_ids))
                )
        if user_ids:
            recount_ids = list(
                db.execute(
                    select(InventoryRecount.id).where(
                        InventoryRecount.assigned_user_id.in_(user_ids)
                    )
                ).scalars()
            )
            if recount_ids:
                db.execute(delete(AuditEvent).where(AuditEvent.entity_id.in_(recount_ids)))
                db.execute(
                    delete(InventoryRecount).where(InventoryRecount.id.in_(recount_ids))
                )

        if session_ids:
            # Evidencia de dano de test: borrar el archivo antes que la fila.
            from app.core.config import get_settings

            evidence_dir = Path(get_settings().EVIDENCE_DIR)
            damage_rows = list(
                db.execute(
                    select(InventoryDamage.id, InventoryDamage.evidence_path).where(
                        InventoryDamage.session_id.in_(session_ids)
                    )
                ).all()
            )
            damage_ids = [row[0] for row in damage_rows]
            damage_paths = [row[1] for row in damage_rows]
            unknown_ids = list(
                db.execute(
                    select(InventoryUnknownCode.id).where(
                        InventoryUnknownCode.session_id.in_(session_ids)
                    )
                ).scalars()
            )
            # Auditorias de resolucion/evidencia (entity_id = damage/unknown).
            linked_ids = damage_ids + unknown_ids
            if linked_ids:
                db.execute(delete(AuditEvent).where(AuditEvent.entity_id.in_(linked_ids)))
            # Primero dannos (FK event_id RESTRICT), luego extras/unknowns.
            db.execute(
                delete(InventoryDamage).where(InventoryDamage.session_id.in_(session_ids))
            )
            db.execute(
                delete(InventoryExtraItem).where(InventoryExtraItem.session_id.in_(session_ids))
            )
            db.execute(
                delete(InventoryUnknownCode).where(
                    InventoryUnknownCode.session_id.in_(session_ids)
                )
            )
            for relative in evidence_paths(evidence_dir, damage_paths):
                with contextlib.suppress(OSError):
                    relative.unlink()
            # Primero los UNDO (FK autorreferencial RESTRICT), luego el resto.
            db.execute(
                delete(InventoryCountEvent).where(
                    InventoryCountEvent.session_id.in_(session_ids),
                    InventoryCountEvent.reverses_event_id.is_not(None),
                )
            )
            db.execute(
                delete(InventoryCountEvent).where(
                    InventoryCountEvent.session_id.in_(session_ids)
                )
            )
            db.execute(
                delete(InventoryCountTotal).where(
                    InventoryCountTotal.session_id.in_(session_ids)
                )
            )
            db.execute(
                delete(InventoryCountSession).where(InventoryCountSession.id.in_(session_ids))
            )

        if campaign_ids:
            db.execute(
                delete(InventorySnapshotItem).where(
                    InventorySnapshotItem.inventory_campaign_id.in_(campaign_ids)
                )
            )
        if campaign_ids:
            db.execute(
                delete(InventoryAssignment).where(
                    InventoryAssignment.inventory_campaign_id.in_(campaign_ids)
                )
            )
        if user_ids:
            db.execute(
                delete(InventoryAssignment).where(InventoryAssignment.user_id.in_(user_ids))
            )
        if campaign_ids:
            db.execute(delete(AuditEvent).where(AuditEvent.entity_id.in_(campaign_ids)))
            db.execute(delete(InventoryCampaign).where(InventoryCampaign.id.in_(campaign_ids)))

        if batch_ids:
            db.execute(delete(StockSnapshot).where(StockSnapshot.import_batch_id.in_(batch_ids)))
            db.execute(delete(ImportBatch).where(ImportBatch.id.in_(batch_ids)))
        db.execute(delete(Product).where(Product.internal_reference.like("TEST-%")))

        if location_ids:
            db.execute(delete(AuditEvent).where(AuditEvent.entity_id.in_(location_ids)))
            db.execute(delete(Location).where(Location.id.in_(location_ids)))

        if user_ids:
            db.execute(delete(UserRole).where(UserRole.user_id.in_(user_ids)))
            db.execute(delete(AuditEvent).where(AuditEvent.entity_id.in_(user_ids)))
            db.execute(delete(User).where(User.id.in_(user_ids)))
        if session_ids:
            db.execute(delete(AuditEvent).where(AuditEvent.entity_id.in_(session_ids)))
        db.commit()


def batch_products(batch_id: str) -> list[tuple[uuid.UUID, str]]:
    """Devuelve [(product_id, internal_reference)] del lote de prueba."""
    with SessionLocal() as db:
        rows = db.execute(
            select(Product.id, Product.internal_reference)
            .join(StockSnapshot, StockSnapshot.product_id == Product.id)
            .where(StockSnapshot.import_batch_id == uuid.UUID(batch_id))
            .order_by(Product.internal_reference)
        ).all()
        return [(row[0], row[1]) for row in rows]


def create_standalone_product(reference: str) -> uuid.UUID:
    """Producto del maestro que NO pertenece a ningun snapshot (caso EXTRA)."""
    with SessionLocal() as db:
        product = Product(
            internal_reference=reference, name=f"Extra {reference}", currency="PEN", active=True
        )
        db.add(product)
        db.commit()
        return product.id


def evidence_paths(evidence_dir: Path, relatives: list[str | None]) -> list[Path]:
    """Resuelve rutas relativas de evidencia sin salir del directorio."""
    base = evidence_dir.resolve()
    result: list[Path] = []
    for relative in relatives:
        if not relative:
            continue
        candidate = (base / relative).resolve()
        if candidate.parent == base and candidate.is_file():
            result.append(candidate)
    return result
