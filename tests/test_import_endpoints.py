"""Pruebas de los endpoints de importacion (TestClient)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from tests.import_helpers import cleanup_test_data, make_xlsx
from tests.test_importers import CONTACT_HEADERS

client = TestClient(app)

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _post(path: str, data: bytes, filename: str = "test.xlsx"):
    return client.post(path, files={"file": (filename, data, _XLSX_MIME)})


def test_preview_contacts() -> None:
    data = make_xlsx(
        CONTACT_HEADERS,
        [
            ["", "", "", "", "", "Proveedor A", "", "Perú", "", "", "", "TEST-E-A"],
            ["", "", "", "", "", "Proveedor B", "", "Perú", "", "", "", "TEST-E-B"],
        ],
    )
    response = _post("/api/v1/imports/preview", data)
    assert response.status_code == 200
    body = response.json()
    assert body["import_type"] == "CONTACTS"
    assert body["contacts_detected"] == 2
    assert body["batch_id"] is None


def test_preview_rejects_non_xlsx() -> None:
    response = client.post(
        "/api/v1/imports/preview", files={"file": ("test.txt", b"hola", "text/plain")}
    )
    assert response.status_code == 415


def test_preview_rejects_corrupt() -> None:
    response = _post("/api/v1/imports/preview", b"esto no es un zip")
    assert response.status_code == 400


def test_dry_run_then_commit_and_batch() -> None:
    cleanup_test_data()
    data = make_xlsx(
        CONTACT_HEADERS,
        [["", "", "", "", "", "Proveedor End", "", "Perú", "", "", "", "TEST-END"]],
    )
    dry = _post("/api/v1/imports?dry_run=true", data)
    assert dry.status_code == 200
    assert dry.json()["created"] == 0

    commit = _post("/api/v1/imports", data)
    assert commit.status_code == 200
    body = commit.json()
    assert body["created"] == 1
    assert body["batch_id"]

    batch = client.get(f"/api/v1/imports/{body['batch_id']}")
    assert batch.status_code == 200
    assert batch.json()["status"] == "COMPLETED"

    errors = client.get(f"/api/v1/imports/{body['batch_id']}/errors")
    assert errors.status_code == 200
    assert errors.json() == []

    again = _post("/api/v1/imports", data)
    assert again.status_code == 200
    assert again.json()["already_imported"] is True

    cleanup_test_data()
