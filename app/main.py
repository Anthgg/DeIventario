"""Punto de entrada de la API Inventario Dedalo."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

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

_SENSITIVE_INPUT_KEYS = frozenset(
    {"password", "refresh_token", "access_token", "token", "secret"}
)
_REDACTED_INPUT = "[REDACTED]"


def _collect_sensitive_values(value: object, *, sensitive: bool = False) -> set[str]:
    """Collect submitted strings nested under credential-like JSON keys."""
    if isinstance(value, dict):
        values: set[str] = set()
        for key, item in value.items():
            is_sensitive = sensitive or str(key).casefold() in _SENSITIVE_INPUT_KEYS
            values.update(_collect_sensitive_values(item, sensitive=is_sensitive))
        return values
    if isinstance(value, list):
        values = set()
        for item in value:
            values.update(_collect_sensitive_values(item, sensitive=sensitive))
        return values
    if sensitive and isinstance(value, str) and value:
        return {value}
    return set()


def _redact_validation_value(value: Any, sensitive_values: set[str]) -> Any:
    if isinstance(value, dict):
        return {
            key: _REDACTED_INPUT
            if str(key).casefold() in _SENSITIVE_INPUT_KEYS
            else _redact_validation_value(item, sensitive_values)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_validation_value(item, sensitive_values) for item in value]
    if isinstance(value, str):
        for secret in sensitive_values:
            value = value.replace(secret, _REDACTED_INPUT)
    return value


async def _request_validation_error_handler(
    _request: Request, exc: Exception
) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):
        raise exc
    errors = jsonable_encoder(exc.errors())
    sensitive_values = _collect_sensitive_values(exc.body)
    safe_errors: list[dict[str, Any]] = []
    for error in errors:
        location = error.get("loc", ())
        has_sensitive_location = isinstance(location, (list, tuple)) and any(
            isinstance(part, str) and part.casefold() in _SENSITIVE_INPUT_KEYS
            for part in location
        )
        if has_sensitive_location and "input" in error:
            error["input"] = _REDACTED_INPUT
        safe_errors.append(_redact_validation_value(error, sensitive_values))
    return JSONResponse(status_code=422, content={"detail": safe_errors})


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
    application.add_exception_handler(
        RequestValidationError, _request_validation_error_handler
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
