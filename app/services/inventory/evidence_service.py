"""Almacenamiento de evidencia de dano (opcional en F006).

- Formatos: image/jpeg, image/png, image/webp validados por magia real.
- El filename del cliente NUNCA se usa como ruta (anti path traversal).
- En la DB se guarda solo un nombre relativo generado por backend.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from app.core.config import get_settings
from app.models import InventoryDamage

_MIME_EXT: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}
ALLOWED_MIMES: frozenset[str] = frozenset(_MIME_EXT)

_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class EvidenceError(Exception):
    def __init__(self, message: str, status_code: int = 400, code: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def sniff_image(data: bytes) -> str | None:
    """Detecta el tipo real por contenido (no por extension ni header del cliente)."""
    if data.startswith(_JPEG_MAGIC):
        return "image/jpeg"
    if data.startswith(_PNG_MAGIC):
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _evidence_dir() -> Path:
    directory = Path(get_settings().EVIDENCE_DIR).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def validate_upload(*, declared_mime: str | None, data: bytes) -> str:
    """Valida tamano, MIME declarado y contenido real. Devuelve el MIME canonico."""
    settings = get_settings()
    max_bytes = settings.MAX_EVIDENCE_MB * 1024 * 1024
    if len(data) == 0:
        raise EvidenceError("Archivo vacio", 422, "EVIDENCE_EMPTY_FILE")
    if len(data) > max_bytes:
        raise EvidenceError(
            f"La evidencia supera el maximo de {settings.MAX_EVIDENCE_MB} MB",
            413,
            "EVIDENCE_TOO_LARGE",
        )
    if declared_mime not in ALLOWED_MIMES:
        raise EvidenceError(
            "Formato de evidencia no permitido (jpeg, png o webp)",
            415,
            "EVIDENCE_UNSUPPORTED_MEDIA",
        )
    sniffed = sniff_image(data)
    if sniffed is None:
        raise EvidenceError("El contenido no es una imagen valida", 422, "EVIDENCE_INVALID_CONTENT")
    if sniffed != declared_mime:
        raise EvidenceError(
            "El contenido no coincide con el tipo declarado", 422, "EVIDENCE_CONTENT_MISMATCH"
        )
    return sniffed


def evidence_relative_path(damage_id: uuid.UUID, mime: str) -> str:
    """Nombre relativo unico generado por backend (nunca el filename del cliente)."""
    return f"{damage_id.hex}.{_MIME_EXT[mime]}"


def write_evidence_file(relative_path: str, data: bytes) -> Path:
    directory = _evidence_dir()
    target = (directory / relative_path).resolve()
    if target.parent != directory:
        raise EvidenceError("Ruta de evidencia invalida", 400, "EVIDENCE_PATH_INVALID")
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(data)
        temporary.replace(target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return target


def resolve_evidence_file(relative_path: str) -> Path:
    """Resuelve una ruta relativa de la DB sin salir del directorio de evidencia."""
    directory = _evidence_dir()
    candidate = (directory / relative_path).resolve()
    if candidate.parent != directory or not candidate.is_file():
        raise EvidenceError("Evidencia no encontrada", 404, "EVIDENCE_NOT_FOUND")
    return candidate


def media_type_for(relative_path: str) -> str:
    suffix = Path(relative_path).suffix.lower().lstrip(".")
    for mime, ext in _MIME_EXT.items():
        if ext == suffix:
            return mime
    return "application/octet-stream"


def has_evidence(damage: InventoryDamage) -> bool:
    return damage.evidence_path is not None
