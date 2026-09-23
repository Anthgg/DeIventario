"""Motor de eventos de conteo: QR, manuales, undo, lote e idempotencia.

Reglas clave:
  - El backend es la unica fuente de verdad: cantidades, secuencia y totales
    se calculan aqui; nunca se aceptan del cliente.
  - ``inventory_count_events`` es historial inmutable: corregir = nuevo evento.
  - ``server_sequence`` es autoritativa y se asigna bajo lock de sesion.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import decimal
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import InventoryCountEvent, InventoryCountSession, InventoryCountTotal, Product
from app.models.enums import CountEventType, EventSource, SessionStatus
from app.services.counting import qr_service, session_service
from app.services.counting.errors import CountError
from app.services.inventory import campaign_service

MAX_BATCH_EVENTS = 100

_QR_TYPES = (CountEventType.QR_SCAN, CountEventType.MULTI_QR_SCAN)
_MANUAL_TYPES = (
    CountEventType.MANUAL_ADD,
    CountEventType.MANUAL_SUBTRACT,
    CountEventType.MANUAL_SET,
)
SUPPORTED_TYPES = _QR_TYPES + _MANUAL_TYPES


@dataclasses.dataclass(frozen=True)
class EventInput:
    client_event_uuid: uuid.UUID
    event_type: CountEventType
    scanned_code: str | None = None
    product_id: uuid.UUID | None = None
    quantity: decimal.Decimal | None = None
    occurred_at: dt.datetime | None = None
    source: EventSource | None = None


@dataclasses.dataclass
class EventResult:
    event: InventoryCountEvent
    already_processed: bool


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _q(value: decimal.Decimal | None) -> str | None:
    """Cantidades con escala fija de 4 decimales (formato estable para la PWA)."""
    if value is None:
        return None
    return f"{decimal.Decimal(value):.4f}"


def _signature(
    event_type: CountEventType,
    product_id: uuid.UUID | None,
    quantity: decimal.Decimal | None,
    scanned_code: str | None,
) -> list[str]:
    return [
        event_type.value,
        str(product_id) if product_id is not None else "",
        "" if quantity is None else str(quantity),
        scanned_code or "",
    ]


def _incoming_signature(payload: EventInput) -> list[str]:
    return _signature(
        payload.event_type, payload.product_id, payload.quantity, payload.scanned_code
    )


def _int_quantity(value: decimal.Decimal | None) -> int:
    if value is None:
        raise CountError("quantity es obligatoria", 422)
    dec = decimal.Decimal(str(value))
    if dec != dec.to_integral_value():
        raise CountError("quantity debe ser un entero (conteo por unidades)", 422)
    return int(dec)


def _assert_open(session: InventoryCountSession) -> None:
    if session.status is not SessionStatus.IN_PROGRESS:
        raise CountError("La sesion no admite mas eventos", 409, "SESSION_NOT_OPEN")


def _assert_actor_owns(
    db: Session, session: InventoryCountSession, actor_id: uuid.UUID
) -> None:
    if session.user_id != actor_id:
        raise CountError("No eres el dueno de esta sesion", 403)
    assignment = campaign_service.active_assignment(db, session.inventory_campaign_id)
    if assignment is None or assignment.user_id != actor_id:
        raise CountError("La asignacion activa no te corresponde", 409, "ASSIGNMENT_NOT_ACTIVE")


def _find_by_uuid(db: Session, client_event_uuid: uuid.UUID) -> InventoryCountEvent | None:
    return db.execute(
        select(InventoryCountEvent)
        .where(InventoryCountEvent.client_event_uuid == client_event_uuid)
        .limit(1)
    ).scalar_one_or_none()


def _idempotent_result(
    existing: InventoryCountEvent, session_id: uuid.UUID, incoming: list[str]
) -> EventResult:
    if existing.session_id != session_id:
        raise CountError(
            "client_event_uuid ya fue usado en otra sesion", 409, "IDEMPOTENCY_CONFLICT"
        )
    stored = (existing.metadata_ or {}).get("sig")
    if stored != incoming:
        raise CountError(
            "client_event_uuid reutilizado con payload distinto", 409, "IDEMPOTENCY_CONFLICT"
        )
    return EventResult(existing, True)


def _resolve_product(db: Session, payload: EventInput) -> uuid.UUID:
    resolved: uuid.UUID | None = None
    if payload.scanned_code is not None:
        code = qr_service.normalize_scanned_code(payload.scanned_code)
        product = db.execute(
            select(Product).where(Product.internal_reference == code).limit(1)
        ).scalar_one_or_none()
        if product is None:
            # F006 implementara el workflow completo de codigos desconocidos.
            raise CountError(
                "Codigo no soportado todavia", 409, "UNKNOWN_CODE_NOT_YET_SUPPORTED"
            )
        resolved = product.id
    if payload.product_id is not None:
        if db.get(Product, payload.product_id) is None:
            raise CountError("Producto no encontrado", 404)
        if resolved is not None and resolved != payload.product_id:
            raise CountError(
                "scanned_code y product_id se contradicen", 409, "PRODUCT_MISMATCH"
            )
        resolved = payload.product_id
    if resolved is None:
        raise CountError("Se requiere product_id o scanned_code", 422)
    return resolved


def _row_total(db: Session, session_id: uuid.UUID, product_id: uuid.UUID) -> InventoryCountTotal:
    total = db.execute(
        select(InventoryCountTotal)
        .where(
            InventoryCountTotal.session_id == session_id,
            InventoryCountTotal.product_id == product_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if total is None:
        total = InventoryCountTotal(
            session_id=session_id,
            product_id=product_id,
            quantity=decimal.Decimal("0.0000"),
            damaged_quantity=decimal.Decimal("0.0000"),
        )
        db.add(total)
        db.flush()
    return total


def _compute(
    payload: EventInput, previous: decimal.Decimal
) -> tuple[decimal.Decimal, decimal.Decimal]:
    event_type = payload.event_type
    if event_type in _QR_TYPES:
        # QR siempre es +1: nunca se acepta cantidad del cliente.
        return previous + decimal.Decimal("1"), decimal.Decimal("1")
    quantity = _int_quantity(payload.quantity)
    if event_type is CountEventType.MANUAL_ADD:
        if quantity <= 0:
            raise CountError("quantity debe ser mayor que 0", 422)
        return previous + quantity, decimal.Decimal(quantity)
    if event_type is CountEventType.MANUAL_SUBTRACT:
        if quantity <= 0:
            raise CountError("quantity debe ser mayor que 0", 422)
        new_quantity = previous - quantity
        if new_quantity < 0:
            raise CountError(
                "El conteo quedaria negativo", 409, "COUNT_WOULD_BE_NEGATIVE"
            )
        return new_quantity, decimal.Decimal(-quantity)
    if event_type is CountEventType.MANUAL_SET:
        if quantity < 0:
            raise CountError("quantity debe ser mayor o igual que 0", 422)
        target = decimal.Decimal(quantity)
        return target, target - previous
    raise CountError("event_type no soportado en esta fase", 422, "EVENT_TYPE_NOT_SUPPORTED")


def _insert_event(
    db: Session,
    session: InventoryCountSession,
    payload: EventInput,
    product_id: uuid.UUID,
    previous: decimal.Decimal,
    new_quantity: decimal.Decimal,
    delta: decimal.Decimal,
    signature: list[str],
) -> InventoryCountEvent:
    session.last_sequence = session.last_sequence + 1
    default_source = EventSource.CAMERA if payload.event_type in _QR_TYPES else EventSource.MANUAL
    event = InventoryCountEvent(
        session_id=session.id,
        product_id=product_id,
        scanned_code=payload.scanned_code,
        event_type=payload.event_type,
        delta_quantity=delta,
        set_quantity=(
            payload.quantity if payload.event_type is CountEventType.MANUAL_SET else None
        ),
        resulting_quantity=new_quantity,
        previous_quantity=previous,
        source=payload.source or default_source,
        client_event_uuid=payload.client_event_uuid,
        occurred_at=payload.occurred_at or _now(),
        received_at=_now(),
        server_sequence=session.last_sequence,
        metadata_={"sig": signature},
    )
    db.add(event)
    return event


def process_event(
    db: Session,
    *,
    session_id: uuid.UUID,
    actor_id: uuid.UUID,
    payload: EventInput,
    commit: bool = True,
) -> EventResult:
    if payload.event_type not in SUPPORTED_TYPES:
        raise CountError("event_type no soportado en esta fase", 422, "EVENT_TYPE_NOT_SUPPORTED")
    incoming = _incoming_signature(payload)
    existing = _find_by_uuid(db, payload.client_event_uuid)
    if existing is not None:
        return _idempotent_result(existing, session_id, incoming)

    session = session_service.lock_session(db, session_id)
    _assert_open(session)
    session_service.assert_campaign_active(db, session)
    _assert_actor_owns(db, session, actor_id)

    product_id = _resolve_product(db, payload)
    total = _row_total(db, session.id, product_id)
    previous = total.quantity
    new_quantity, delta = _compute(payload, previous)
    event = _insert_event(db, session, payload, product_id, previous, new_quantity, delta, incoming)
    total.quantity = new_quantity
    total.updated_at = _now()
    session.last_activity_at = _now()

    if not commit:
        db.flush()
        return EventResult(event, False)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        winner = _find_by_uuid(db, payload.client_event_uuid)
        if winner is not None:
            return EventResult(winner, True)
        raise CountError("Conflicto al procesar el evento", 409, "EVENT_CONFLICT") from exc
    return EventResult(event, False)


def process_batch(
    db: Session,
    *,
    session_id: uuid.UUID,
    actor_id: uuid.UUID,
    events: list[EventInput],
    max_events: int = MAX_BATCH_EVENTS,
) -> list[EventResult]:
    """Procesa un lote atomico respetando el orden del array."""
    if not events:
        raise CountError("El lote esta vacio", 422)
    if len(events) > max_events:
        raise CountError(
            f"El lote supera el maximo de {max_events} eventos", 422, "BATCH_TOO_LARGE"
        )
    results: list[EventResult] = []
    for index, payload in enumerate(events):
        try:
            results.append(
                process_event(
                    db, session_id=session_id, actor_id=actor_id, payload=payload, commit=False
                )
            )
        except CountError as exc:
            db.rollback()
            raise CountError(
                f"Evento {index}: {exc}", exc.status_code, exc.code, exc.payload
            ) from exc
    db.commit()
    return results


def undo_event(
    db: Session,
    *,
    session_id: uuid.UUID,
    target_event_id: uuid.UUID,
    actor_id: uuid.UUID,
    client_event_uuid: uuid.UUID | None = None,
) -> EventResult:
    session = session_service.lock_session(db, session_id)
    _assert_open(session)
    session_service.assert_campaign_active(db, session)
    _assert_actor_owns(db, session, actor_id)

    target = db.get(InventoryCountEvent, target_event_id)
    if target is None or target.session_id != session.id:
        raise CountError("Evento no encontrado en la sesion", 404)
    if target.event_type is CountEventType.UNDO:
        raise CountError("No se puede deshacer un UNDO", 409, "UNDO_OF_UNDO")

    existing_undo = db.execute(
        select(InventoryCountEvent)
        .where(InventoryCountEvent.reverses_event_id == target.id)
        .limit(1)
    ).scalar_one_or_none()
    if existing_undo is not None:
        return EventResult(existing_undo, True)

    if target.product_id is None:
        raise CountError("El evento no tiene producto asociado", 409)
    reversed_ids = set(
        db.execute(
            select(InventoryCountEvent.reverses_event_id).where(
                InventoryCountEvent.session_id == session.id,
                InventoryCountEvent.reverses_event_id.is_not(None),
            )
        ).scalars()
    )
    product_events = list(
        db.execute(
            select(InventoryCountEvent)
            .where(
                InventoryCountEvent.session_id == session.id,
                InventoryCountEvent.product_id == target.product_id,
                InventoryCountEvent.event_type != CountEventType.UNDO,
            )
            .order_by(InventoryCountEvent.server_sequence)
        ).scalars()
    )
    effective = [event for event in product_events if event.id not in reversed_ids]
    if not effective or effective[-1].id != target.id:
        raise CountError(
            "Solo se puede deshacer el ultimo evento efectivo del producto",
            409,
            "UNDO_NOT_LATEST_PRODUCT_EVENT",
        )

    total = _row_total(db, session.id, target.product_id)
    previous = total.quantity
    resulting = (
        target.previous_quantity
        if target.previous_quantity is not None
        else decimal.Decimal("0")
    )
    delta = resulting - previous
    session.last_sequence = session.last_sequence + 1
    event = InventoryCountEvent(
        session_id=session.id,
        product_id=target.product_id,
        scanned_code=target.scanned_code,
        event_type=CountEventType.UNDO,
        delta_quantity=delta,
        resulting_quantity=resulting,
        previous_quantity=previous,
        source=EventSource.SYSTEM,
        client_event_uuid=client_event_uuid or uuid.uuid4(),
        occurred_at=_now(),
        received_at=_now(),
        server_sequence=session.last_sequence,
        reverses_event_id=target.id,
        metadata_={"sig": ["UNDO", str(target.id)]},
    )
    db.add(event)
    total.quantity = resulting
    total.updated_at = _now()
    session.last_activity_at = _now()
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        winner = db.execute(
            select(InventoryCountEvent)
            .where(InventoryCountEvent.reverses_event_id == target.id)
            .limit(1)
        ).scalar_one_or_none()
        if winner is not None:
            return EventResult(winner, True)
        raise CountError("Conflicto al deshacer el evento", 409) from exc
    return EventResult(event, False)


def event_history(
    db: Session, session_id: uuid.UUID, *, offset: int, limit: int
) -> list[InventoryCountEvent]:
    return list(
        db.execute(
            select(InventoryCountEvent)
            .where(InventoryCountEvent.session_id == session_id)
            .order_by(InventoryCountEvent.server_sequence)
            .offset(offset)
            .limit(limit)
        ).scalars()
    )


def event_payload(event: InventoryCountEvent, *, already_processed: bool) -> dict[str, object]:
    return {
        "event_id": str(event.id),
        "client_event_uuid": str(event.client_event_uuid),
        "server_sequence": event.server_sequence,
        "product_id": str(event.product_id) if event.product_id else None,
        "event_type": event.event_type.value,
        "quantity": _q(event.delta_quantity),
        "previous_quantity": _q(event.previous_quantity),
        "resulting_quantity": _q(event.resulting_quantity),
        "scanned_code": event.scanned_code,
        "occurred_at": event.occurred_at.isoformat() if event.occurred_at else None,
        "received_at": event.received_at.isoformat() if event.received_at else None,
        "reverses_event_id": (
            str(event.reverses_event_id) if event.reverses_event_id else None
        ),
        "already_processed": already_processed,
    }
