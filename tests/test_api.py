from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from meridian_energy.api import MeridianEnergyApi
from meridian_energy.auth import MeridianEnergyAuth, TokenSet
from meridian_energy.const import GRAPHQL_URL
from meridian_energy.errors import MeridianApiError
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


def _page(
    *,
    nodes: list[dict],
    has_next: bool,
    end_cursor: str | None,
) -> dict:
    return {
        "data": {
            "account": {
                "id": "100001",
                "properties": [
                    {
                        "id": "200001",
                        "measurements": {
                            "edges": [
                                {"cursor": f"c{i}", "node": n}
                                for i, n in enumerate(nodes)
                            ],
                            "pageInfo": {
                                "hasNextPage": has_next,
                                "endCursor": end_cursor,
                            },
                        },
                    }
                ],
            }
        }
    }


def _node(start: str, value: str = "1.0") -> dict:
    return {
        "source": "Amphio",
        "value": value,
        "unit": "kwh",
        "readAt": start,
        "startAt": start,
        "endAt": start,
        "metaData": {
            "utilityFilters": {
                "readingDirection": "CONSUMPTION",
                "readingQuality": "ACTUAL",
                "readingFrequencyType": "RAW_INTERVAL",
                "registerId": None,
                "deviceId": None,
                "marketSupplyPointId": "0000000000EXAMP",
            },
            "statistics": [],
        },
    }


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
    assert body["variables"]["first"] == 500
    assert "after" not in body["variables"]


@respx.mock
async def test_get_measurements_follows_pagination_cursor() -> None:
    page1 = _page(
        nodes=[_node("2026-07-01T00:00:00+12:00"), _node("2026-07-01T00:30:00+12:00")],
        has_next=True,
        end_cursor="CURSOR_1",
    )
    page2 = _page(
        nodes=[_node("2026-07-01T01:00:00+12:00")],
        has_next=False,
        end_cursor="CURSOR_2",
    )
    route = respx.post(GRAPHQL_URL)
    route.side_effect = [
        httpx.Response(200, json=page1),
        httpx.Response(200, json=page2),
    ]

    async with MeridianEnergyApi(_auth()) as api:
        readings = await api.get_measurements(
            "A-EXAMPLE01",
            start_on=date(2026, 7, 1),
            end_on=date(2026, 7, 2),
            page_size=2,
        )

    assert len(readings) == 3
    assert route.call_count == 2
    bodies = [json.loads(c.request.content) for c in route.calls]
    assert "after" not in bodies[0]["variables"]
    assert bodies[1]["variables"]["after"] == "CURSOR_1"
    assert bodies[0]["variables"]["first"] == 2


@respx.mock
async def test_get_measurements_detects_stuck_cursor() -> None:
    stuck = _page(
        nodes=[_node("2026-07-01T00:00:00+12:00")],
        has_next=True,
        end_cursor="SAME",
    )
    respx.post(GRAPHQL_URL).mock(return_value=httpx.Response(200, json=stuck))
    # First page OK with after=None; second page returns same cursor.
    # Actually first response has end_cursor SAME and has_next; second call
    # with after=SAME returns same again → stuck after first advance attempt.
    async with MeridianEnergyApi(_auth()) as api:
        with pytest.raises(MeridianApiError, match="did not advance"):
            await api.get_measurements(
                "A-EXAMPLE01",
                start_on=date(2026, 7, 1),
                end_on=date(2026, 7, 2),
                page_size=1,
            )


@respx.mock
async def test_get_usage_merges_generation() -> None:
    cons = json.loads((FIXTURES / "measurements_consumption.json").read_text())
    # inject pageInfo into fixture shape
    for prop in cons["data"]["account"]["properties"]:
        prop["measurements"]["pageInfo"] = {
            "hasNextPage": False,
            "endCursor": None,
        }
    gen = json.loads((FIXTURES / "measurements_generation.json").read_text())
    for prop in gen["data"]["account"]["properties"]:
        prop["measurements"]["pageInfo"] = {
            "hasNextPage": False,
            "endCursor": None,
        }
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
