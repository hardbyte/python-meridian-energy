"""Python client for the Meridian Energy (NZ) customer API."""

from __future__ import annotations

import logging

from meridian_energy.api import MeridianEnergyApi
from meridian_energy.auth import MeridianEnergyAuth, TokenSet
from meridian_energy.errors import (
    MeridianApiError,
    MeridianAuthError,
    MeridianEnergyError,
    ReauthenticationRequiredError,
)
from meridian_energy.models import (
    Account,
    HourlyDelta,
    Measurement,
    MeterPoint,
    Property,
    ReadingDirection,
    ReadingFrequency,
    ReadingQuality,
    StatisticCursor,
    UsageSummary,
    build_incremental_statistics,
)

logging.getLogger(__name__).addHandler(logging.NullHandler())

__all__ = [
    "Account",
    "HourlyDelta",
    "Measurement",
    "MeridianApiError",
    "MeridianAuthError",
    "MeridianEnergyApi",
    "MeridianEnergyAuth",
    "MeridianEnergyError",
    "MeterPoint",
    "Property",
    "ReadingDirection",
    "ReadingFrequency",
    "ReadingQuality",
    "ReauthenticationRequiredError",
    "StatisticCursor",
    "TokenSet",
    "UsageSummary",
    "build_incremental_statistics",
]
