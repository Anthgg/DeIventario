"""Entorno Alembic: reutiliza la configuracion central del backend.

La URL nunca se escribe aqui ni en alembic.ini: se obtiene de
``app.core.config.get_settings().sqlalchemy_url`` (que ya fuerza
``sslmode=require``).
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool

import app.models  # noqa: F401  (registrar todos los modelos en Base.metadata)
from app.core.config import get_settings
from app.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    """URL de conexion desde la configuracion central (sin duplicar)."""
    return get_settings().sqlalchemy_url


def run_migrations_offline() -> None:
    """Modo offline: emite SQL sin conectarse a la base."""
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Modo online: usa el engine del proyecto (psycopg3 + TLS obligatorio)."""
    connectable = context.config.attributes.get("connection")
    if connectable is None:
        from sqlalchemy import create_engine

        connectable = create_engine(get_url(), poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
