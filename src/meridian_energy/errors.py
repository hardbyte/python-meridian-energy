"""Typed errors raised by the Meridian Energy client."""

from __future__ import annotations


class MeridianEnergyError(Exception):
    """Base error for the Meridian Energy client."""


class MeridianAuthError(MeridianEnergyError):
    """Raised when authentication fails."""


class ReauthenticationRequiredError(MeridianAuthError):
    """Stored tokens can no longer be refreshed; interactive OTP is required."""


class MeridianApiError(MeridianEnergyError):
    """Raised when the GraphQL API returns an error."""
