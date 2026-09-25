"""Configuracion comun de los tests."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import event
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

from app.core.config import get_settings

_LOCAL_TEST_HOSTS = frozenset(
    {"localhost", "127.0.0.1", "::1", "host.docker.internal", "postgres", "db"}
)


def _validate_test_database_url(value: str) -> str:
    """Require an explicit local PostgreSQL database with a test-like name."""
    try:
        url: URL = make_url(value)
    except (ArgumentError, TypeError, ValueError) as exc:
        raise ValueError("TEST_DATABASE_URL no es una URL valida") from exc

    database = (url.database or "").lower()
    has_test_marker = any(part == "test" for part in database.replace("-", "_").split("_"))
    if (
        url.get_backend_name() != "postgresql"
        or (url.host or "").lower() not in _LOCAL_TEST_HOSTS
        or not has_test_marker
    ):
        raise ValueError(
            "TEST_DATABASE_URL debe apuntar a PostgreSQL local y una base cuyo nombre incluya test"
        )
    return value


_TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
if _TEST_DATABASE_URL:
    try:
        _TEST_DATABASE_URL = _validate_test_database_url(_TEST_DATABASE_URL)
    except ValueError as exc:
        raise pytest.UsageError(str(exc)) from exc
    # Settings se crea al importar app.main; sustituir antes impide que .env
    # conecte los tests a la base normal del proyecto.
    os.environ["DATABASE_URL"] = _TEST_DATABASE_URL
else:
    # CI can provide DATABASE_URL directly; accept it only when it is clearly
    # a local PostgreSQL test database. Hosted or ordinary project DBs remain
    # blocked, while pure tests can still run without a test database.
    try:
        _TEST_DATABASE_URL = _validate_test_database_url(
            os.environ.get("DATABASE_URL") or get_settings().DATABASE_URL
        )
    except ValueError:
        _TEST_DATABASE_URL = None

from app.db.session import engine  # noqa: E402
from app.main import app  # noqa: E402


def _deny_unisolated_database_connection(
    _dialect: object, _connection_record: object, _cargs: object, _cparams: object
) -> None:
    raise RuntimeError(
        "Acceso a DB de pytest bloqueado; define TEST_DATABASE_URL con PostgreSQL local de pruebas"
    )


if not _TEST_DATABASE_URL:
    # Defensa adicional para una prueba nueva o una selección que el hook de
    # colección no reconozca. do_connect ocurre antes de abrir el socket.
    event.listen(engine, "do_connect", _deny_unisolated_database_connection)


_NO_DATABASE_TESTS = frozenset({"test_backend_audit_regressions.py"})


def pytest_collection_finish(session: pytest.Session) -> None:
    """Stop the default integration suite before any test can touch shared DB."""
    if _TEST_DATABASE_URL:
        return
    unsafe = [item.nodeid for item in session.items if item.path.name not in _NO_DATABASE_TESTS]
    if unsafe:
        raise pytest.UsageError(
            "La suite contiene tests con base de datos. Configura TEST_DATABASE_URL local "
            "(PostgreSQL y nombre con 'test'); no se ejecutaron tests."
        )


@pytest.fixture(autouse=True)
def _clear_dependency_overrides() -> Iterator[None]:
    """Evita que los overrides de dependencias se filtren entre tests."""
    yield
    app.dependency_overrides.clear()
