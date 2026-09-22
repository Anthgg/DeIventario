"""006 auth RBAC: permissions + role_permissions + seed de roles/permisos/matriz.

Revision ID: 006_auth_rbac
Revises: ee5e5ebc8e5a
Create Date: 2026-09-21

Semilla determinista e idempotente de la configuracion estructural de RBAC:
roles (OPERATOR/SUPERVISOR/MANAGER/ADMIN), catalogo de permisos y la matriz
rol->permisos. No crea usuarios humanos.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "006_auth_rbac"
down_revision: str | None = "ee5e5ebc8e5a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROLES: tuple[tuple[str, str], ...] = (
    ("OPERATOR", "Operador"),
    ("SUPERVISOR", "Supervisor"),
    ("MANAGER", "Gerente"),
    ("ADMIN", "Administrador"),
)

_PERMISSIONS: tuple[tuple[str, str, str | None], ...] = (
    ("auth.self.read", "Leer perfil propio", None),
    ("imports.read", "Leer importaciones", None),
    ("imports.preview", "Previsualizar importacion", None),
    ("imports.execute", "Ejecutar importacion", None),
    ("users.read", "Leer usuarios", None),
    ("users.manage", "Gestionar usuarios", None),
    ("users.manage_roles", "Gestionar roles de usuario", None),
    ("inventory.read", "Leer inventario", None),
    ("inventory.create", "Crear inventario", None),
    ("inventory.assign", "Asignar inventario", None),
    ("inventory.count", "Contar inventario", None),
    ("inventory.monitor", "Monitorear inventario", None),
    ("inventory.recount", "Recontar inventario", None),
    ("inventory.reconcile", "Conciliar inventario", None),
    ("inventory.approve", "Aprobar inventario", None),
    ("inventory.close", "Cerrar inventario", None),
    ("inventory.reopen", "Reabrir inventario", None),
    ("damage.report", "Reportar dano", None),
    ("damage.review", "Revisar dano", None),
    ("exports.create", "Crear exportaciones", None),
    ("exports.read", "Leer exportaciones", None),
    ("audit.read", "Leer auditoria", None),
    ("system.manage", "Gestionar sistema", None),
)

_OPERATOR = ("auth.self.read", "inventory.read", "inventory.count", "damage.report")
_SUPERVISOR = _OPERATOR + ("inventory.monitor", "inventory.recount", "damage.review")
_MANAGER = _SUPERVISOR + (
    "imports.read",
    "imports.preview",
    "imports.execute",
    "inventory.create",
    "inventory.assign",
    "inventory.reconcile",
    "inventory.approve",
    "inventory.close",
    "exports.create",
    "exports.read",
    "audit.read",
)
_ADMIN = tuple(code for code, _name, _desc in _PERMISSIONS)

_ROLE_PERMISSIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("OPERATOR", _OPERATOR),
    ("SUPERVISOR", _SUPERVISOR),
    ("MANAGER", _MANAGER),
    ("ADMIN", _ADMIN),
)


def _quote(value: str) -> str:
    return value.replace("'", "''")


def upgrade() -> None:
    op.create_table(
        "permissions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_permissions")),
        sa.UniqueConstraint("code", name=op.f("uq_permissions_code")),
    )
    op.create_table(
        "role_permissions",
        sa.Column("role_id", sa.Uuid(), nullable=False),
        sa.Column("permission_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["role_id"], ["roles.id"], name=op.f("fk_role_permissions_role_id_roles"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["permission_id"],
            ["permissions.id"],
            name=op.f("fk_role_permissions_permission_id_permissions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("role_id", "permission_id", name=op.f("pk_role_permissions")),
    )

    for code, name in _ROLES:
        op.execute(
            f"INSERT INTO roles (id, code, name, created_at) "
            f"VALUES (gen_random_uuid(), '{_quote(code)}', '{_quote(name)}', now()) "
            f"ON CONFLICT (code) DO NOTHING"
        )

    for code, name, description in _PERMISSIONS:
        desc_sql = "NULL" if description is None else f"'{_quote(description)}'"
        op.execute(
            f"INSERT INTO permissions (id, code, name, description, created_at) "
            f"VALUES (gen_random_uuid(), '{_quote(code)}', '{_quote(name)}', {desc_sql}, now()) "
            f"ON CONFLICT (code) DO NOTHING"
        )

    for role_code, permission_codes in _ROLE_PERMISSIONS:
        quoted_codes = ", ".join(f"'{_quote(code)}'" for code in permission_codes)
        op.execute(
            "INSERT INTO role_permissions (role_id, permission_id, created_at) "
            "SELECT r.id, p.id, now() FROM roles r "
            f"JOIN permissions p ON p.code IN ({quoted_codes}) "
            f"WHERE r.code = '{_quote(role_code)}' "
            "ON CONFLICT (role_id, permission_id) DO NOTHING"
        )


def downgrade() -> None:
    op.drop_table("role_permissions")
    op.drop_table("permissions")
