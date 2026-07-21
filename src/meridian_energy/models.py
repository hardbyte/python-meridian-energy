"""Pydantic models and pure helpers for Meridian Energy data."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ReadingDirection(StrEnum):
    """Direction of an electricity interval reading.

    Meridian's GraphQL enum is ``ReadingDirectionType`` with values
    ``CONSUMPTION`` (grid import) and ``GENERATION`` (export / solar).
    """

    CONSUMPTION = "CONSUMPTION"
    GENERATION = "GENERATION"
    UNKNOWN = "UNKNOWN"


class ReadingQuality(StrEnum):
    """Quality flag on an interval reading."""

    ACTUAL = "ACTUAL"
    ESTIMATE = "ESTIMATE"
    UNKNOWN = "UNKNOWN"


class ReadingFrequency(StrEnum):
    """Interval aggregation requested from / returned by the API."""

    RAW_INTERVAL = "RAW_INTERVAL"  # typically 30 minutes
    HOUR_INTERVAL = "HOUR_INTERVAL"
    DAY_INTERVAL = "DAY_INTERVAL"


class MeterPoint(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str | None = None
    market_identifier: str | None = Field(default=None, alias="marketIdentifier")


class Property(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str | None = None
    address: str | None = None
    meter_points: list[MeterPoint] = Field(default_factory=list, alias="meterPoints")


class Account(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    number: str
    status: str | None = None
    billing_name: str | None = Field(default=None, alias="billingName")
    id: str | None = None
    properties: list[Property] = Field(default_factory=list)

    @property
    def primary_icp(self) -> str | None:
        """First market identifier (ICP) found on any property."""
        for prop in self.properties:
            for meter in prop.meter_points:
                if meter.market_identifier:
                    return meter.market_identifier
        return None


class Measurement(BaseModel):
    """A single interval usage reading."""

    model_config = ConfigDict(extra="ignore")

    value: float
    unit: str | None = None
    source: str | None = None
    read_at: datetime | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    direction: ReadingDirection = ReadingDirection.CONSUMPTION
    quality: ReadingQuality = ReadingQuality.UNKNOWN
    frequency: ReadingFrequency | None = None
    # Energy cost for this interval only (NZD major units), excl. standing charge.
    consumption_cost_nzd: float | None = None
    standing_charge_nzd: float | None = None
    cost_currency: str | None = None
    property_id: str | None = None

    @property
    def period_start(self) -> datetime | None:
        """Preferred start timestamp for the interval (falls back to read_at)."""
        return self.start_at or self.read_at

    @property
    def is_export(self) -> bool:
        return self.direction == ReadingDirection.GENERATION

    @property
    def is_estimated(self) -> bool:
        return self.quality == ReadingQuality.ESTIMATE


class IntervalStatistic(BaseModel):
    """One cumulative external-statistic point for Home Assistant."""

    start: datetime
    sum: float
    kind: Literal["import", "export", "cost"]


class UsageSummary(BaseModel):
    """Aggregated interval usage suitable for HA external statistics."""

    measurements: list[Measurement]
    import_kwh: float = 0.0
    export_kwh: float = 0.0
    cost_nzd: float = 0.0
    cost_currency: str | None = None
    statistics: list[IntervalStatistic] = Field(default_factory=list)

    @classmethod
    def from_measurements(
        cls,
        measurements: list[Measurement],
        *,
        skip_estimated: bool = False,
        include_standing_charge: bool = False,
    ) -> UsageSummary:
        """Build cumulative import/export/cost series from interval readings.

        Sub-hour readings are aggregated into hour-aligned buckets so Home
        Assistant external statistics accept the timestamps (minutes and
        seconds must be 0). Cumulative sums cover the published window only;
        callers should use a stable lookback so the recorder can de-dupe by
        statistic id + start time.
        """
        ordered = sorted(
            (m for m in measurements if m.period_start is not None),
            key=lambda m: m.period_start or datetime.min,
        )

        # hour_start -> [import_kwh, export_kwh, cost_nzd]
        buckets: dict[datetime, list[float]] = {}
        currency: str | None = None

        for reading in ordered:
            if skip_estimated and reading.is_estimated:
                continue
            start = reading.period_start
            assert start is not None
            hour = start.replace(minute=0, second=0, microsecond=0)
            slot = buckets.setdefault(hour, [0.0, 0.0, 0.0])
            if reading.is_export:
                slot[1] += reading.value
            else:
                slot[0] += reading.value
            interval_cost = reading.consumption_cost_nzd
            if include_standing_charge and reading.standing_charge_nzd is not None:
                interval_cost = (interval_cost or 0.0) + reading.standing_charge_nzd
            if interval_cost is not None:
                slot[2] += interval_cost
                currency = reading.cost_currency or currency

        import_sum = export_sum = cost_sum = 0.0
        stats: list[IntervalStatistic] = []
        for hour in sorted(buckets):
            imp, exp, cost = buckets[hour]
            if imp:
                import_sum += imp
                stats.append(
                    IntervalStatistic(start=hour, sum=import_sum, kind="import")
                )
            if exp:
                export_sum += exp
                stats.append(
                    IntervalStatistic(start=hour, sum=export_sum, kind="export")
                )
            if cost:
                cost_sum += cost
                stats.append(IntervalStatistic(start=hour, sum=cost_sum, kind="cost"))

        return cls(
            measurements=ordered,
            import_kwh=import_sum,
            export_kwh=export_sum,
            cost_nzd=cost_sum,
            cost_currency=currency,
            statistics=stats,
        )


def _parse_dt(raw: Any) -> datetime | None:
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw
    text = str(raw).replace("Z", "+00:00")
    return datetime.fromisoformat(text)


def _cents_to_nzd(amount: Any) -> float | None:
    """Convert Meridian's cost amounts to NZD dollars.

    Live responses show ``pricePerUnit.amount`` ≈ 25–30 with unit
    KILOWATT_HOURS (i.e. c/kWh) and ``estimatedAmount`` ≈ kWh × c/kWh,
    so amounts are in cents. HA's Energy dashboard expects major currency
    units (NZD).
    """
    if amount is None:
        return None
    return float(amount) / 100.0


def parse_measurement_node(
    node: dict[str, Any], *, property_id: str | None = None
) -> Measurement | None:
    """Parse one GraphQL measurement edge node into a Measurement."""
    value = node.get("value")
    if value is None:
        return None

    meta = node.get("metaData") or {}
    filters = meta.get("utilityFilters") or {}
    raw_direction = (filters.get("readingDirection") or "CONSUMPTION").upper()
    try:
        direction = ReadingDirection(raw_direction)
    except ValueError:
        direction = ReadingDirection.UNKNOWN

    raw_quality = (filters.get("readingQuality") or "").upper()
    try:
        quality = ReadingQuality(raw_quality) if raw_quality else ReadingQuality.UNKNOWN
    except ValueError:
        quality = ReadingQuality.UNKNOWN

    raw_freq = (filters.get("readingFrequencyType") or "").upper()
    try:
        frequency = ReadingFrequency(raw_freq) if raw_freq else None
    except ValueError:
        frequency = None

    consumption_cost: float | None = None
    standing_charge: float | None = None
    cost_currency: str | None = None
    for stat in meta.get("statistics") or []:
        stat_type = (stat.get("type") or "").upper()
        cost = stat.get("costInclTax") or {}
        amount_nzd = _cents_to_nzd(cost.get("estimatedAmount"))
        if amount_nzd is None:
            continue
        cost_currency = cost.get("costCurrency") or cost_currency
        if stat_type == "CONSUMPTION_COST":
            consumption_cost = amount_nzd
        elif stat_type == "STANDING_CHARGE_COST":
            standing_charge = amount_nzd

    return Measurement(
        value=float(value),
        unit=node.get("unit"),
        source=node.get("source"),
        read_at=_parse_dt(node.get("readAt")),
        start_at=_parse_dt(node.get("startAt")),
        end_at=_parse_dt(node.get("endAt")),
        direction=direction,
        quality=quality,
        frequency=frequency,
        consumption_cost_nzd=consumption_cost,
        standing_charge_nzd=standing_charge,
        cost_currency=cost_currency,
        property_id=property_id,
    )


def parse_accounts(raw_accounts: list[dict[str, Any]]) -> list[Account]:
    """Parse the ``viewer.accounts`` GraphQL payload."""
    return [Account.model_validate(item) for item in raw_accounts]


def flatten_property_measurements(
    properties: list[dict[str, Any]],
) -> list[Measurement]:
    """Flatten ``account.properties[].measurements.edges`` into Measurements."""
    out: list[Measurement] = []
    for prop in properties:
        prop_id = prop.get("id")
        edges = (prop.get("measurements") or {}).get("edges") or []
        for edge in edges:
            node = edge.get("node") or {}
            parsed = parse_measurement_node(node, property_id=prop_id)
            if parsed is not None:
                out.append(parsed)
    return out
