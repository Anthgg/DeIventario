"""Normalizacion de codigos QR escaneados (sin peticiones de red)."""

from __future__ import annotations

import re

MAX_CODE_LENGTH = 100
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_VALID_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/]*$")


class QrCodeError(Exception):
    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


def normalize_scanned_code(raw: str | None) -> str:
    """Normaliza un codigo escaneado.

    - trim de espacios
    - rechaza vacio
    - rechaza caracteres de control
    - aplica limite de longitud
    - si es una URL con ultimo segmento inequivoco, extrae solo el codigo
      (nunca devuelve el enlace como identificador y nunca hace HTTP).
    """
    if raw is None:
        raise QrCodeError("scanned_code es obligatorio")
    value = raw.strip()
    if not value:
        raise QrCodeError("scanned_code vacio")
    if _CONTROL_CHARS.search(value):
        raise QrCodeError("scanned_code contiene caracteres de control")
    if len(value) > MAX_CODE_LENGTH:
        raise QrCodeError("scanned_code supera la longitud maxima")

    if "://" in value:
        candidate = value.rstrip("/").rsplit("/", 1)[-1]
        if not candidate or not _VALID_CODE.match(candidate):
            raise QrCodeError("QR con URL no soportada")
        value = candidate

    if not _VALID_CODE.match(value):
        raise QrCodeError("scanned_code con formato invalido")
    return value
