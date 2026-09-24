"""Snapshot de branding (F010A): identidad configurable, nunca hardcodeada.

El hash de branding se calcula sobre los campos de identidad; el logo entra
via ``logo_sha256`` (contenido), no via ruta. Cualquier cambio de identidad
produce un ``branding_sha256`` distinto y, por tanto, un documento nuevo.
"""

from __future__ import annotations

import dataclasses

from app.documents.engine.hashing import hash_payload
from app.models import OrganizationSettings
from app.services.system import organization_service


@dataclasses.dataclass(frozen=True)
class BrandingSnapshot:
    """Copia inmutable de la identidad usada por renderers."""

    legal_name: str | None
    tax_id: str | None
    tax_id_label: str | None
    address: str | None
    phone: str | None
    email: str | None
    website: str | None
    brand_color_primary: str | None
    brand_color_secondary: str | None
    brand_color_accent: str | None
    currency_style: str
    date_format: str
    footer_text: str | None
    footer_left: str | None
    footer_right: str | None
    logo_media_type: str | None
    logo_sha256: str | None
    settings_version: int
    logo_bytes: bytes | None = dataclasses.field(default=None, compare=False, repr=False)

    def identity(self) -> dict[str, object]:
        return {
            "legal_name": self.legal_name,
            "tax_id": self.tax_id,
            "tax_id_label": self.tax_id_label,
            "address": self.address,
            "phone": self.phone,
            "email": self.email,
            "website": self.website,
            "brand_color_primary": self.brand_color_primary,
            "brand_color_secondary": self.brand_color_secondary,
            "brand_color_accent": self.brand_color_accent,
            "currency_style": self.currency_style,
            "date_format": self.date_format,
            "footer_text": self.footer_text,
            "footer_left": self.footer_left,
            "footer_right": self.footer_right,
            "logo_sha256": self.logo_sha256,
        }


def branding_hash(snapshot: BrandingSnapshot) -> str:
    return hash_payload(snapshot.identity())


def snapshot_from_settings(row: OrganizationSettings) -> BrandingSnapshot:
    logo_bytes: bytes | None = None
    if row.logo_path is not None:
        try:
            logo_bytes = organization_service.resolve_logo_file(row.logo_path).read_bytes()
        except organization_service.OrganizationError:
            logo_bytes = None
    return BrandingSnapshot(
        legal_name=row.legal_name,
        tax_id=row.tax_id,
        tax_id_label=row.tax_id_label,
        address=row.address,
        phone=row.phone,
        email=row.email,
        website=row.website,
        brand_color_primary=row.brand_color_primary,
        brand_color_secondary=row.brand_color_secondary,
        brand_color_accent=row.brand_color_accent,
        currency_style=row.currency_style.value,
        date_format=row.date_format,
        footer_text=row.footer_text,
        footer_left=row.footer_left,
        footer_right=row.footer_right,
        logo_media_type=row.logo_media_type,
        logo_sha256=row.logo_sha256,
        settings_version=row.version,
        logo_bytes=logo_bytes,
    )
