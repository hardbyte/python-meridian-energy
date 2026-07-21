from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from meridian_energy.models import (
    HourlyDelta,
    ReadingDirection,
    ReadingQuality,
    StatisticCursor,
    UsageSummary,
    build_incremental_statistics,
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
    assert summary.hourly
    import_stats = [s for s in summary.statistics if s.kind == "import"]
    assert import_stats
    # Cumulative sums are non-decreasing.
    sums = [s.sum for s in import_stats]
    assert sums == sorted(sums)
    # Hours are top-of-hour.
    assert all(h.start.minute == 0 and h.start.second == 0 for h in summary.hourly)


def test_incremental_statistics_do_not_rewrite_history() -> None:
    t0 = datetime(2026, 7, 1, 0, 0)
    hourly = [
        HourlyDelta(start=t0 + timedelta(hours=i), import_kwh=1.0, cost_nzd=0.3)
        for i in range(5)
    ]
    first, cursor = build_incremental_statistics(hourly, kind="import")
    assert len(first) == 5
    assert first[-1].sum == 5.0
    assert cursor.last_start == hourly[-1].start

    # Sliding window still includes old hours plus one new hour — only the new
    # hour must be emitted, continuing the absolute cumulative sum.
    slid = hourly[1:] + [
        HourlyDelta(start=t0 + timedelta(hours=5), import_kwh=2.0, cost_nzd=0.6)
    ]
    second, cursor2 = build_incremental_statistics(slid, kind="import", cursor=cursor)
    assert len(second) == 1
    assert second[0].start == t0 + timedelta(hours=5)
    assert second[0].sum == 7.0
    assert cursor2.sum == 7.0


def test_incremental_statistics_empty_when_caught_up() -> None:
    t0 = datetime(2026, 7, 1, 0, 0)
    hourly = [HourlyDelta(start=t0, import_kwh=1.5)]
    points, cursor = build_incremental_statistics(hourly, kind="import")
    assert points and cursor.sum == 1.5
    again, cursor2 = build_incremental_statistics(hourly, kind="import", cursor=cursor)
    assert again == []
    assert cursor2.sum == cursor.sum
    assert isinstance(cursor2, StatisticCursor)
