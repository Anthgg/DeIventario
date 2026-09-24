"""Errores de dominio de la valorizacion (F009)."""

from __future__ import annotations


class ValuationError(Exception):
    """Error de valorizacion con codigo estable y payload opcional."""

    def __init__(
        self,
        message: str,
        status_code: int = 400,
        code: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.payload = payload or {}
