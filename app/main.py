"""Punto de entrada de la API Inventario Dedalo."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.admin import router as admin_router
from app.api.auth import router as auth_router
from app.api.counting import router as counting_router
from app.api.documents import router as documents_router
from app.api.exceptions import router as exceptions_router
from app.api.health import router as health_router
from app.api.imports import router as imports_router
from app.api.inventory import router as inventory_router
from app.api.inventory_documents import router as inventory_documents_router
from app.api.reconciliation import router as reconciliation_router
from app.api.recounts import router as recounts_router
from app.api.system import router as system_router
from app.api.valuation import router as valuation_router
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
    application.include_router(auth_router, prefix=settings.API_PREFIX)
    application.include_router(admin_router, prefix=settings.API_PREFIX)
    application.include_router(inventory_router, prefix=settings.API_PREFIX)
    application.include_router(counting_router, prefix=settings.API_PREFIX)
    application.include_router(exceptions_router, prefix=settings.API_PREFIX)
    application.include_router(recounts_router, prefix=settings.API_PREFIX)
    application.include_router(reconciliation_router, prefix=settings.API_PREFIX)
    application.include_router(valuation_router, prefix=settings.API_PREFIX)
    application.include_router(system_router, prefix=settings.API_PREFIX)
    application.include_router(documents_router, prefix=settings.API_PREFIX)
    application.include_router(inventory_documents_router, prefix=settings.API_PREFIX)
    return application


app = create_app()
