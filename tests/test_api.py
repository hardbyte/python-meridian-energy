from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import respx

from meridian_energy.api import MeridianEnergyApi
from meridian_energy.auth import MeridianEnergyAuth, TokenSet
from meridian_energy.const import GRAPHQL_URL
from meridian_energy.models import ReadingDirection

FIXTURES = Path(__file__).parent / "fixtures"


def _auth() -> MeridianEnergyAuth:
    return MeridianEnergyAuth(
        tokens=TokenSet(
            id_token="id",
            refresh_token="refresh",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )


@respx.mock
async def test_get_accounts() -> None:
    respx.post(GRAPHQL_URL).mock(
        return_value=httpx.Response(
            200, json=json.loads((FIXTURES / "accounts.json").read_text())
        )
    )
    async with MeridianEnergyApi(_auth()) as api:
        accounts = await api.get_accounts()
    assert accounts[0].number == "A-EXAMPLE01"
    assert accounts[0].primary_icp == "0000000000EXAMP"


@respx.mock
async def test_get_measurements_sends_utility_filters() -> None:
    route = respx.post(GRAPHQL_URL).mock(
        return_value=httpx.Response(
            200,
            json=json.loads((FIXTURES / "measurements_consumption.json").read_text()),
        )
    )
    async with MeridianEnergyApi(_auth()) as api:
        readings = await api.get_measurements(
            "A-EXAMPLE01",
            start_on=date(2026, 7, 1),
            end_on=date(2026, 7, 14),
            direction=ReadingDirection.CONSUMPTION,
        )
    assert readings
    body = json.loads(route.calls.last.request.content)
    assert body["operationName"] == "measurementsAllProperties"
    filters = body["variables"]["utilityFilters"][0]["electricityFilters"]
    assert filters["readingDirection"] == "CONSUMPTION"
    assert filters["readingFrequencyType"] == "RAW_INTERVAL"
    assert body["variables"]["first"] == 1500


@respx.mock
async def test_get_usage_merges_generation() -> None:
    cons = json.loads((FIXTURES / "measurements_consumption.json").read_text())
    gen = json.loads((FIXTURES / "measurements_generation.json").read_text())
    route = respx.post(GRAPHQL_URL)
    route.side_effect = [
        httpx.Response(200, json=cons),
        httpx.Response(200, json=gen),
    ]
    async with MeridianEnergyApi(_auth()) as api:
        summary = await api.get_usage("A-EXAMPLE01", days=7)
    assert route.call_count == 2
    assert summary.import_kwh > 0
    assert any(m.is_export for m in summary.measurements) or summary.export_kwh >= 0
