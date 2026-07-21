"""Authentication and session lifecycle for the Meridian Energy API.

Meridian's current customer portal authenticates via an emailed one-time code
rather than a password: request an OTP, verify it to get a Firebase custom
token, then exchange that for a Firebase ID/refresh token pair. GraphQL calls
use the ID token as a bearer token.

This flow and the Firebase project config were reverse engineered from
Meridian's public web app bundle.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from meridian_energy.const import (
    AUTH_BASE_URL,
    BRAND,
    CLIENT_HEADERS,
    DEFAULT_REDIRECT_URL,
    FIREBASE_API_KEY,
    IDENTITY_TOOLKIT_URL,
    SECURE_TOKEN_URL,
)
from meridian_energy.errors import MeridianAuthError, ReauthenticationRequiredError

logger = logging.getLogger("meridian_energy.auth")

# Refresh this long before recorded expiry to absorb clock skew / in-flight races.
_EXPIRY_SAFETY_MARGIN = timedelta(seconds=60)

OnTokenUpdate = Callable[["TokenSet"], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class TokenSet:
    """An immutable Firebase ID/refresh token pair, safe to persist."""

    id_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at: datetime

    def __post_init__(self) -> None:
        if self.expires_at.tzinfo is None:
            object.__setattr__(self, "expires_at", self.expires_at.replace(tzinfo=UTC))

    @property
    def is_expired(self) -> bool:
        """Whether this token should be refreshed (with a safety margin)."""
        return datetime.now(UTC) >= self.expires_at - _EXPIRY_SAFETY_MARGIN

    def to_dict(self) -> dict[str, Any]:
        """Serialise for storage (config entry, CLI cache, etc.)."""
        return {
            "id_token": self.id_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TokenSet:
        """Deserialise from storage."""
        expires_at = data["expires_at"]
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)
        return cls(
            id_token=data["id_token"],
            refresh_token=data["refresh_token"],
            expires_at=expires_at,
        )


class MeridianEnergyAuth(httpx.Auth):
    """OTP sign-in flow and transparent token refresh.

    Mirrors the evnex client's EvnexAuth: a central httpx.Auth that injects
    the bearer token and refreshes on expiry or a 401, persisting the new
    tokens via an on_token_update callback before they're used.
    """

    requires_response_body = True

    def __init__(
        self,
        tokens: TokenSet | None = None,
        on_token_update: OnTokenUpdate | None = None,
        *,
        httpx_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._tokens = tokens
        self._on_token_update = on_token_update
        self._refresh_lock = asyncio.Lock()
        # Prefer a shared client (e.g. HA's) so refresh does not construct a
        # new SSL context on the event loop.
        self._httpx_client = httpx_client

    @property
    def tokens(self) -> TokenSet | None:
        """The current token set, if signed in."""
        return self._tokens

    async def request_otp(
        self,
        client: httpx.AsyncClient,
        email: str,
        *,
        redirect_url: str = DEFAULT_REDIRECT_URL,
    ) -> str:
        """Request an emailed OTP code. Returns a journey_id for verify_otp."""
        journey_id = str(uuid.uuid4())
        response = await client.post(
            f"{AUTH_BASE_URL}/cf/email-connector",
            json={
                "email": email,
                "brand": BRAND,
                "redirectUrl": redirect_url,
                "journeyId": journey_id,
                "otpEnabled": True,
            },
            headers=CLIENT_HEADERS,
        )
        if response.status_code == 403:
            raise MeridianAuthError("Brand access denied")
        if response.status_code == 404:
            raise MeridianAuthError("No Meridian account found for that email")
        if response.status_code == 400:
            raise MeridianAuthError(
                f"Failed to request OTP: {response.status_code} {response.text}"
            )
        if not response.is_success:
            raise MeridianAuthError(
                f"Failed to request OTP: {response.status_code} {response.text}"
            )
        return journey_id

    async def verify_otp(
        self,
        client: httpx.AsyncClient,
        email: str,
        otp: str,
        journey_id: str,
    ) -> TokenSet:
        """Verify the emailed OTP and complete sign-in."""
        response = await client.post(
            f"{AUTH_BASE_URL}/cf/email-otp-authenticator",
            json={
                "email": email,
                "otp": otp,
                "brand": BRAND,
                "journeyId": journey_id,
            },
            headers=CLIENT_HEADERS,
        )
        try:
            data = response.json()
        except ValueError:
            data = {}
        if not response.is_success:
            raise MeridianAuthError(
                (data.get("error") if isinstance(data, dict) else None)
                or "Failed to validate OTP"
            )

        custom_token = data["customToken"]
        tokens = await self._exchange_custom_token(client, custom_token)
        await self._set_tokens(tokens)
        return tokens

    async def _exchange_custom_token(
        self, client: httpx.AsyncClient, custom_token: str
    ) -> TokenSet:
        """Exchange a Firebase custom token for an ID/refresh token pair."""
        response = await client.post(
            f"{IDENTITY_TOOLKIT_URL}/accounts:signInWithCustomToken",
            params={"key": FIREBASE_API_KEY},
            json={"token": custom_token, "returnSecureToken": True},
        )
        if not response.is_success:
            raise MeridianAuthError(
                f"Failed to exchange custom token: {response.status_code}"
            )
        payload = response.json()
        return TokenSet(
            id_token=payload["idToken"],
            refresh_token=payload["refreshToken"],
            expires_at=datetime.now(UTC) + timedelta(seconds=int(payload["expiresIn"])),
        )

    async def _set_tokens(self, tokens: TokenSet) -> None:
        self._tokens = tokens
        if self._on_token_update is not None:
            await self._on_token_update(tokens)

    async def refresh(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        force: bool = True,
    ) -> TokenSet:
        """Refresh tokens. Raises ReauthenticationRequiredError if impossible.

        Concurrent callers share one in-flight refresh via ``_refresh_lock``.
        When ``force`` is false, returns the current tokens if another caller
        already refreshed them while we waited for the lock.
        """
        async with self._refresh_lock:
            if self._tokens is None:
                raise ReauthenticationRequiredError("Not authenticated")
            if not force and not self._tokens.is_expired:
                return self._tokens

            owns_client = False
            if client is None:
                client = self._httpx_client
            if client is None:
                # Last resort for bare CLI use; avoid on the HA event loop.
                client = httpx.AsyncClient(timeout=30.0)
                owns_client = True
            try:
                response = await client.post(
                    SECURE_TOKEN_URL,
                    params={"key": FIREBASE_API_KEY},
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": self._tokens.refresh_token,
                    },
                )
            finally:
                if owns_client:
                    await client.aclose()

            if not response.is_success:
                raise ReauthenticationRequiredError(
                    f"Failed to refresh session: {response.status_code}"
                )
            payload = response.json()
            tokens = TokenSet(
                id_token=payload["id_token"],
                refresh_token=payload["refresh_token"],
                expires_at=datetime.now(UTC)
                + timedelta(seconds=int(payload["expires_in"])),
            )
            await self._set_tokens(tokens)
            return tokens

    def auth_flow(self, request: httpx.Request):  # pragma: no cover
        """Not supported; this client is async-only."""
        raise NotImplementedError("MeridianEnergyAuth is async-only")

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        """Attach a bearer token, refreshing first if expired or rejected."""
        if self._tokens is None:
            raise MeridianAuthError("Not authenticated")

        if self._tokens.is_expired:
            await self.refresh(force=False)

        request.headers["Authorization"] = f"Bearer {self._tokens.id_token}"
        response = yield request

        if response.status_code == 401:
            await self.refresh(force=True)
            request.headers["Authorization"] = f"Bearer {self._tokens.id_token}"
            yield request
