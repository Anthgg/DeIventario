"""CLI: bootstrap del primer ADMIN (idempotente y con ``--confirm``).

Uso:
    python -m app.cli.bootstrap_admin --email usuario@dominio.com [--confirm]

Sin ``--confirm`` funciona en modo DRY RUN (no muta nada).
Busca el usuario en Supabase Auth (API administrativa); NO crea credenciales.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from typing import Any

from sqlalchemy import select

from app.auth.supabase_client import SupabaseAuthClient, SupabaseAuthError
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models import User
from app.services.auth import audit_service, rbac_service

_ADMIN = rbac_service.ADMIN_ROLE_CODE


def find_supabase_user(email: str) -> dict[str, Any] | None:
    """Busca (read-only) un usuario por email en Supabase Auth."""
    settings = get_settings()
    service_key = settings.supabase_secret_key
    if not service_key:
        print("Falta SUPABASE_SERVICE_ROLE_KEY / SUPABASE_SECRET_KEY.")
        return None
    client = SupabaseAuthClient(settings)
    target = email.strip().lower()
    page = 1
    while True:
        try:
            data = client.admin_list_users(service_key, page=page, per_page=200)
        except SupabaseAuthError:
            print("Supabase Auth no respondio correctamente.")
            return None
        users = data.get("users")
        if not isinstance(users, list):
            return None
        for user in users:
            if isinstance(user, dict) and str(user.get("email") or "").lower() == target:
                return user
        if len(users) < 200:
            return None
        page += 1


def _display_name(supabase_user: dict[str, Any], email: str) -> str:
    metadata = supabase_user.get("user_metadata")
    if isinstance(metadata, dict):
        for key in ("full_name", "name"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return email


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli.bootstrap_admin",
        description="Asigna ADMIN a un usuario existente de Supabase Auth (idempotente).",
    )
    parser.add_argument("--email", required=True, help="Email del usuario en Supabase Auth.")
    parser.add_argument(
        "--confirm", action="store_true", help="Aplica los cambios (sin esto es DRY RUN)."
    )
    args = parser.parse_args(argv)

    email = args.email.strip()
    supabase_user = find_supabase_user(email)
    if supabase_user is None:
        print("No se encontro un usuario en Supabase Auth con ese email. No se crea nada.")
        return 1

    raw_id = supabase_user.get("id")
    try:
        auth_user_id = uuid.UUID(str(raw_id))
    except (ValueError, TypeError):
        print("El usuario de Supabase no tiene un id valido.")
        return 1

    with SessionLocal() as db:
        user = db.execute(
            select(User).where(User.auth_user_id == auth_user_id)
        ).scalar_one_or_none()

        if not args.confirm:
            action = "actualizaria" if user is not None else "crearia"
            print(
                f"DRY RUN: se {action} el usuario local y se asignaria ADMIN "
                f"a {email} (auth_user_id={auth_user_id})."
            )
            return 0

        if user is None:
            user = User(
                auth_user_id=auth_user_id,
                email=email,
                display_name=_display_name(supabase_user, email),
                is_active=True,
            )
            db.add(user)
            db.flush()
            audit_service.record(
                db,
                action=audit_service.USER_AUTO_PROVISIONED,
                entity_type="user",
                entity_id=user.id,
                metadata={"source": "bootstrap_admin"},
            )
        else:
            user.email = email
            user.is_active = True

        rbac_service.assign_role(db, user.id, _ADMIN)
        audit_service.record(
            db,
            action=audit_service.ADMIN_BOOTSTRAPPED,
            entity_type="user",
            entity_id=user.id,
            metadata={"email": email},
        )
        db.commit()
        print(f"ADMIN asignado a {email} (usuario local {user.id}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
