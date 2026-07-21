from __future__ import annotations

import json
from pathlib import Path

from meridian_energy.models import (
    ReadingDirection,
    ReadingQuality,
    UsageSummary,
    flatten_property_measurements,
    parse_accounts,
    parse_measurement_node,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_accounts() -> None:
    payload = json.loads((FIXTURES / "accounts.json").read_text())
    accounts = parse_accounts(payload["data"]["viewer"]["accounts"])
    assert len(accounts) == 1
    account = accounts[0]
    assert account.number == "A-EXAMPLE01"
    assert account.primary_icp == "0000000000EXAMP"
    assert account.properties[0].address is not None


def test_parse_consumption_node_cost_in_nzd() -> None:
    payload = json.loads((FIXTURES / "measurements_consumption.json").read_text())
    node = payload["data"]["account"]["properties"][0]["measurements"]["edges"][0][
        "node"
    ]
    reading = parse_measurement_node(node)
    assert reading is not None
    assert reading.direction == ReadingDirection.CONSUMPTION
    assert reading.quality in {ReadingQuality.ACTUAL, ReadingQuality.ESTIMATE}
    assert reading.value > 0
    # Costs from the API are cents; model exposes NZD dollars.
    assert reading.consumption_cost_nzd is not None
    assert reading.consumption_cost_nzd < 5  # single half-hour shouldn't be $5+
    assert reading.cost_currency == "NZD"


def test_parse_generation_node() -> None:
    payload = json.loads((FIXTURES / "measurements_generation.json").read_text())
    node = payload["data"]["account"]["properties"][0]["measurements"]["edges"][0][
        "node"
    ]
    reading = parse_measurement_node(node)
    assert reading is not None
    assert reading.direction == ReadingDirection.GENERATION
    assert reading.is_export


def test_usage_summary_cumulative() -> None:
    cons = json.loads((FIXTURES / "measurements_consumption.json").read_text())
    gen = json.loads((FIXTURES / "measurements_generation.json").read_text())
    measurements = flatten_property_measurements(
        cons["data"]["account"]["properties"]
    ) + flatten_property_measurements(gen["data"]["account"]["properties"])
    summary = UsageSummary.from_measurements(measurements)
    assert summary.import_kwh > 0
    assert summary.export_kwh >= 0
    assert summary.cost_nzd > 0
    import_stats = [s for s in summary.statistics if s.kind == "import"]
    assert import_stats
    # Cumulative sums are non-decreasing.
    sums = [s.sum for s in import_stats]
    assert sums == sorted(sums)
