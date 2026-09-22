"""Capa de autenticacion y autorizacion (Supabase Auth + RBAC)."""

from app.auth.claims import Claims
from app.auth.dependencies import (
    CurrentUser,
    get_current_user,
    require_any_permission,
    require_permission,
    require_role,
)
from app.auth.jwt import InvalidTokenError, JwtVerifier

__all__ = [
    "Claims",
    "CurrentUser",
    "InvalidTokenError",
    "JwtVerifier",
    "get_current_user",
    "require_any_permission",
    "require_permission",
    "require_role",
]
