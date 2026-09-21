"""Base declarativa central de SQLAlchemy.

Todas las tablas heredan de :class:`Base`. La convencion de nombres garantiza
constraints con nombres estables y predecibles (importante para Alembic).
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid

import sqlalchemy as sa
from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base declarativa unica del proyecto."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def varchar_enum(enum_class: type[enum.Enum]) -> sa.Enum:
    """Enum almacenado como VARCHAR + CHECK (NO enum nativo de PostgreSQL).

    - ``native_enum=False``: no crea tipos PostgreSQL (facilita anadir valores).
    - ``create_constraint=True``: la base valida los valores permitidos.
    """
    return sa.Enum(
        enum_class,
        native_enum=False,
        create_constraint=True,
        length=50,
        values_callable=lambda members: [member.value for member in members],
    )


class UUIDPrimaryKeyMixin:
    """PK UUID generada por la aplicacion de forma segura."""

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)


class CreatedAtMixin:
    """Timestamp de creacion con zona horaria."""

    created_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class TimestampMixin(CreatedAtMixin):
    """Timestamps de creacion y actualizacion con zona horaria."""

    updated_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
    )
