"""Errores de dominio del motor de conteo."""

from __future__ import annotations


class CountError(Exception):
    """Error de dominio del conteo (con codigo estable y payload opcional)."""

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
