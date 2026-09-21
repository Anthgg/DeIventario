"""Endpoints de salud (liveness y comprobacion read-only de la base de datos)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_db

router = APIRouter(tags=["health"])

DbSession = Annotated[Session, Depends(get_db)]


@router.get("/health")
def read_health() -> dict[str, str]:
    """Liveness: confirma que el proceso responde."""
    settings = get_settings()
    return {
        "status": "ok",
        "service": "inventario-dedalo-api",
        "environment": settings.APP_ENV,
    }


@router.get("/health/database")
def read_database_health(db: DbSession, response: Response) -> dict[str, str]:
    """Readiness de base de datos: ejecuta exclusivamente ``SELECT 1``."""
    try:
        db.execute(text("SELECT 1")).scalar_one()
    except (SQLAlchemyError, OSError):
        # Respuesta controlada: sin hostname, sin credenciales, sin stack traces.
        response.status_code = 503
        return {"status": "error", "database": "unavailable"}
    return {"status": "ok", "database": "connected"}
