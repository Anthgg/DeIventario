"""Motor de eventos de conteo: QR, manuales, dano, undo, lote e idempotencia.

Reglas clave:
  - El backend es la unica fuente de verdad: cantidades, secuencia y totales
    se calculan aqui; nunca se aceptan del cliente.
  - ``inventory_count_events`` es historial inmutable: corregir = nuevo evento.
  - ``server_sequence`` es autoritativa y se asigna bajo lock de sesion.
  - El operador nunca es interrumpido: codigos fuera de snapshot (EXTRA) o
    desconocidos (UNKNOWN) se clasifican internamente sin error ni warning.
  - El dano NO reduce la existencia fisica: fisico = buenos + dannados.
  - F006: la resolucion administrativa de un UNKNOWN nunca muta el historial
    ni suma retroactivamente quantity dentro de inventory_count_totals; esa
    combinacion pertenece a la conciliacion (F008).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import decimal
import uuid

from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.models import (
    InventoryCountEvent,
    InventoryCountSession,
    InventoryCountTotal,
    InventoryDamage,
    InventoryExtraItem,
    InventorySnapshotItem,
    InventoryUnknownCode,
    Product,
)
from app.models.enums import (
    CountEventType,
    DamageAction,
    EventSource,
    SessionStatus,
)
from app.services.counting import qr_service, session_service
from app.services.counting.errors import CountError
from app.services.inventory import campaign_service

MAX_BATCH_EVENTS = 100
MAX_REASON_LENGTH = 255
MAX_OBSERVATION_LENGTH = 4000

_QR_TYPES = (CountEventType.QR_SCAN, CountEventType.MULTI_QR_SCAN)
_MANUAL_TYPES = (
    CountEventType.MANUAL_ADD,
    CountEventType.MANUAL_SUBTRACT,
    CountEventType.MANUAL_SET,
)
_DAMAGE_TYPES = (CountEventType.DAMAGE_ADD, CountEventType.DAMAGE_SUBTRACT)
SUPPORTED_TYPES = _QR_TYPES + _MANUAL_TYPES + _DAMAGE_TYPES


@dataclasses.dataclass(frozen=True)
class EventInput:
    client_event_uuid: uuid.UUID
    event_type: CountEventType
    scanned_code: str | None = None
    product_id: uuid.UUID | None = None
    quantity: decimal.Decimal | None = None
    occurred_at: dt.datetime | None = None
    source: EventSource | None = None
    reason: str | None = None
    observation: str | None = None


@dataclasses.dataclass
class EventResult:
    event: InventoryCountEvent
    already_processed: bool


@dataclasses.dataclass
class _Target:
    """Objetivo de un evento: producto conocido O codigo unknown (exactamente uno)."""

    product_id: uuid.UUID | None = None
    unknown: InventoryUnknownCode | None = None
    code: str | None = None

    @property
    def is_unknown(self) -> bool:
        return self.unknown is not None


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


def _normalize_input(payload: EventInput) -> EventInput:
    """Normaliza y valida motivo/observacion de dano ANTES de la firma."""
    if payload.event_type not in _DAMAGE_TYPES:
        return payload
    reason = (payload.reason or "").strip()
    if not reason:
        raise CountError("reason es obligatorio para eventos de dano", 422)
    if len(reason) > MAX_REASON_LENGTH:
        raise CountError(
            f"reason supera la longitud maxima de {MAX_REASON_LENGTH}", 422
        )
    observation_raw = (payload.observation or "").strip()
    if observation_raw and len(observation_raw) > MAX_OBSERVATION_LENGTH:
        raise CountError(
            f"observation supera la longitud maxima de {MAX_OBSERVATION_LENGTH}", 422
        )
    return dataclasses.replace(
        payload, reason=reason, observation=observation_raw or None
    )


def _incoming_signature(payload: EventInput) -> list[str]:
    sig = _signature(
        payload.event_type, payload.product_id, payload.quantity, payload.scanned_code
    )
    if payload.event_type in _DAMAGE_TYPES:
        # La firma del dano incluye los campos relevantes del dano (seccion 63).
        sig += [payload.reason or "", payload.observation or ""]
    return sig


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


def _find_product_by_code(db: Session, code: str) -> Product | None:
    return db.execute(
        select(Product).where(Product.internal_reference == code).limit(1)
    ).scalar_one_or_none()


def _lock_unknown(
    db: Session, session_id: uuid.UUID, code: str
) -> InventoryUnknownCode | None:
    return db.execute(
        select(InventoryUnknownCode)
        .where(
            InventoryUnknownCode.session_id == session_id,
            InventoryUnknownCode.scanned_code == code,
        )
        .with_for_update()
    ).scalar_one_or_none()


def _require_unknown(
    db: Session, session_id: uuid.UUID, code: str
) -> InventoryUnknownCode:
    """Un UNKNOWN solo nace con QR/MULTI_QR: manuales/dano exigen que exista."""
    unknown = _lock_unknown(db, session_id, code)
    if unknown is None:
        raise CountError(
            "El codigo desconocido aun no fue escaneado en esta sesion",
            409,
            "UNKNOWN_CODE_NOT_REGISTERED",
        )
    return unknown


def _get_or_create_unknown(
    db: Session, session_id: uuid.UUID, code: str
) -> InventoryUnknownCode:
    # El lock de sesion serializa la creacion: unico (session_id, scanned_code).
    unknown = _lock_unknown(db, session_id, code)
    if unknown is None:
        unknown = InventoryUnknownCode(
            session_id=session_id,
            scanned_code=code,
            quantity=decimal.Decimal("0.0000"),
            damaged_quantity=decimal.Decimal("0.0000"),
        )
        db.add(unknown)
        db.flush()
    return unknown


def _resolve_target(
    db: Session, session: InventoryCountSession, payload: EventInput
) -> _Target:
    """Resuelve el objetivo sin crear productos ficticios ni derivar por prefijo."""
    code: str | None = None
    if payload.scanned_code is not None:
        code = qr_service.normalize_scanned_code(payload.scanned_code)
        product = _find_product_by_code(db, code)
        if product is not None:
            if payload.product_id is not None and payload.product_id != product.id:
                raise CountError(
                    "scanned_code y product_id se contradicen", 409, "PRODUCT_MISMATCH"
                )
            return _Target(product_id=product.id, code=code)
        # Codigo desconocido: jamas HTTP ni busqueda externa (seccion 13).
        if payload.product_id is not None:
            raise CountError(
                "scanned_code y product_id se contradicen", 409, "PRODUCT_MISMATCH"
            )
        if payload.event_type in _QR_TYPES:
            unknown = _get_or_create_unknown(db, session.id, code)
        else:
            unknown = _require_unknown(db, session.id, code)
        return _Target(unknown=unknown, code=code)

    if payload.product_id is not None:
        if db.get(Product, payload.product_id) is None:
            raise CountError("Producto no encontrado", 404)
        return _Target(product_id=payload.product_id)
    raise CountError("Se requiere product_id o scanned_code", 422)


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


def _existing_total(
    db: Session, session_id: uuid.UUID, product_id: uuid.UUID
) -> InventoryCountTotal | None:
    return db.execute(
        select(InventoryCountTotal)
        .where(
            InventoryCountTotal.session_id == session_id,
            InventoryCountTotal.product_id == product_id,
        )
        .with_for_update()
    ).scalar_one_or_none()


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


def _damage_quantity(payload: EventInput) -> decimal.Decimal:
    quantity = _int_quantity(payload.quantity)
    if quantity <= 0:
        raise CountError("quantity debe ser mayor que 0", 422)
    return decimal.Decimal(quantity)


def _compute_damaged(
    payload: EventInput, physical: decimal.Decimal, damaged: decimal.Decimal
) -> decimal.Decimal:
    quantity = _damage_quantity(payload)
    if payload.event_type is CountEventType.DAMAGE_ADD:
        new_damaged = damaged + quantity
        if new_damaged > physical:
            raise CountError(
                "El dano supera la cantidad fisica", 409, "DAMAGE_EXCEEDS_PHYSICAL_QUANTITY"
            )
        return new_damaged
    new_damaged = damaged - quantity
    if new_damaged < 0:
        raise CountError(
            "El dano quedaria negativo", 409, "DAMAGE_WOULD_BE_NEGATIVE"
        )
    return new_damaged


def _sync_extra(
    db: Session, session: InventoryCountSession, product_id: uuid.UUID, quantity: decimal.Decimal
) -> None:
    """Materializa el EXTRA sincronizando con el total oficial (nunca a ciegas)."""
    snapshot_item = db.execute(
        select(InventorySnapshotItem.id)
        .where(
            InventorySnapshotItem.inventory_campaign_id == session.inventory_campaign_id,
            InventorySnapshotItem.product_id == product_id,
        )
        .limit(1)
    ).scalar_one_or_none()
    if snapshot_item is not None:
        return
    extra = db.execute(
        select(InventoryExtraItem)
        .where(
            InventoryExtraItem.session_id == session.id,
            InventoryExtraItem.product_id == product_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if extra is None:
        db.add(
            InventoryExtraItem(
                session_id=session.id,
                product_id=product_id,
                quantity=quantity,
            )
        )
    else:
        # Refleja el resultado oficial; la fila historica nunca se borra.
        extra.quantity = quantity
        extra.updated_at = _now()


def _insert_event(
    db: Session,
    session: InventoryCountSession,
    payload: EventInput,
    *,
    product_id: uuid.UUID | None,
    scanned_code: str | None,
    previous: decimal.Decimal,
    new_quantity: decimal.Decimal,
    delta: decimal.Decimal,
    signature: list[str],
    damage_delta: decimal.Decimal | None = None,
    previous_damaged: decimal.Decimal | None = None,
    resulting_damaged: decimal.Decimal | None = None,
) -> InventoryCountEvent | EventResult:
    next_sequence = session.last_sequence + 1
    default_source = EventSource.CAMERA if payload.event_type in _QR_TYPES else EventSource.MANUAL
    event = InventoryCountEvent(
        session_id=session.id,
        product_id=product_id,
        scanned_code=scanned_code,
        event_type=payload.event_type,
        delta_quantity=delta,
        set_quantity=(
            payload.quantity if payload.event_type is CountEventType.MANUAL_SET else None
        ),
        resulting_quantity=new_quantity,
        previous_quantity=previous,
        damage_delta_quantity=damage_delta,
        previous_damaged_quantity=previous_damaged,
        resulting_damaged_quantity=resulting_damaged,
        source=payload.source or default_source,
        client_event_uuid=payload.client_event_uuid,
        occurred_at=payload.occurred_at or _now(),
        received_at=_now(),
        server_sequence=next_sequence,
        metadata_={"sig": signature},
    )
    db.add(event)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        winner = _find_by_uuid(db, payload.client_event_uuid)
        if winner is not None:
            return _idempotent_result(winner, session.id, signature)
        raise CountError("Conflicto al procesar el evento", 409, "EVENT_CONFLICT") from exc
    session.last_sequence = next_sequence
    return event


def _process_damage_record(
    db: Session,
    event: InventoryCountEvent,
    *,
    session_id: uuid.UUID,
    actor_id: uuid.UUID,
    payload: EventInput,
    product_id: uuid.UUID | None,
    scanned_code: str | None,
) -> InventoryDamage:
    """Crea el registro inmutable de dano ligado al evento (una fila por evento)."""
    record = InventoryDamage(
        session_id=session_id,
        product_id=product_id,
        scanned_code=scanned_code,
        quantity=_damage_quantity(payload),
        action=(
            DamageAction.ADD
            if payload.event_type is CountEventType.DAMAGE_ADD
            else DamageAction.SUBTRACT
        ),
        reason=payload.reason,
        observation=payload.observation,
        event_id=event.id,
        created_by=actor_id,
    )
    db.add(record)
    return record


def process_event(
    db: Session,
    *,
    session_id: uuid.UUID,
    actor_id: uuid.UUID,
    payload: EventInput,
    commit: bool = True,
) -> EventResult:
    payload = _normalize_input(payload)
    if payload.event_type not in SUPPORTED_TYPES:
        raise CountError("event_type no soportado en esta fase", 422, "EVENT_TYPE_NOT_SUPPORTED")
    incoming = _incoming_signature(payload)
    campaign = session_service.lock_campaign_for_session(db, session_id)
    session = session_service.lock_session(db, session_id)
    _assert_actor_owns(db, session, actor_id)
    existing = _find_by_uuid(db, payload.client_event_uuid)
    if existing is not None:
        return _idempotent_result(existing, session_id, incoming)

    _assert_open(session)
    session_service.assert_campaign_active(db, session, campaign=campaign)

    target = _resolve_target(db, session, payload)

    if payload.event_type in _DAMAGE_TYPES:
        return _process_damage_event(
            db,
            session=session,
            payload=payload,
            target=target,
            actor_id=actor_id,
            signature=incoming,
            commit=commit,
        )

    if target.is_unknown:
        return _process_unknown_physical(
            db, session=session, payload=payload, target=target, signature=incoming, commit=commit
        )
    return _process_product_physical(
        db, session=session, payload=payload, target=target, signature=incoming, commit=commit
    )


def _finish(
    db: Session, event: InventoryCountEvent, signature: list[str], commit: bool
) -> EventResult:
    try:
        if commit:
            db.commit()
        else:
            db.flush()
    except IntegrityError as exc:
        db.rollback()
        winner = _find_by_uuid(db, event.client_event_uuid)
        if winner is not None:
            return _idempotent_result(winner, event.session_id, signature)
        raise CountError("Conflicto al procesar el evento", 409, "EVENT_CONFLICT") from exc
    return EventResult(event, False)


def _process_product_physical(
    db: Session,
    *,
    session: InventoryCountSession,
    payload: EventInput,
    target: _Target,
    signature: list[str],
    commit: bool,
) -> EventResult:
    product_id = target.product_id
    if product_id is None:  # pragma: no cover - garantizado por _resolve_target
        raise CountError("Se requiere product_id o scanned_code", 422)
    total = _row_total(db, session.id, product_id)
    previous = total.quantity
    new_quantity, delta = _compute(payload, previous)
    if new_quantity < total.damaged_quantity:
        raise CountError(
            "El dano supera la cantidad fisica resultante",
            409,
            "DAMAGED_EXCEEDS_RESULTING_PHYSICAL_QUANTITY",
        )
    inserted = _insert_event(
        db,
        session,
        payload,
        product_id=product_id,
        scanned_code=payload.scanned_code,
        previous=previous,
        new_quantity=new_quantity,
        delta=delta,
        signature=signature,
    )
    if isinstance(inserted, EventResult):
        return inserted
    event = inserted
    total.quantity = new_quantity
    total.updated_at = _now()
    session.last_activity_at = _now()
    _sync_extra(db, session, product_id, new_quantity)
    return _finish(db, event, signature, commit)


def _process_unknown_physical(
    db: Session,
    *,
    session: InventoryCountSession,
    payload: EventInput,
    target: _Target,
    signature: list[str],
    commit: bool,
) -> EventResult:
    unknown = target.unknown
    if unknown is None:  # pragma: no cover - garantizado por _resolve_target
        raise CountError("Se requiere product_id o scanned_code", 422)
    previous = unknown.quantity
    new_quantity, delta = _compute(payload, previous)
    if new_quantity < unknown.damaged_quantity:
        raise CountError(
            "El dano supera la cantidad fisica resultante",
            409,
            "DAMAGED_EXCEEDS_RESULTING_PHYSICAL_QUANTITY",
        )
    inserted = _insert_event(
        db,
        session,
        payload,
        product_id=None,
        scanned_code=target.code,
        previous=previous,
        new_quantity=new_quantity,
        delta=delta,
        signature=signature,
    )
    if isinstance(inserted, EventResult):
        return inserted
    event = inserted
    unknown.quantity = new_quantity
    session.last_activity_at = _now()
    return _finish(db, event, signature, commit)


def _process_damage_event(
    db: Session,
    *,
    session: InventoryCountSession,
    payload: EventInput,
    target: _Target,
    actor_id: uuid.UUID,
    signature: list[str],
    commit: bool,
) -> EventResult:
    """Dano: delta fisico 0, delta de dano +/-quantity, registro inmutable."""
    total: InventoryCountTotal | None = None
    if target.is_unknown:
        unknown = target.unknown
        if unknown is None:  # pragma: no cover
            raise CountError("Se requiere product_id o scanned_code", 422)
        physical = unknown.quantity
        damaged = unknown.damaged_quantity
        product_id: uuid.UUID | None = None
        scanned_code: str | None = target.code
    else:
        product_id = target.product_id
        if product_id is None:  # pragma: no cover - garantizado por _resolve_target
            raise CountError("Se requiere product_id o scanned_code", 422)
        total = _existing_total(db, session.id, product_id)
        if total is None:
            physical = decimal.Decimal("0.0000")
            damaged = decimal.Decimal("0.0000")
        else:
            physical = total.quantity
            damaged = total.damaged_quantity
        scanned_code = payload.scanned_code

    # Todas las validaciones ocurren ANTES de tocar el historial.
    new_damaged = _compute_damaged(payload, physical, damaged)
    if not target.is_unknown and total is None:
        # Imposible si _compute_damaged paso (fisico 0 nunca admite dano).
        raise CountError(
            "El dano supera la cantidad fisica", 409, "DAMAGE_EXCEEDS_PHYSICAL_QUANTITY"
        )
    damage_delta = new_damaged - damaged  # +quantity en ADD, -quantity en SUBTRACT

    inserted = _insert_event(
        db,
        session,
        payload,
        product_id=product_id,
        scanned_code=scanned_code if target.is_unknown else payload.scanned_code,
        previous=physical,
        new_quantity=physical,  # la existencia fisica NO cambia con el dano
        delta=decimal.Decimal("0"),
        signature=signature,
        damage_delta=damage_delta,
        previous_damaged=damaged,
        resulting_damaged=new_damaged,
    )
    if isinstance(inserted, EventResult):
        return inserted
    event = inserted
    if target.unknown is not None:
        target.unknown.damaged_quantity = new_damaged
    elif total is not None:
        total.damaged_quantity = new_damaged
        total.updated_at = _now()

    _process_damage_record(
        db,
        event,
        session_id=session.id,
        actor_id=actor_id,
        payload=payload,
        product_id=product_id,
        scanned_code=scanned_code if target.is_unknown else None,
    )
    session.last_activity_at = _now()
    return _finish(db, event, signature, commit)


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


def _target_match(target: InventoryCountEvent) -> ColumnElement[bool]:
    """Dominio del objetivo: producto conocido O codigo unknown (product_id NULL)."""
    if target.product_id is not None:
        return InventoryCountEvent.product_id == target.product_id
    if target.scanned_code:
        return and_(
            InventoryCountEvent.product_id.is_(None),
            InventoryCountEvent.scanned_code == target.scanned_code,
        )
    raise CountError("El evento no tiene producto asociado", 409)


def _domain_events(
    db: Session, session: InventoryCountSession, target: InventoryCountEvent
) -> list[InventoryCountEvent]:
    """Eventos efectivos del MISMO dominio (fisico vs dano) del mismo objetivo.

    El orden fisico y de dano comparte server_sequence, pero 'latest relevant
    event' se evalua por dominio para no romper consistencia (seccion 34).
    """
    reversed_ids = set(
        db.execute(
            select(InventoryCountEvent.reverses_event_id).where(
                InventoryCountEvent.session_id == session.id,
                InventoryCountEvent.reverses_event_id.is_not(None),
            )
        ).scalars()
    )
    is_damage = target.event_type in _DAMAGE_TYPES
    match = _target_match(target)
    if is_damage:
        type_filter = InventoryCountEvent.event_type.in_(_DAMAGE_TYPES)
    else:
        type_filter = InventoryCountEvent.event_type.notin_(_DAMAGE_TYPES)
    events = list(
        db.execute(
            select(InventoryCountEvent)
            .where(
                InventoryCountEvent.session_id == session.id,
                match,
                type_filter,
                InventoryCountEvent.event_type != CountEventType.UNDO,
            )
            .order_by(InventoryCountEvent.server_sequence)
        ).scalars()
    )
    return [event for event in events if event.id not in reversed_ids]


def undo_event(
    db: Session,
    *,
    session_id: uuid.UUID,
    target_event_id: uuid.UUID,
    actor_id: uuid.UUID,
    client_event_uuid: uuid.UUID | None = None,
) -> EventResult:
    campaign = session_service.lock_campaign_for_session(db, session_id)
    session = session_service.lock_session(db, session_id)
    _assert_open(session)
    session_service.assert_campaign_active(db, session, campaign=campaign)
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

    effective = _domain_events(db, session, target)
    if not effective or effective[-1].id != target.id:
        is_damage = target.event_type in _DAMAGE_TYPES
        raise CountError(
            "Solo se puede deshacer el ultimo evento efectivo del objetivo",
            409,
            "UNDO_NOT_LATEST_DAMAGE_EVENT" if is_damage else "UNDO_NOT_LATEST_PRODUCT_EVENT",
        )

    session.last_sequence = session.last_sequence + 1
    if target.event_type in _DAMAGE_TYPES:
        event = _undo_damage(db, session, target, client_event_uuid)
    else:
        event = _undo_physical(db, session, target, client_event_uuid)
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


def _undo_physical(
    db: Session,
    session: InventoryCountSession,
    target: InventoryCountEvent,
    client_event_uuid: uuid.UUID | None,
) -> InventoryCountEvent:
    """Restaura la cantidad fisica; nunca deja damaged > physical."""
    unknown: InventoryUnknownCode | None = None
    total: InventoryCountTotal | None = None
    if target.product_id is not None:
        total = _row_total(db, session.id, target.product_id)
        previous = total.quantity
        damaged = total.damaged_quantity
    else:
        if target.scanned_code:
            unknown = _lock_unknown(db, session.id, target.scanned_code)
        if unknown is None:
            raise CountError("El codigo desconocido no existe en la sesion", 404)
        previous = unknown.quantity
        damaged = unknown.damaged_quantity

    resulting = (
        target.previous_quantity if target.previous_quantity is not None else decimal.Decimal("0")
    )
    if resulting < damaged:
        raise CountError(
            "El dano supera la cantidad fisica resultante",
            409,
            "DAMAGED_EXCEEDS_RESULTING_PHYSICAL_QUANTITY",
        )
    delta = resulting - previous
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
    db.flush()
    if total is not None and target.product_id is not None:
        total.quantity = resulting
        total.updated_at = _now()
        _sync_extra(db, session, target.product_id, resulting)
    elif unknown is not None:
        unknown.quantity = resulting
    return event


def _undo_damage(
    db: Session,
    session: InventoryCountSession,
    target: InventoryCountEvent,
    client_event_uuid: uuid.UUID | None,
) -> InventoryCountEvent:
    """Restaura previous_damaged_quantity del evento de dano original."""
    if target.previous_damaged_quantity is None:
        raise CountError("El evento de dano no registra estado previo", 409)
    resulting_damaged = target.previous_damaged_quantity

    unknown: InventoryUnknownCode | None = None
    total: InventoryCountTotal | None = None
    if target.product_id is not None:
        total = _existing_total(db, session.id, target.product_id)
        physical = total.quantity if total is not None else decimal.Decimal("0.0000")
        damaged = total.damaged_quantity if total is not None else decimal.Decimal("0.0000")
    else:
        if target.scanned_code:
            unknown = _lock_unknown(db, session.id, target.scanned_code)
        if unknown is None:
            raise CountError("El codigo desconocido no existe en la sesion", 404)
        physical = unknown.quantity
        damaged = unknown.damaged_quantity

    if resulting_damaged > physical:
        raise CountError(
            "El dano supera la cantidad fisica resultante",
            409,
            "DAMAGED_EXCEEDS_RESULTING_PHYSICAL_QUANTITY",
        )
    damage_delta = resulting_damaged - damaged
    event = InventoryCountEvent(
        session_id=session.id,
        product_id=target.product_id,
        scanned_code=target.scanned_code,
        event_type=CountEventType.UNDO,
        delta_quantity=decimal.Decimal("0"),
        resulting_quantity=physical,
        previous_quantity=physical,
        damage_delta_quantity=damage_delta,
        previous_damaged_quantity=damaged,
        resulting_damaged_quantity=resulting_damaged,
        source=EventSource.SYSTEM,
        client_event_uuid=client_event_uuid or uuid.uuid4(),
        occurred_at=_now(),
        received_at=_now(),
        server_sequence=session.last_sequence,
        reverses_event_id=target.id,
        metadata_={"sig": ["UNDO", str(target.id)]},
    )
    db.add(event)
    db.flush()
    if total is not None:
        total.damaged_quantity = resulting_damaged
        total.updated_at = _now()
    elif unknown is not None:
        unknown.damaged_quantity = resulting_damaged
    return event


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
        "damage_delta_quantity": _q(event.damage_delta_quantity),
        "previous_damaged_quantity": _q(event.previous_damaged_quantity),
        "resulting_damaged_quantity": _q(event.resulting_damaged_quantity),
        "scanned_code": event.scanned_code,
        "occurred_at": event.occurred_at.isoformat() if event.occurred_at else None,
        "received_at": event.received_at.isoformat() if event.received_at else None,
        "reverses_event_id": (
            str(event.reverses_event_id) if event.reverses_event_id else None
        ),
        "already_processed": already_processed,
    }
