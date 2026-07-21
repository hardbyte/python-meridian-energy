"""GraphQL client for the Meridian Energy Kraken API."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from meridian_energy.auth import MeridianEnergyAuth
from meridian_energy.const import DEFAULT_TIMEZONE, GRAPHQL_URL
from meridian_energy.errors import MeridianApiError
from meridian_energy.models import (
    Account,
    Measurement,
    ReadingDirection,
    ReadingFrequency,
    UsageSummary,
    flatten_property_measurements,
    parse_accounts,
)
from meridian_energy.queries import (
    ACCOUNTS_LIST_QUERY,
    MEASUREMENTS_ALL_PROPERTIES_QUERY,
)

logger = logging.getLogger("meridian_energy.api")

# Server-side max for the measurements connection `first` argument.
MAX_MEASUREMENTS_PAGE = 1500


class MeridianEnergyApi:
    """Thin async client over Meridian's customer GraphQL API."""

    def __init__(
        self,
        auth: MeridianEnergyAuth,
        httpx_client: httpx.AsyncClient | None = None,
        *,
        owns_client: bool | None = None,
    ) -> None:
        self._auth = auth
        if httpx_client is None:
            self._client = httpx.AsyncClient(timeout=60.0)
            self._owns_client = True
        else:
            self._client = httpx_client
            self._owns_client = False if owns_client is None else owns_client

    @property
    def auth(self) -> MeridianEnergyAuth:
        return self._auth

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> MeridianEnergyApi:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def _graphql(
        self,
        operation_name: str,
        query: str,
        variables: dict[str, Any],
    ) -> dict[str, Any]:
        response = await self._client.post(
            GRAPHQL_URL,
            json={
                "operationName": operation_name,
                "query": query,
                "variables": variables,
            },
            auth=self._auth,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            raise MeridianApiError(str(payload["errors"]))
        data = payload.get("data")
        if data is None:
            raise MeridianApiError("GraphQL response missing data")
        return data

    async def get_accounts(self) -> list[Account]:
        """List accounts (and properties / ICPs) visible to the signed-in user."""
        data = await self._graphql("accountsList", ACCOUNTS_LIST_QUERY, {})
        return parse_accounts(data["viewer"]["accounts"])

    async def get_measurements(
        self,
        account_number: str,
        *,
        start_on: date | None = None,
        end_on: date | None = None,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        direction: ReadingDirection | None = ReadingDirection.CONSUMPTION,
        frequency: ReadingFrequency | None = ReadingFrequency.RAW_INTERVAL,
        first: int = MAX_MEASUREMENTS_PAGE,
        timezone: str = DEFAULT_TIMEZONE,
    ) -> list[Measurement]:
        """Fetch interval measurements for one account.

        ``direction=None`` omits the readingDirection filter (API default is
        consumption-only). Pass ``GENERATION`` for export/solar.
        """
        if first > MAX_MEASUREMENTS_PAGE:
            raise ValueError(
                f"first={first} exceeds API limit of {MAX_MEASUREMENTS_PAGE}"
            )

        electricity_filters: dict[str, Any] = {}
        if direction is not None:
            electricity_filters["readingDirection"] = direction.value
        if frequency is not None:
            electricity_filters["readingFrequencyType"] = frequency.value

        variables: dict[str, Any] = {
            "accountNumber": account_number,
            "first": first,
            "timezone": timezone,
            "utilityFilters": [{"electricityFilters": electricity_filters}],
        }
        if start_on is not None:
            variables["startOn"] = start_on.isoformat()
        if end_on is not None:
            variables["endOn"] = end_on.isoformat()
        if start_at is not None:
            variables["startAt"] = start_at.isoformat()
        if end_at is not None:
            variables["endAt"] = end_at.isoformat()

        data = await self._graphql(
            "measurementsAllProperties",
            MEASUREMENTS_ALL_PROPERTIES_QUERY,
            variables,
        )
        properties = data["account"]["properties"]
        return flatten_property_measurements(properties)

    async def get_usage(
        self,
        account_number: str,
        *,
        days: int = 10,
        end_on: date | None = None,
        frequency: ReadingFrequency = ReadingFrequency.RAW_INTERVAL,
        include_generation: bool = True,
        skip_estimated: bool = False,
        timezone: str = DEFAULT_TIMEZONE,
    ) -> UsageSummary:
        """Fetch consumption (+ optional generation) and build a usage summary.

        Uses date-bounded queries with ``utilityFilters`` — required for the
        API to return rows. Half-hourly ``RAW_INTERVAL`` is the default and
        fits ~31 days under the 1500-row page limit per direction.
        """
        if end_on is None:
            end_on = datetime.now().astimezone().date()
        start_on = end_on - timedelta(days=days)

        measurements = await self.get_measurements(
            account_number,
            start_on=start_on,
            end_on=end_on,
            direction=ReadingDirection.CONSUMPTION,
            frequency=frequency,
            timezone=timezone,
        )
        if include_generation:
            try:
                generation = await self.get_measurements(
                    account_number,
                    start_on=start_on,
                    end_on=end_on,
                    direction=ReadingDirection.GENERATION,
                    frequency=frequency,
                    timezone=timezone,
                )
                measurements = [*measurements, *generation]
            except MeridianApiError:
                logger.debug(
                    "Generation measurements unavailable for %s", account_number
                )

        return UsageSummary.from_measurements(
            measurements, skip_estimated=skip_estimated
        )
