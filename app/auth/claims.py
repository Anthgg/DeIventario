"""Claims del JWT verificado."""

from __future__ import annotations

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True)
class Claims:
    """Identidad extraida de un token valido.

    ``role_claim`` es el claim ``role`` de Supabase (normalmente
    ``authenticated``); NO es un rol de negocio de Inventario Dedalo.
    """

    sub: uuid.UUID
    email: str | None
    role_claim: str | None
    metadata: dict[str, object]
