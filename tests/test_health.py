"""Pruebas de los endpoints raiz y de salud."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_root_returns_running() -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {"name": "Inventario Dedalo API", "status": "running"}


def test_health_returns_ok() -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "inventario-dedalo-api"
    assert "environment" in body
