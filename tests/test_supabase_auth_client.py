"""Pruebas aisladas del transporte HTTP de Supabase Auth."""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import SecretStr

from app.auth.supabase_client import SupabaseAuthClient, SupabaseAuthError
from app.core.config import Settings


@pytest.fixture
def client() -> SupabaseAuthClient:
    settings = Settings.model_construct(
        SUPABASE_URL="https://supabase.example.test/",
        SUPABASE_PUBLISHABLE_KEY=SecretStr("publishable-test-key"),
        SUPABASE_ANON_KEY=SecretStr(""),
        AUTH_HTTP_TIMEOUT_SECONDS=3.5,
    )
    return SupabaseAuthClient(settings)


def _response(
    status_code: int,
    *,
    json_body: dict[str, Any] | None = None,
    url: str = "https://supabase.example.test/",
) -> httpx.Response:
    request = httpx.Request("POST", url)
    if json_body is not None:
        return httpx.Response(status_code, json=json_body, request=request)
    return httpx.Response(status_code, request=request)


def test_sign_in_sends_password_grant_in_query_and_credentials_in_body(
    client: SupabaseAuthClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {"access_token": "access-test-token", "refresh_token": "refresh-test-token"}
    response = _response(
        200,
        json_body=payload,
        url="https://supabase.example.test/auth/v1/token?grant_type=password",
    )
    request = Mock(return_value=response)
    monkeypatch.setattr("app.auth.supabase_client.httpx.request", request)

    assert client.sign_in("qa@example.test", "password-test") == payload

    request.assert_called_once()
    method, url = request.call_args.args
    assert method == "POST"
    assert urlsplit(url).path == "/auth/v1/token"
    assert parse_qs(urlsplit(url).query) == {"grant_type": ["password"]}
    assert request.call_args.kwargs == {
        "json": {"email": "qa@example.test", "password": "password-test"},
        "headers": {"apikey": "publishable-test-key"},
        "timeout": 3.5,
    }


def test_refresh_sends_refresh_grant_in_query_and_token_in_body(
    client: SupabaseAuthClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {"access_token": "rotated-access-token", "refresh_token": "rotated-refresh-token"}
    response = _response(
        200,
        json_body=payload,
        url="https://supabase.example.test/auth/v1/token?grant_type=refresh_token",
    )
    request = Mock(return_value=response)
    monkeypatch.setattr("app.auth.supabase_client.httpx.request", request)

    assert client.refresh("old-refresh-token") == payload

    request.assert_called_once()
    method, url = request.call_args.args
    assert method == "POST"
    assert urlsplit(url).path == "/auth/v1/token"
    assert parse_qs(urlsplit(url).query) == {"grant_type": ["refresh_token"]}
    assert request.call_args.kwargs == {
        "json": {"refresh_token": "old-refresh-token"},
        "headers": {"apikey": "publishable-test-key"},
        "timeout": 3.5,
    }


def test_logout_sends_bearer_and_publishable_key(
    client: SupabaseAuthClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = Mock(
        return_value=_response(
            204,
            url="https://supabase.example.test/auth/v1/logout",
        )
    )
    monkeypatch.setattr("app.auth.supabase_client.httpx.request", request)

    assert client.logout("access-test-token") is None

    request.assert_called_once_with(
        "POST",
        "https://supabase.example.test/auth/v1/logout",
        json=None,
        headers={
            "apikey": "publishable-test-key",
            "Authorization": "Bearer access-test-token",
        },
        timeout=3.5,
    )


@pytest.mark.parametrize("status_code", [400, 401, 500])
def test_http_errors_are_wrapped(
    client: SupabaseAuthClient,
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    request = Mock(
        return_value=_response(
            status_code,
            json_body={"message": "rejected"},
            url="https://supabase.example.test/auth/v1/token?grant_type=password",
        )
    )
    monkeypatch.setattr("app.auth.supabase_client.httpx.request", request)

    with pytest.raises(SupabaseAuthError, match="rechazo"):
        client.sign_in("qa@example.test", "password-test")


def test_network_errors_are_wrapped(
    client: SupabaseAuthClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    network_error = httpx.ConnectError("connection unavailable")
    request = Mock(side_effect=network_error)
    monkeypatch.setattr("app.auth.supabase_client.httpx.request", request)

    with pytest.raises(SupabaseAuthError, match="no responde") as exc_info:
        client.refresh("refresh-test-token")

    assert exc_info.value.__cause__ is network_error
