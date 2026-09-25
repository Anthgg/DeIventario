"""Almacenamiento seguro de documentos generados (F010F).

- Rutas generadas por backend (nunca filename del cliente).
- Resolucion confinada al directorio de documentos (anti path traversal).
"""

from __future__ import annotations

import uuid
from pathlib import Path

from app.core.config import get_settings

_CONTENT_TYPES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


class DocumentStorageError(Exception):
    def __init__(self, message: str, status_code: int = 400, code: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def document_root() -> Path:
    directory = Path(get_settings().DOCUMENT_DIR).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def content_type_for(filename: str) -> str:
    return _CONTENT_TYPES.get(Path(filename).suffix.lower(), "application/octet-stream")


def write_document(relative_path: str, data: bytes) -> Path:
    root = document_root()
    target = (root / relative_path).resolve()
    if target == root or not target.is_relative_to(root):
        raise DocumentStorageError("Ruta de documento invalida", 400, "DOCUMENT_PATH_INVALID")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(data)
        temporary.replace(target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return target


def resolve_document(relative_path: str) -> Path:
    root = document_root()
    candidate = (root / relative_path).resolve()
    if candidate == root or not candidate.is_relative_to(root) or not candidate.is_file():
        raise DocumentStorageError("Documento no encontrado", 404, "DOCUMENT_NOT_FOUND")
    return candidate
