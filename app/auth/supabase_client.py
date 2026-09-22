"""Cliente HTTP aislado contra Supabase Auth.

Nunca loguea ni persiste password/tokens. Los secretos se toman de Settings
y se usan solo como cabeceras en memoria.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.config import Settings


class SupabaseAuthError(Exception):
    """El intercambio con Supabase Auth fallo (credenciales invalidas, red, etc.)."""


class SupabaseAuthClient:
    def __init__(self, settings: Settings) -> None:
        self._base = settings.SUPABASE_URL.rstrip("/")
        self._timeout = settings.AUTH_HTTP_TIMEOUT_SECONDS
        self._anon_key = settings.supabase_publishable_key

    def sign_in(self, email: str, password: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/auth/v1/token",
            json_body={"email": email, "password": password, "grant_type": "password"},
            apikey=self._anon_key,
        )

    def refresh(self, refresh_token: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/auth/v1/token",
            json_body={"refresh_token": refresh_token, "grant_type": "refresh_token"},
            apikey=self._anon_key,
        )

    def logout(self, access_token: str) -> None:
        self._request("POST", "/auth/v1/logout", bearer=access_token, apikey=self._anon_key)

    def admin_list_users(
        self, service_key: str, page: int = 1, per_page: int = 200
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/auth/v1/admin/users?page={page}&per_page={per_page}",
            apikey=service_key,
            bearer=service_key,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        apikey: str | None = None,
        bearer: str | None = None,
    ) -> dict[str, Any]:
        headers: dict[str, str] = {}
        if apikey:
            headers["apikey"] = apikey
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        try:
            response = httpx.request(
                method,
                f"{self._base}{path}",
                json=json_body,
                headers=headers,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise SupabaseAuthError("Supabase Auth no responde") from exc
        if response.status_code >= 400:
            raise SupabaseAuthError("Supabase Auth rechazo la solicitud")
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()
