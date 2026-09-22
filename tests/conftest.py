"""Configuracion comun de los tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.main import app


@pytest.fixture(autouse=True)
def _clear_dependency_overrides() -> Iterator[None]:
    """Evita que los overrides de dependencias se filtren entre tests."""
    yield
    app.dependency_overrides.clear()
