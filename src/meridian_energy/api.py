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
# Default page size when walking the cursor (keeps payloads modest).
DEFAULT_PAGE_SIZE = 500
# Hard stop so a runaway cursor cannot loop forever.
MAX_MEASUREMENT_PAGES = 50


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
        # Let auth refresh reuse this client (important under HA's event loop).
        if getattr(auth, "_httpx_client", None) is None:
            auth._httpx_client = self._client

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
        page_size: int = DEFAULT_PAGE_SIZE,
        timezone: str = DEFAULT_TIMEZONE,
    ) -> list[Measurement]:
        """Fetch interval measurements for one account, following page cursors.

        ``direction=None`` omits the readingDirection filter (API default is
        consumption-only). Pass ``GENERATION`` for export/solar.

        The API caps ``first`` at 1500. This method pages with ``after`` until
        ``pageInfo.hasNextPage`` is false (or ``MAX_MEASUREMENT_PAGES``).
        """
        if page_size < 1 or page_size > MAX_MEASUREMENTS_PAGE:
            raise ValueError(
                f"page_size={page_size} must be between 1 and {MAX_MEASUREMENTS_PAGE}"
            )

        electricity_filters: dict[str, Any] = {}
        if direction is not None:
            electricity_filters["readingDirection"] = direction.value
        if frequency is not None:
            electricity_filters["readingFrequencyType"] = frequency.value

        base_variables: dict[str, Any] = {
            "accountNumber": account_number,
            "first": page_size,
            "timezone": timezone,
            "utilityFilters": [{"electricityFilters": electricity_filters}],
        }
        if start_on is not None:
            base_variables["startOn"] = start_on.isoformat()
        if end_on is not None:
            base_variables["endOn"] = end_on.isoformat()
        if start_at is not None:
            base_variables["startAt"] = start_at.isoformat()
        if end_at is not None:
            base_variables["endAt"] = end_at.isoformat()

        measurements: list[Measurement] = []
        after: str | None = None
        for page_num in range(MAX_MEASUREMENT_PAGES):
            variables = dict(base_variables)
            if after is not None:
                variables["after"] = after

            data = await self._graphql(
                "measurementsAllProperties",
                MEASUREMENTS_ALL_PROPERTIES_QUERY,
                variables,
            )
            properties = data["account"]["properties"]
            page_rows = flatten_property_measurements(properties)
            measurements.extend(page_rows)

            has_next, end_cursor = _page_info(properties)
            logger.debug(
                "measurements page %s: +%s rows (total %s) has_next=%s",
                page_num,
                len(page_rows),
                len(measurements),
                has_next,
            )
            if not has_next:
                break
            if not end_cursor:
                raise MeridianApiError(
                    "measurements pageInfo.hasNextPage without endCursor"
                )
            if end_cursor == after:
                raise MeridianApiError("measurements pagination cursor did not advance")
            after = end_cursor
        else:
            raise MeridianApiError(
                f"measurements exceeded {MAX_MEASUREMENT_PAGES} pages "
                f"({len(measurements)} rows); widen filters or raise the limit"
            )

        return measurements

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
        API to return rows. Pages automatically past the 1500-row cap.
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


def _page_info(properties: list[dict[str, Any]]) -> tuple[bool, str | None]:
    """Combine pageInfo across properties.

    ``after`` is a single argument shared by every property's measurements
    connection in this query shape, so we treat any property reporting
    ``hasNextPage`` as needing another round.
    """
    has_next = False
    end_cursor: str | None = None
    for prop in properties:
        measurements = prop.get("measurements") or {}
        page_info = measurements.get("pageInfo") or {}
        if page_info.get("hasNextPage"):
            has_next = True
            end_cursor = page_info.get("endCursor") or end_cursor
    return has_next, end_cursor
