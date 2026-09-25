"""Configuracion organizacional unica y branding de documentos (F010A).

- ``get_settings`` materializa la unica fila activa (singleton) bajo lock.
- ``update_settings`` aplica actualizacion optimista con ``expected_version``.
- ``update_logo`` valida por magia real, almacena en ``storage/branding`` con
  nombre generado por backend (anti path traversal) y versiona la fila.
- Ningun valor de marca vive hardcodeado en el codigo: todo persiste aqui.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings as get_app_settings
from app.models import OrganizationSettings
from app.models.enums import CurrencyStyle
from app.services.auth import audit_service
from app.services.inventory.evidence_service import sniff_image

_HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_DOC_PREFIX = re.compile(r"^[A-Za-z0-9_-]{1,30}$")
_ALLOWED_DATE_FORMATS = ("YYYY-MM-DD", "YYYY/MM/DD", "DD/MM/YYYY", "MM/DD/YYYY")

_MIME_EXT: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}
ALLOWED_LOGO_MIMES: frozenset[str] = frozenset(_MIME_EXT)

_TEXT_FIELDS = (
    "legal_name",
    "tax_id",
    "tax_id_label",
    "address",
    "phone",
    "email",
    "website",
    "brand_color_primary",
    "brand_color_secondary",
    "brand_color_accent",
    "footer_text",
    "footer_left",
    "footer_right",
)


class OrganizationError(Exception):
    def __init__(self, message: str, status_code: int = 400, code: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def _now_payload(row: OrganizationSettings) -> dict[str, object]:
    return {
        "id": str(row.id),
        "version": row.version,
        "legal_name": row.legal_name,
        "tax_id": row.tax_id,
        "tax_id_label": row.tax_id_label,
        "address": row.address,
        "phone": row.phone,
        "email": row.email,
        "website": row.website,
        "brand_color_primary": row.brand_color_primary,
        "brand_color_secondary": row.brand_color_secondary,
        "brand_color_accent": row.brand_color_accent,
        "currency_style": row.currency_style.value,
        "date_format": row.date_format,
        "footer_text": row.footer_text,
        "footer_left": row.footer_left,
        "footer_right": row.footer_right,
        "document_number_prefix": row.document_number_prefix,
        "next_document_number": row.next_document_number,
        "logo": (
            {
                "path": row.logo_path,
                "sha256": row.logo_sha256,
                "media_type": row.logo_media_type,
                "original_name": row.logo_original_name,
            }
            if row.logo_path is not None
            else None
        ),
        "is_configured": is_configured(row),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def is_configured(row: OrganizationSettings) -> bool:
    return bool(row.legal_name and row.tax_id and row.tax_id_label)


def require_configured(row: OrganizationSettings) -> None:
    if not is_configured(row):
        raise OrganizationError(
            "La configuracion organizacional esta incompleta: razon social, "
            "identificacion tributaria y etiqueta son obligatorias",
            409,
            "ORGANIZATION_SETTINGS_INCOMPLETE",
        )


def get_settings(db: Session, *, lock: bool = False) -> OrganizationSettings:
    """Devuelve la unica configuracion activa (la materializa si no existe)."""
    query = select(OrganizationSettings)
    if lock:
        query = query.with_for_update()
    row = db.execute(query.limit(1)).scalar_one_or_none()
    if row is not None:
        return row
    row = OrganizationSettings(id=uuid.uuid4(), version=1, created_by=None, updated_by=None)
    db.add(row)
    db.flush()
    audit_service.record(
        db,
        action=audit_service.ORGANIZATION_SETTINGS_CREATED,
        entity_type="organization_settings",
        entity_id=row.id,
        metadata={"version": row.version},
    )
    db.commit()
    return row


def _validate(patch: dict[str, object]) -> dict[str, object]:
    cleaned: dict[str, object] = {}
    for field in _TEXT_FIELDS:
        if field not in patch:
            continue
        value = patch[field]
        if value is None:
            cleaned[field] = None
            continue
        if not isinstance(value, str):
            raise OrganizationError(f"{field} debe ser texto", 422, "ORGANIZATION_INVALID_FIELD")
        text = value.strip()
        cleaned[field] = text or None

    for color_field in ("brand_color_primary", "brand_color_secondary", "brand_color_accent"):
        if cleaned.get(color_field) is not None and not _HEX_COLOR.match(str(cleaned[color_field])):
            raise OrganizationError(
                f"{color_field} debe ser un color hexadecimal (#RGB o #RRGGBB)",
                422,
                "ORGANIZATION_INVALID_COLOR",
            )

    if "currency_style" in patch and patch["currency_style"] is not None:
        raw = patch["currency_style"]
        try:
            cleaned["currency_style"] = CurrencyStyle(str(raw))
        except ValueError as exc:
            raise OrganizationError(
                "currency_style no soportado", 422, "ORGANIZATION_INVALID_FIELD"
            ) from exc

    if "date_format" in patch and patch["date_format"] is not None:
        date_format = str(patch["date_format"])
        if date_format not in _ALLOWED_DATE_FORMATS:
            raise OrganizationError(
                f"date_format debe ser uno de {', '.join(_ALLOWED_DATE_FORMATS)}",
                422,
                "ORGANIZATION_INVALID_FIELD",
            )
        cleaned["date_format"] = date_format

    if "document_number_prefix" in patch and patch["document_number_prefix"] is not None:
        prefix = str(patch["document_number_prefix"]).strip()
        if not _DOC_PREFIX.match(prefix):
            raise OrganizationError(
                "document_number_prefix solo admite letras, numeros, '-' y '_' (max 30)",
                422,
                "ORGANIZATION_INVALID_FIELD",
            )
        cleaned["document_number_prefix"] = prefix

    return cleaned


def update_settings(
    db: Session,
    *,
    actor_id: uuid.UUID | None,
    expected_version: int | None,
    patch: dict[str, object],
) -> dict[str, object]:
    """Actualizacion optimista de la configuracion (PATCH)."""
    row = get_settings(db, lock=True)
    if expected_version is not None and row.version != expected_version:
        raise OrganizationError("Conflicto de version", 409, "VERSION_CONFLICT")
    cleaned = _validate(patch)
    if not cleaned:
        raise OrganizationError("No hay cambios para aplicar", 422, "ORGANIZATION_NO_CHANGES")
    for field, value in cleaned.items():
        setattr(row, field, value)
    row.version += 1
    row.updated_by = actor_id
    db.flush()
    audit_service.record(
        db,
        action=audit_service.ORGANIZATION_SETTINGS_UPDATED,
        actor_user_id=actor_id,
        entity_type="organization_settings",
        entity_id=row.id,
        metadata={"version": row.version, "fields": sorted(cleaned)},
    )
    db.commit()
    return _now_payload(row)


def brand_dir() -> Path:
    directory = Path(get_app_settings().BRANDING_DIR).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def update_logo(
    db: Session,
    *,
    actor_id: uuid.UUID | None,
    declared_mime: str | None,
    data: bytes,
    original_name: str | None,
) -> dict[str, object]:
    """Subida de logo: validacion por contenido, nombre backend, versionado."""
    settings = get_app_settings()
    max_bytes = settings.MAX_BRANDING_LOGO_MB * 1024 * 1024
    if len(data) == 0:
        raise OrganizationError("Archivo vacio", 422, "LOGO_EMPTY_FILE")
    if len(data) > max_bytes:
        raise OrganizationError(
            f"El logo supera el maximo de {settings.MAX_BRANDING_LOGO_MB} MB",
            413,
            "LOGO_TOO_LARGE",
        )
    if declared_mime not in ALLOWED_LOGO_MIMES:
        raise OrganizationError(
            "Formato de logo no permitido (jpeg, png o webp)", 415, "LOGO_UNSUPPORTED_MEDIA"
        )
    sniffed = sniff_image(data)
    if sniffed is None:
        raise OrganizationError("El contenido no es una imagen valida", 422, "LOGO_INVALID_CONTENT")
    if sniffed != declared_mime:
        raise OrganizationError(
            "El contenido no coincide con el tipo declarado", 422, "LOGO_CONTENT_MISMATCH"
        )

    digest = hashlib.sha256(data).hexdigest()
    relative_path = f"{digest}.{_MIME_EXT[sniffed]}"
    directory = brand_dir()
    target = (directory / relative_path).resolve()
    if target.parent != directory:
        raise OrganizationError("Ruta de logo invalida", 400, "LOGO_PATH_INVALID")
    row = get_settings(db, lock=True)
    previous_logo_path = row.logo_path
    target_existed = target.exists()
    try:
        if not target_existed:
            temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            try:
                temporary.write_bytes(data)
                temporary.replace(target)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise

        row.logo_path = relative_path
        row.logo_sha256 = digest
        row.logo_media_type = sniffed
        row.logo_original_name = Path(original_name).name if original_name else None
        row.version += 1
        row.updated_by = actor_id
        db.flush()
        audit_service.record(
            db,
            action=audit_service.ORGANIZATION_LOGO_UPDATED,
            actor_user_id=actor_id,
            entity_type="organization_settings",
            entity_id=row.id,
            metadata={
                "version": row.version,
                "sha256": digest,
                "media_type": sniffed,
                "size": len(data),
            },
        )
        db.commit()
    except Exception:
        db.rollback()
        if not target_existed and previous_logo_path != relative_path:
            try:
                file_is_referenced = (
                    db.execute(
                        select(OrganizationSettings.id).where(
                            OrganizationSettings.logo_path == relative_path
                        )
                    ).scalar_one_or_none()
                    is not None
                )
            except Exception:
                file_is_referenced = True
            if not file_is_referenced:
                target.unlink(missing_ok=True)
        raise
    return _now_payload(row)


def resolve_logo_file(logo_path: str) -> Path:
    """Resuelve el logo almacenado sin salir del directorio de branding."""
    directory = brand_dir()
    candidate = (directory / logo_path).resolve()
    if candidate == directory or not candidate.is_relative_to(directory) or not candidate.is_file():
        raise OrganizationError("Logo no encontrado", 404, "LOGO_NOT_FOUND")
    return candidate


def settings_payload(row: OrganizationSettings) -> dict[str, object]:
    return _now_payload(row)
