"""Configuracion centralizada del backend.

Esta es la UNICA capa del proyecto autorizada a leer variables de entorno.
El resto del codigo debe consumir ``get_settings()`` en lugar de ``os.getenv()``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR: Path = Path(__file__).resolve().parents[2]

_PSYCOPG_SCHEME = "postgresql+psycopg://"
_SSL_MODE = "sslmode=require"


def _normalize_database_url(raw: str) -> str:
    """Normaliza la URL de base de datos para SQLAlchemy + psycopg 3.

    - Fuerza el driver ``psycopg`` (SQLAlchemy 2 mapea ``postgresql://`` a
      psycopg2, que no esta instalado).
    - Garantiza ``sslmode=require`` sin concatenaciones fragiles.
    """
    url = raw.strip()
    if not url:
        return url

    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            url = _PSYCOPG_SCHEME + url[len(prefix) :]
            break

    if "sslmode=" not in url:
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}{_SSL_MODE}"

    return url


class Settings(BaseSettings):
    """Configuracion tipada cargada desde el entorno y ``.env``."""

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # -- APP --
    APP_NAME: str = "Inventario Dedalo API"
    APP_ENV: str = "local"
    APP_DEBUG: bool = False

    # -- API --
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    API_PREFIX: str = "/api/v1"
    API_DOCS_ENABLED: bool = True

    # -- DATABASE --
    DATABASE_URL: SecretStr = SecretStr("")
    DATABASE_ECHO: bool = False
    DATABASE_POOL_SIZE: int = 5
    DATABASE_MAX_OVERFLOW: int = 10

    # -- SUPABASE --
    SUPABASE_URL: str = ""
    SUPABASE_ANON_KEY: SecretStr = SecretStr("")
    SUPABASE_SERVICE_ROLE_KEY: SecretStr = SecretStr("")
    SUPABASE_JWT_SECRET: SecretStr = SecretStr("")
    SUPABASE_PUBLISHABLE_KEY: SecretStr = SecretStr("")
    SUPABASE_SECRET_KEY: SecretStr = SecretStr("")

    # -- SEGURIDAD (aplicacion) --
    SECRET_KEY: SecretStr = SecretStr("")

    # -- AUTH / JWT --
    SUPABASE_JWT_AUDIENCE: str = "authenticated"
    SUPABASE_JWT_ISSUER: str = ""  # vacio = derivado de SUPABASE_URL
    AUTH_JWKS_CACHE_SECONDS: int = 300
    AUTH_HTTP_TIMEOUT_SECONDS: float = 10.0

    # -- CORS --
    FRONTEND_URL: str = "http://localhost:5173"
    CORS_ORIGINS: str = ""

    # -- STORAGE --
    UPLOAD_DIR: str = Field(
        default="./storage/uploads",
        validation_alias=AliasChoices("UPLOAD_DIR", "STORAGE_UPLOADS_DIR"),
    )
    EXPORT_DIR: str = Field(
        default="./storage/exports",
        validation_alias=AliasChoices("EXPORT_DIR", "STORAGE_EXPORTS_DIR"),
    )
    EVIDENCE_DIR: str = Field(
        default="./storage/evidence",
        validation_alias=AliasChoices("EVIDENCE_DIR", "STORAGE_EVIDENCE_DIR"),
    )
    MAX_UPLOAD_MB: int = Field(
        default=25,
        validation_alias=AliasChoices("MAX_UPLOAD_MB", "STORAGE_MAX_UPLOAD_MB"),
    )
    MAX_EVIDENCE_MB: int = Field(
        default=8,
        validation_alias=AliasChoices("MAX_EVIDENCE_MB", "EVIDENCE_MAX_MB"),
    )

    # -- LOCALIZACION --
    DEFAULT_CURRENCY: str = Field(
        default="PEN",
        validation_alias=AliasChoices("DEFAULT_CURRENCY", "APP_CURRENCY"),
    )
    DEFAULT_TIMEZONE: str = Field(
        default="America/Lima",
        validation_alias=AliasChoices("DEFAULT_TIMEZONE", "APP_TIMEZONE"),
    )

    # -- LOGGING --
    LOG_LEVEL: str = "INFO"

    @property
    def cors_origins(self) -> list[str]:
        """Origenes CORS permitidos (lista normalizada)."""
        origins = [item.strip() for item in self.CORS_ORIGINS.split(",") if item.strip()]
        if not origins and self.FRONTEND_URL:
            return [self.FRONTEND_URL]
        return origins

    @property
    def supabase_jwks_url(self) -> str:
        """URL del JWKS derivada de SUPABASE_URL (nunca hardcodeada)."""
        return f"{self.SUPABASE_URL.rstrip('/')}/auth/v1/.well-known/jwks.json"

    @property
    def supabase_jwt_issuer(self) -> str:
        """Issuer esperado: configurado o derivado de SUPABASE_URL."""
        return self.SUPABASE_JWT_ISSUER or f"{self.SUPABASE_URL.rstrip('/')}/auth/v1"

    @property
    def supabase_publishable_key(self) -> str:
        """Clave publica efectiva: ``SUPABASE_PUBLISHABLE_KEY`` o fallback a anon."""
        publishable = self.SUPABASE_PUBLISHABLE_KEY.get_secret_value()
        return publishable or self.SUPABASE_ANON_KEY.get_secret_value()

    @property
    def supabase_secret_key(self) -> str:
        """Clave secreta efectiva: ``SUPABASE_SECRET_KEY`` o fallback a service role."""
        secret = self.SUPABASE_SECRET_KEY.get_secret_value()
        return secret or self.SUPABASE_SERVICE_ROLE_KEY.get_secret_value()

    @property
    def sqlalchemy_url(self) -> str:
        """URL de SQLAlchemy lista para psycopg 3 y con TLS obligatorio."""
        return _normalize_database_url(self.DATABASE_URL.get_secret_value())

    @property
    def secret_values(self) -> tuple[str, ...]:
        """Valores sensibles que el logging debe redactar siempre."""
        candidates = (
            self.DATABASE_URL.get_secret_value(),
            self.SECRET_KEY.get_secret_value(),
            self.SUPABASE_ANON_KEY.get_secret_value(),
            self.SUPABASE_SERVICE_ROLE_KEY.get_secret_value(),
            self.SUPABASE_JWT_SECRET.get_secret_value(),
            self.SUPABASE_PUBLISHABLE_KEY.get_secret_value(),
            self.SUPABASE_SECRET_KEY.get_secret_value(),
        )
        return tuple(value for value in candidates if value)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Devuelve la configuracion (instancia unica cacheada)."""
    return Settings()
