"""Servicio de importacion de contactos (upsert controlado, sin borrados)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.importers.base import ParsedContact
from app.models import Contact


class ContactImportService:
    """Upsert de contactos. La ausencia en el archivo nunca implica eliminacion."""

    def upsert_batch(
        self, session: Session, contacts: list[ParsedContact]
    ) -> tuple[int, int]:
        """Upsert en lote (carga los existentes una sola vez). Devuelve (creados, actualizados)."""
        existing = session.execute(select(Contact)).scalars().all()
        by_ref: dict[str, Contact] = {}
        by_name: dict[str, Contact] = {}
        for existing_row in existing:
            if existing_row.external_ref:
                by_ref[existing_row.external_ref] = existing_row
            by_name[existing_row.name] = existing_row

        created = 0
        updated = 0
        for contact in contacts:
            row = by_ref.get(contact.external_ref) if contact.external_ref else None
            if row is None:
                row = by_name.get(contact.name)
            if row is None:
                new = Contact(
                    external_ref=contact.external_ref,
                    name=contact.name,
                    raw_external_id=contact.raw_external_id,
                    active=True,
                )
                session.add(new)
                if contact.external_ref:
                    by_ref[contact.external_ref] = new
                by_name[contact.name] = new
                created += 1
            else:
                row.name = contact.name
                row.active = True
                if contact.external_ref:
                    row.external_ref = contact.external_ref
                    by_ref[contact.external_ref] = row
                if contact.raw_external_id:
                    row.raw_external_id = contact.raw_external_id
                updated += 1
        session.flush()
        return created, updated

    def upsert(self, session: Session, contact: ParsedContact) -> bool:
        """Crea o actualiza un contacto. Devuelve True si fue creado."""
        existing = self._find_existing(session, contact)
        if existing is None:
            session.add(
                Contact(
                    external_ref=contact.external_ref,
                    name=contact.name,
                    raw_external_id=contact.raw_external_id,
                    active=True,
                )
            )
            session.flush()
            return True
        existing.name = contact.name
        existing.active = True
        if contact.external_ref:
            existing.external_ref = contact.external_ref
        if contact.raw_external_id:
            existing.raw_external_id = contact.raw_external_id
        return False

    @staticmethod
    def _find_existing(session: Session, contact: ParsedContact) -> Contact | None:
        if contact.external_ref:
            row = session.execute(
                select(Contact).where(Contact.external_ref == contact.external_ref).limit(1)
            ).scalar_one_or_none()
            if row is not None:
                return row
        # Fallback por nombre exacto (el nombre no se asume unico).
        return session.execute(
            select(Contact).where(Contact.name == contact.name).limit(1)
        ).scalar_one_or_none()
