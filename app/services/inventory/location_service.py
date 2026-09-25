"""Servicio de ubicaciones fisicas."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Location
from app.services.auth import audit_service


class LocationError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def get_location(db: Session, location_id: uuid.UUID) -> Location:
    location = db.get(Location, location_id)
    if location is None:
        raise LocationError("Ubicacion no encontrada", 404)
    return location


def list_locations(
    db: Session,
    *,
    active: bool | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Location]:
    query = select(Location).order_by(Location.name, Location.id)
    if active is not None:
        query = query.where(Location.active.is_(active))
    return list(db.execute(query.offset(offset).limit(limit)).scalars())


def _assert_code_available(db: Session, code: str | None, exclude_id: uuid.UUID | None) -> None:
    if not code:
        return
    query = select(Location.id).where(Location.code == code)
    if exclude_id is not None:
        query = query.where(Location.id != exclude_id)
    if db.execute(query.limit(1)).scalar_one_or_none() is not None:
        raise LocationError("Ya existe una ubicacion con ese codigo", 409)


def create_location(
    db: Session,
    *,
    actor_id: uuid.UUID,
    name: str,
    code: str | None = None,
    external_ref: str | None = None,
    active: bool = True,
) -> Location:
    if not name.strip():
        raise LocationError("El nombre es obligatorio", 422)
    _assert_code_available(db, code, None)
    location = Location(
        name=name.strip(), code=code or None, external_ref=external_ref, active=active
    )
    db.add(location)
    db.flush()
    audit_service.record(
        db,
        action=audit_service.LOCATION_CREATED,
        actor_user_id=actor_id,
        entity_type="location",
        entity_id=location.id,
        metadata={"code": code, "name": location.name},
    )
    db.commit()
    return location


def update_location(
    db: Session,
    *,
    actor_id: uuid.UUID,
    location_id: uuid.UUID,
    name: str | None = None,
    code: str | None = None,
    external_ref: str | None = None,
    active: bool | None = None,
) -> Location:
    location = get_location(db, location_id)
    if name is not None:
        if not name.strip():
            raise LocationError("El nombre no puede quedar vacio", 422)
        location.name = name.strip()
    if code is not None:
        _assert_code_available(db, code, location_id)
        location.code = code or None
    if external_ref is not None:
        location.external_ref = external_ref
    if active is not None:
        location.active = active
    audit_service.record(
        db,
        action=audit_service.LOCATION_UPDATED,
        actor_user_id=actor_id,
        entity_type="location",
        entity_id=location.id,
        metadata={"code": location.code, "active": location.active},
    )
    db.commit()
    return location
