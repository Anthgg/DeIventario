"""Sesion de base de datos con SQLAlchemy 2.

No define modelos ni ejecuta DDL/migraciones: solo prepara el engine y la
fabrica de sesiones que usaran las fases siguientes.
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

_settings = get_settings()

engine: Engine = create_engine(
    _settings.sqlalchemy_url,
    echo=_settings.DATABASE_ECHO,
    pool_pre_ping=True,
    pool_size=_settings.DATABASE_POOL_SIZE,
    max_overflow=_settings.DATABASE_MAX_OVERFLOW,
)

SessionLocal: sessionmaker[Session] = sessionmaker(
    bind=engine,
    class_=Session,
    autoflush=False,
    expire_on_commit=False,
)


def get_db() -> Iterator[Session]:
    """Dependency de FastAPI: entrega una sesion y la cierra al finalizar."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
