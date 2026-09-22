"""Punto de entrada de la API Inventario Dedalo."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.health import router as health_router
from app.api.imports import router as imports_router
from app.core.config import get_settings
from app.core.logging import configure_logging


def create_app() -> FastAPI:
    """Construye la aplicacion FastAPI a partir de la configuracion."""
    settings = get_settings()
    configure_logging()

    application = FastAPI(
        title=settings.APP_NAME,
        version="0.1.0",
        docs_url="/docs" if settings.API_DOCS_ENABLED else None,
        redoc_url="/redoc" if settings.API_DOCS_ENABLED else None,
        openapi_url="/openapi.json" if settings.API_DOCS_ENABLED else None,
    )

    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    @application.get("/", tags=["root"])
    def read_root() -> dict[str, str]:
        """Raiz publica: no expone versiones, hosts ni variables internas."""
        return {"name": "Inventario Dedalo API", "status": "running"}

    application.include_router(health_router, prefix=settings.API_PREFIX)
    application.include_router(imports_router, prefix=settings.API_PREFIX)
    return application


app = create_app()
