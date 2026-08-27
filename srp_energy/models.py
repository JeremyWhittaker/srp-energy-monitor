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
class DemandObservation:
    """The headline demand figure SRP shows for the current bill cycle.

    A live scrape of one number, not a CSV row. Its *meaning depends on the
    account's price plan*, so the on-page caption is captured alongside the
    value rather than assumed:

    * On an average-demand plan (e.g. "Customer Generation Avg Demand Plan")
      the card reads "Current Average On-Peak Demand" and the number is the
      mean on-peak demand across the cycle so far.
    * On a peak-demand plan (E-27 / E-28) it is the highest on-peak demand
      recorded in the cycle.

    Treating one as the other silently misreports the number that drives the
    demand charge, so ``label`` is part of the data, not decoration.
    """

    observed_at: datetime
    demand_kw: float
    label: str | None = None
    source: str | None = None

    @property
    def is_average(self) -> bool:
        """True when SRP captions this figure as an average rather than a peak."""
        return bool(self.label and "average" in self.label.lower())

    def to_dict(self) -> dict[str, Any]:
        return {
            "observed_at": self.observed_at.isoformat(),
            "demand_kw": self.demand_kw,
            "unit": "kW",
            "label": self.label,
            "is_average": self.is_average,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class IntervalReading:
    """One hourly interval, from SRP's Hourly/Interval export.

    SRP reports intervals as a local wall-clock time with no zone or offset, so
    ``interval_start`` is naive and means Arizona local time. Arizona does not
    observe daylight saving, which is the only reason a naive timestamp is safe
    here -- there are no repeated or missing hours to disambiguate.
    """

    series: Series
    interval_start: datetime
    off_peak: float | None = None
    on_peak: float | None = None

    @property
    def usage_date(self) -> date:
        return self.interval_start.date()

    @property
    def total(self) -> float | None:
        if self.off_peak is None and self.on_peak is None:
            return None
        return (self.off_peak or 0.0) + (self.on_peak or 0.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "series": self.series.value,
            "interval_start": self.interval_start.isoformat(),
            "usage_date": self.usage_date.isoformat(),
            "hour": self.interval_start.hour,
            "off_peak": self.off_peak,
            "on_peak": self.on_peak,
            "total": self.total,
            "unit": self.series.unit,
        }


@dataclass(frozen=True, slots=True)
class BillProjection:
    """SRP's projected bill amount for the cycle in progress.

    The portal shows this as a bare dollar figure captioned "projected bill
    amount", alongside a sentence naming the month projected and the usage date
    the projection runs through. That sentence is captured as ``basis`` because
    the number alone is undated -- a projection "through 8/25" means something
    different on the 26th than on the 3rd of the next month.
    """

    observed_at: datetime
    amount_usd: float
    basis: str | None = None
    source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "observed_at": self.observed_at.isoformat(),
            "amount_usd": self.amount_usd,
            "currency": "USD",
            "basis": self.basis,
            "source": self.source,
        }
