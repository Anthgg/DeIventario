"""Sincronizacion del usuario local a partir de los claims verificados."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.claims import Claims
from app.models import User


def resolve_local_user(db: Session, claims: Claims) -> tuple[User, bool]:
    """Devuelve (usuario_local, hubo_cambios).

    Busca por ``auth_user_id``; si no existe lo crea. Nunca inventa nombres
    humanos: usa metadata de Supabase, luego email, y el UUID como ultimo recurso.
    """
    user = db.execute(select(User).where(User.auth_user_id == claims.sub)).scalar_one_or_none()
    if user is None:
        user = User(
            auth_user_id=claims.sub,
            email=claims.email,
            display_name=_display_name(claims),
            is_active=True,
        )
        db.add(user)
        db.flush()
        return user, True

    changed = False
    if claims.email and user.email != claims.email:
        user.email = claims.email
        changed = True
    return user, changed


def _display_name(claims: Claims) -> str:
    for key in ("full_name", "name", "display_name"):
        value = claims.metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if claims.email:
        return claims.email
    return str(claims.sub)
