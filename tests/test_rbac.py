"""Tests de la matriz RBAC real (DB) y del gateo por permisos en endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.session import SessionLocal
from app.main import app
from app.models import Permission, Role, RolePermission

client = TestClient(app)

OPERATOR = {
    "auth.self.read",
    "inventory.read",
    "inventory.count",
    "damage.report",
}
SUPERVISOR = OPERATOR | {"inventory.monitor", "inventory.recount", "damage.review"}
MANAGER = SUPERVISOR | {
    "imports.read",
    "imports.preview",
    "imports.execute",
    "inventory.create",
    "inventory.assign",
    "inventory.reconcile",
    "inventory.approve",
    "inventory.close",
    "inventory.expected.read",
    "exports.create",
    "exports.read",
    "audit.read",
}
ALL_PERMISSIONS = {
    "auth.self.read",
    "imports.read",
    "imports.preview",
    "imports.execute",
    "users.read",
    "users.manage",
    "users.manage_roles",
    "inventory.read",
    "inventory.create",
    "inventory.assign",
    "inventory.count",
    "inventory.monitor",
    "inventory.recount",
    "inventory.reconcile",
    "inventory.approve",
    "inventory.close",
    "inventory.reopen",
    "inventory.expected.read",
    "damage.report",
    "damage.review",
    "exports.create",
    "exports.read",
    "audit.read",
    "system.manage",
}
ADMIN = ALL_PERMISSIONS

EXPECTED_MATRIX = {
    "OPERATOR": OPERATOR,
    "SUPERVISOR": SUPERVISOR,
    "MANAGER": MANAGER,
    "ADMIN": ADMIN,
}


def _role_permissions(role_code: str) -> set[str]:
    with SessionLocal() as db:
        role = db.execute(select(Role).where(Role.code == role_code)).scalar_one()
        return set(
            db.execute(
                select(Permission.code)
                .join(RolePermission, RolePermission.permission_id == Permission.id)
                .where(RolePermission.role_id == role.id)
            ).scalars()
        )


def test_roles_exist_and_are_canonical() -> None:
    with SessionLocal() as db:
        codes = {role.code for role in db.execute(select(Role)).scalars()}
    assert {"OPERATOR", "SUPERVISOR", "MANAGER", "ADMIN"} <= codes


def test_permission_count() -> None:
    with SessionLocal() as db:
        count = len(list(db.execute(select(Permission)).scalars()))
    assert count == len(ALL_PERMISSIONS)


def test_matrix_matches_specification() -> None:
    for role_code, expected in EXPECTED_MATRIX.items():
        assert _role_permissions(role_code) == expected, role_code


def test_inheritance_is_monotonic() -> None:
    assert OPERATOR < SUPERVISOR < MANAGER < ADMIN


def _post_import() -> int:
    return client.post(
        "/api/v1/imports",
        files={"file": ("test.xlsx", b"contenido-invalido", "application/octet-stream")},
    ).status_code


def _get_batch() -> int:
    return client.get("/api/v1/imports/00000000-0000-0000-0000-000000000000").status_code


def test_import_execute_denied_for_operator_and_supervisor() -> None:
    for permissions in (OPERATOR, SUPERVISOR):
        from tests.auth_helpers import override_auth

        override_auth(permissions)
        assert _post_import() == 403


def test_import_execute_allowed_for_manager_and_admin() -> None:
    from tests.auth_helpers import override_auth

    for permissions in (MANAGER, ADMIN):
        override_auth(permissions)
        # Permiso concedido: el archivo invalido produce 400 (no 403).
        assert _post_import() == 400


def test_import_read_denied_without_permission() -> None:
    from tests.auth_helpers import override_auth

    override_auth(OPERATOR)
    assert _get_batch() == 403


def test_import_read_allowed_with_permission() -> None:
    from tests.auth_helpers import override_auth

    override_auth(MANAGER)
    # Permiso concedido: lote inexistente -> 404.
    assert _get_batch() == 404
