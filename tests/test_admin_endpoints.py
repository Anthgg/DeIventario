"""Tests de los endpoints de administracion de usuarios (RBAC)."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select

from app.api import admin as admin_api
from app.db.session import SessionLocal, engine
from app.main import app
from app.models import Role, User, UserRole
from app.services.auth import rbac_service
from tests.auth_helpers import cleanup_auth_test_data, create_test_user, override_auth

client = TestClient(app)

ADMIN_PERMS = {"users.read", "users.manage", "users.manage_roles"}
MISSING_ID = "00000000-0000-0000-0000-000000000000"


def _as_admin() -> uuid.UUID:
    """Crea un usuario actor real y lo usa como identidad autenticada."""
    actor_id = create_test_user(email=f"test-auth-actor-{uuid.uuid4()}@example.invalid")
    override_auth(ADMIN_PERMS, user_id=actor_id)
    return actor_id


def test_list_requires_permission() -> None:
    override_auth({"auth.self.read"})
    assert client.get("/api/v1/admin/users").status_code == 403


def test_list_and_get_user() -> None:
    cleanup_auth_test_data()
    target = create_test_user(email="test-auth-target@example.invalid", display_name="Objetivo Uno")
    _as_admin()
    listed = client.get("/api/v1/admin/users")
    assert listed.status_code == 200
    assert any(item["id"] == str(target) for item in listed.json())

    first_page = client.get("/api/v1/admin/users?limit=1&offset=0")
    second_page = client.get("/api/v1/admin/users?limit=1&offset=1")
    assert first_page.status_code == second_page.status_code == 200
    assert len(first_page.json()) == len(second_page.json()) == 1
    assert first_page.json()[0]["id"] != second_page.json()[0]["id"]

    fetched = client.get(f"/api/v1/admin/users/{target}")
    assert fetched.status_code == 200
    assert fetched.json()["display_name"] == "Objetivo Uno"
    cleanup_auth_test_data()


def test_list_users_batches_role_lookup() -> None:
    cleanup_auth_test_data()
    target = create_test_user(email="test-auth-batched-role@example.invalid")
    _as_admin()
    assigned = client.post(f"/api/v1/admin/users/{target}/roles/OPERATOR")
    assert assigned.status_code == 200, assigned.text

    statements: list[str] = []

    def collect_role_queries(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if "user_roles" in statement.lower():
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", collect_role_queries)
    try:
        with SessionLocal() as db:
            users = rbac_service.list_users(db, limit=100, offset=0)
        assert len(statements) == 1
        target_entry = next((item for item in users if item[0].id == target), None)
        assert target_entry is not None
        assert "OPERATOR" in target_entry[1]
    finally:
        event.remove(engine, "before_cursor_execute", collect_role_queries)
        cleanup_auth_test_data()


def test_get_unknown_user_is_404() -> None:
    _as_admin()
    assert client.get(f"/api/v1/admin/users/{MISSING_ID}").status_code == 404


def test_assign_and_revoke_role() -> None:
    cleanup_auth_test_data()
    target = create_test_user(email="test-auth-role@example.invalid")
    _as_admin()
    assigned = client.post(f"/api/v1/admin/users/{target}/roles/OPERATOR")
    assert assigned.status_code == 200
    assert "OPERATOR" in assigned.json()["roles"]

    revoked = client.delete(f"/api/v1/admin/users/{target}/roles/OPERATOR")
    assert revoked.status_code == 200
    assert "OPERATOR" not in revoked.json()["roles"]
    cleanup_auth_test_data()


def test_assign_unknown_role_is_404() -> None:
    cleanup_auth_test_data()
    target = create_test_user(email="test-auth-role404@example.invalid")
    _as_admin()
    assert client.post(f"/api/v1/admin/users/{target}/roles/NO_EXISTE").status_code == 404
    cleanup_auth_test_data()


def test_assign_role_to_unknown_user_is_404() -> None:
    _as_admin()
    assert client.post(f"/api/v1/admin/users/{MISSING_ID}/roles/OPERATOR").status_code == 404


def test_manage_roles_requires_permission() -> None:
    override_auth({"users.read"})
    response = client.post(f"/api/v1/admin/users/{MISSING_ID}/roles/OPERATOR")
    assert response.status_code == 403


def test_activate_and_deactivate_user() -> None:
    cleanup_auth_test_data()
    target = create_test_user(email="test-auth-active@example.invalid")
    _as_admin()
    deactivated = client.patch(
        f"/api/v1/admin/users/{target}/active", json={"is_active": False}
    )
    assert deactivated.status_code == 200
    assert deactivated.json()["is_active"] is False

    reactivated = client.patch(
        f"/api/v1/admin/users/{target}/active", json={"is_active": True}
    )
    assert reactivated.status_code == 200
    assert reactivated.json()["is_active"] is True
    cleanup_auth_test_data()


def test_last_admin_is_protected() -> None:
    cleanup_auth_test_data()
    first = create_test_user(email="test-auth-last1@example.invalid")
    _as_admin()
    assert client.post(f"/api/v1/admin/users/{first}/roles/ADMIN").status_code == 200

    # Unico ADMIN activo: no se puede desactivar ni quitarle el rol.
    assert client.patch(
        f"/api/v1/admin/users/{first}/active", json={"is_active": False}
    ).status_code == 409
    assert client.delete(f"/api/v1/admin/users/{first}/roles/ADMIN").status_code == 409

    # Con un segundo ADMIN activo, la operacion se permite.
    second = create_test_user(email="test-auth-last2@example.invalid")
    assert client.post(f"/api/v1/admin/users/{second}/roles/ADMIN").status_code == 200
    assert client.patch(
        f"/api/v1/admin/users/{first}/active", json={"is_active": False}
    ).status_code == 200
    cleanup_auth_test_data()


def test_concurrent_last_admin_removals_leave_one_admin_active() -> None:
    cleanup_auth_test_data()
    first = create_test_user(email=f"test-auth-race1-{uuid.uuid4()}@example.invalid")
    second = create_test_user(email=f"test-auth-race2-{uuid.uuid4()}@example.invalid")
    actor_id = _as_admin()
    assert client.post(f"/api/v1/admin/users/{first}/roles/ADMIN").status_code == 200
    assert client.post(f"/api/v1/admin/users/{second}/roles/ADMIN").status_code == 200
    barrier = Barrier(2)

    def run(operation: str, target_id: uuid.UUID) -> int:
        with SessionLocal() as db:
            barrier.wait(timeout=10)
            try:
                if operation == "deactivate":
                    admin_api.set_active(
                        target_id,
                        admin_api.ActiveUpdate(is_active=False),
                        SimpleNamespace(id=actor_id),
                        db,
                    )
                else:
                    admin_api.revoke_role(
                        target_id,
                        "ADMIN",
                        SimpleNamespace(id=actor_id),
                        db,
                    )
                return 200
            except HTTPException as exc:
                db.rollback()
                return exc.status_code

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [
                pool.submit(run, "deactivate", first),
                pool.submit(run, "revoke", second),
            ]
            statuses = sorted(future.result() for future in results)
        assert statuses == [200, 409]
        with SessionLocal() as db:
            active_admins = db.execute(
                select(func.count())
                .select_from(UserRole)
                .join(User, User.id == UserRole.user_id)
                .join(Role, Role.id == UserRole.role_id)
                .where(Role.code == "ADMIN", User.is_active.is_(True))
            ).scalar_one()
        assert active_admins == 1
    finally:
        cleanup_auth_test_data()
