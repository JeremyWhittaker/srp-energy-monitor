"""Core data types for SRP energy data.

These are deliberately plain dataclasses with no database or browser
dependencies, so they can be constructed and asserted against in tests
without a network or a Playwright install.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date, datetime
from enum import Enum
from typing import Any


class Series(str, Enum):
    """The four data series SRP exposes on the usage page.

    The value is our canonical short name (used in the database, the API and
    filenames). ``label`` is the button text on the SRP usage page, which is
    what the scraper has to click.
    """

    USAGE = "usage"
    GENERATION = "generation"
    NET = "net"
    DEMAND = "demand"

    @property
    def label(self) -> str:
        return {
            Series.USAGE: "Usage",
            Series.GENERATION: "Generation",
            Series.NET: "Net energy",
            Series.DEMAND: "Demand",
        }[self]

    @property
    def unit(self) -> str:
        return "kW" if self is Series.DEMAND else "kWh"

    @classmethod
    def parse(cls, value: str) -> "Series":
        try:
            return cls(value.strip().lower())
        except ValueError as exc:
            valid = ", ".join(s.value for s in cls)
            raise ValueError(f"unknown series {value!r} (expected one of: {valid})") from exc


@dataclass(frozen=True, slots=True)
class DailyReading:
    """One day of one series, as SRP reports it.

    SRP's export gives every series the same shape: a meter read date, the
    usage date the numbers actually describe, one or two values, and the day's
    high/low temperature. Energy series populate ``off_peak`` / ``on_peak``
    (kWh); the demand series populates ``on_peak`` only (kW). Keeping one table
    for all four means callers can query them uniformly.
    """

    series: Series
    usage_date: date
    meter_read_date: date | None = None
    off_peak: float | None = None
    on_peak: float | None = None
    high_temp_f: float | None = None
    low_temp_f: float | None = None

    @property
    def total(self) -> float | None:
        """Off-peak + on-peak, when both are present.

        Meaningless for the demand series (peak demand is a maximum, not a
        sum), so it returns None there.
        """
        if self.series is Series.DEMAND:
            return None
        if self.off_peak is None and self.on_peak is None:
            return None
        return (self.off_peak or 0.0) + (self.on_peak or 0.0)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["series"] = self.series.value
        d["usage_date"] = self.usage_date.isoformat()
        d["meter_read_date"] = self.meter_read_date.isoformat() if self.meter_read_date else None
        d["unit"] = self.series.unit
        d["total"] = self.total
        return d


@dataclass(frozen=True, slots=True)
class PeakDemandObservation:
    """The billing-cycle peak demand figure shown on the usage page.

    This is a live scrape of a single number, not a CSV row: SRP shows the
    highest on-peak demand recorded so far in the current billing cycle. It is
    the value most worth alerting on, because it sets the demand charge for the
    whole month.
    """

    observed_at: datetime
    demand_kw: float
    source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "observed_at": self.observed_at.isoformat(),
            "demand_kw": self.demand_kw,
            "unit": "kW",
            "source": self.source,
        }
