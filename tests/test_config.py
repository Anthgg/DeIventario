"""Pruebas de la configuracion centralizada."""

from __future__ import annotations

from app.core.config import Settings, get_settings


def test_app_name_loads() -> None:
    assert get_settings().APP_NAME


def test_app_env_loads() -> None:
    assert get_settings().APP_ENV


def test_database_url_is_configured() -> None:
    assert get_settings().DATABASE_URL.get_secret_value()


def test_supabase_keys_are_configured() -> None:
    settings = get_settings()
    assert settings.supabase_publishable_key
    assert settings.supabase_secret_key


def test_sqlalchemy_url_forces_psycopg_and_sslmode() -> None:
    settings = Settings(DATABASE_URL="postgresql://user:pass@host:5432/db")
    url = settings.sqlalchemy_url
    assert url.startswith("postgresql+psycopg://")
    assert "sslmode=require" in url


def test_sqlalchemy_url_keeps_existing_query_params() -> None:
    settings = Settings(DATABASE_URL="postgresql://user:pass@host:5432/db?application_name=api")
    url = settings.sqlalchemy_url
    assert "application_name=api" in url
    assert "&sslmode=require" in url


def test_supabase_aliases_prefer_modern_names() -> None:
    settings = Settings(
        SUPABASE_ANON_KEY="legacy-anon-key-value",
        SUPABASE_PUBLISHABLE_KEY="modern-publishable-key",
        SUPABASE_SERVICE_ROLE_KEY="legacy-service-key-value",
        SUPABASE_SECRET_KEY="modern-secret-key",
    )
    assert settings.supabase_publishable_key == "modern-publishable-key"
    assert settings.supabase_secret_key == "modern-secret-key"


def test_supabase_aliases_fall_back_to_legacy_names() -> None:
    settings = Settings(
        SUPABASE_ANON_KEY="legacy-anon-key-value",
        SUPABASE_PUBLISHABLE_KEY="",
        SUPABASE_SERVICE_ROLE_KEY="legacy-service-key-value",
        SUPABASE_SECRET_KEY="",
    )
    assert settings.supabase_publishable_key == "legacy-anon-key-value"
    assert settings.supabase_secret_key == "legacy-service-key-value"


def test_secrets_are_not_exposed_in_repr() -> None:
    settings = get_settings()
    representation = repr(settings)
    for secret in settings.secret_values:
        assert secret not in representation
