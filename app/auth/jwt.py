"""Verificador de JWT contra el JWKS de Supabase (ES256)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import jwt

from app.auth.claims import Claims
from app.core.config import Settings

_ALLOWED_ALGORITHMS: tuple[str, ...] = ("ES256",)


class InvalidTokenError(Exception):
    """El token no es valido (malformado/expirado/firma/issuer/audiencia/alg/sub)."""


class JwtVerifier:
    """Valida firma, exp, nbf, issuer, audiencia, alg y ``sub`` (UUID).

    ``key_provider`` permite inyectar la resolucion de claves en tests
    (sin depender de la red). En produccion usa ``PyJWKClient`` con cache.
    """

    def __init__(
        self, settings: Settings, key_provider: Callable[[str], Any] | None = None
    ) -> None:
        self._audience = settings.SUPABASE_JWT_AUDIENCE
        self._issuer = settings.supabase_jwt_issuer
        self._algorithms = list(_ALLOWED_ALGORITHMS)
        self._provider = key_provider
        self._client: jwt.PyJWKClient | None = None
        if key_provider is None:
            self._client = jwt.PyJWKClient(
                settings.supabase_jwks_url,
                cache_keys=True,
                lifespan=settings.AUTH_JWKS_CACHE_SECONDS,
            )

    def verify(self, token: str) -> Claims:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise InvalidTokenError("token malformado") from exc
        if header.get("alg") not in self._algorithms:
            raise InvalidTokenError("algoritmo no permitido")
        try:
            key = self._signing_key(token)
            payload = jwt.decode(
                token,
                key,
                algorithms=self._algorithms,
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "sub", "iat"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise InvalidTokenError("token expirado") from exc
        except jwt.PyJWTError as exc:
            raise InvalidTokenError("token invalido") from exc
        sub = payload.get("sub")
        try:
            sub_uuid = uuid.UUID(str(sub))
        except (ValueError, TypeError) as exc:
            raise InvalidTokenError("sub invalido") from exc
        return Claims(
            sub=sub_uuid,
            email=payload.get("email"),
            role_claim=payload.get("role"),
            metadata=dict(payload.get("user_metadata") or {}),
        )

    def _signing_key(self, token: str) -> Any:
        if self._provider is not None:
            return self._provider(token)
        assert self._client is not None
        return self._client.get_signing_key_from_jwt(token).key
