"""Pruebas de conexion a base de datos (solo lectura, sin escrituras)."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db.session import engine
from app.main import app

client = TestClient(app)


def test_select_one_via_sqlalchemy() -> None:
    with engine.connect() as connection:
        assert connection.execute(text("SELECT 1")).scalar_one() == 1


def test_database_health_endpoint_reports_connected() -> None:
    response = client.get("/api/v1/health/database")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "connected"}
