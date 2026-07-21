from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from meridian_energy.auth import MeridianEnergyAuth, TokenSet
from meridian_energy.const import (
    AUTH_BASE_URL,
    DEFAULT_REDIRECT_URL,
    FIREBASE_API_KEY,
    IDENTITY_TOOLKIT_URL,
    SECURE_TOKEN_URL,
)
from meridian_energy.errors import MeridianAuthError, ReauthenticationRequiredError

FIXTURES = Path(__file__).parent / "fixtures"


@respx.mock
async def test_request_otp_sends_redirect_url() -> None:
    route = respx.post(f"{AUTH_BASE_URL}/cf/email-connector").mock(
        return_value=httpx.Response(
            200, json=json.loads((FIXTURES / "otp_request_ok.json").read_text())
        )
    )
    auth = MeridianEnergyAuth()
    async with httpx.AsyncClient() as client:
        journey_id = await auth.request_otp(client, "user@example.com")
    assert journey_id
    assert route.called
    body = json.loads(route.calls.last.request.content)
    assert body["redirectUrl"] == DEFAULT_REDIRECT_URL
    assert body["email"] == "user@example.com"
    assert body["otpEnabled"] is True


@respx.mock
async def test_verify_otp_exchanges_custom_token() -> None:
    respx.post(f"{AUTH_BASE_URL}/cf/email-otp-authenticator").mock(
        return_value=httpx.Response(
            200, json=json.loads((FIXTURES / "otp_verify_ok.json").read_text())
        )
    )
    respx.post(
        url__regex=rf"{IDENTITY_TOOLKIT_URL}/accounts:signInWithCustomToken.*"
    ).mock(
        return_value=httpx.Response(
            200, json=json.loads((FIXTURES / "firebase_signin.json").read_text())
        )
    )
    updates: list[TokenSet] = []

    async def on_update(tokens: TokenSet) -> None:
        updates.append(tokens)

    auth = MeridianEnergyAuth(on_token_update=on_update)
    async with httpx.AsyncClient() as client:
        tokens = await auth.verify_otp(
            client, "user@example.com", "123456", "journey-1"
        )
    assert tokens.id_token == "fake-id-token"
    assert not tokens.is_expired
    assert updates and updates[0].id_token == "fake-id-token"


@respx.mock
async def test_request_otp_unknown_email() -> None:
    respx.post(f"{AUTH_BASE_URL}/cf/email-connector").mock(
        return_value=httpx.Response(404, json={"error": "not found"})
    )
    auth = MeridianEnergyAuth()
    async with httpx.AsyncClient() as client:
        with pytest.raises(MeridianAuthError, match="No Meridian account"):
            await auth.request_otp(client, "missing@example.com")


@respx.mock
async def test_refresh_updates_tokens() -> None:
    respx.post(url__regex=rf"{SECURE_TOKEN_URL}.*").mock(
        return_value=httpx.Response(
            200, json=json.loads((FIXTURES / "firebase_refresh.json").read_text())
        )
    )
    auth = MeridianEnergyAuth(
        tokens=TokenSet(
            id_token="old",
            refresh_token="refresh",
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    )
    tokens = await auth.refresh()
    assert tokens.id_token == "fake-id-token-2"
    assert FIREBASE_API_KEY  # referenced so const stays imported/used in mental model


@respx.mock
async def test_refresh_failure_requires_reauth() -> None:
    respx.post(url__regex=rf"{SECURE_TOKEN_URL}.*").mock(
        return_value=httpx.Response(400, json={"error": "invalid"})
    )
    auth = MeridianEnergyAuth(
        tokens=TokenSet(
            id_token="old",
            refresh_token="bad",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    with pytest.raises(ReauthenticationRequiredError):
        await auth.refresh()
