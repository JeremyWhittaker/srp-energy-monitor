"""Parse the CSV exports SRP produces from the usage page.

Kept free of any browser or database dependency: give it text, get back
:class:`DailyReading` objects. That makes the fiddly part of this project --
SRP's inconsistent columns -- testable against fixtures without credentials.

SRP exports one CSV per series, all with the same general shape::

    Meter read date,Usage date,Off-peak kWh,On-peak kWh,High temperature (F),Low temperature (F)
    7/1/2025,6/30/2025,"89","22",114,86

The demand series substitutes a single ``On-peak kW`` column for the two kWh
columns. Files are UTF-8 with a BOM, numeric cells are quoted, and some exports
append a "Combined total" summary row that is not a day and must be dropped.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from .models import DailyReading, IntervalReading, Series

# Accepted date formats, most likely first. SRP uses m/d/Y in every export
# observed so far, but it has shipped ISO dates in the past.
_DATE_FORMATS = ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y")

# Column headers vary in capitalisation and whitespace between exports, so
# lookups are normalised rather than exact.
_USAGE_DATE_KEYS = ("usage date", "date")
_METER_DATE_KEYS = ("meter read date", "read date")
_OFF_PEAK_KEYS = ("off-peak kwh", "off peak kwh", "offpeak kwh")
_ON_PEAK_KWH_KEYS = ("on-peak kwh", "on peak kwh", "onpeak kwh")
_ON_PEAK_KW_KEYS = ("on-peak kw", "on peak kw", "onpeak kw", "peak demand kw")
_HIGH_TEMP_KEYS = ("high temperature (f)", "high temperature", "high temp (f)", "high temp")
_LOW_TEMP_KEYS = ("low temperature (f)", "low temperature", "low temp (f)", "low temp")


class ParseError(ValueError):
    """Raised when a CSV does not look like an SRP export at all."""


def _normalise(row: dict[str, str | None]) -> dict[str, str]:
    """Lowercase and strip keys so header drift doesn't break lookups."""
    out: dict[str, str] = {}
    for key, value in row.items():
        if key is None:
            continue
        out[key.strip().lower().lstrip("﻿")] = (value or "").strip()
    return out


def _pick(row: dict[str, str], keys: Sequence[str]) -> str | None:
    for key in keys:
        if key in row and row[key] != "":
            return row[key]
    return None


def _to_float(raw: str | None) -> float | None:
    """Parse an SRP numeric cell.

    Cells arrive quoted (``"89"``), sometimes with thousands separators, a
    trailing unit, or a currency-style parenthesised negative.
    """
    if raw is None:
        return None
    text = raw.strip().strip('"').replace(",", "").replace("$", "")
    if not text or text in {"-", "--", "N/A", "n/a"}:
        return None
    # (1.5) is how some exports write -1.5
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    # Strip a trailing unit such as "2.4 kW".
    for unit in ("kwh", "kw", "f"):
        if text.lower().endswith(unit):
            text = text[: -len(unit)].strip()
            break
    try:
        return float(text)
    except ValueError:
        return None


def _to_date(raw: str | None) -> date | None:
    if not raw:
        return None
    text = raw.strip().strip('"')
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def infer_series(headers: Iterable[str]) -> Series | None:
    """Guess the series from the header row.

    Only the demand export is self-identifying (it has a ``kW`` column). The
    usage, generation and net exports are byte-identical in structure, so they
    cannot be told apart from headers alone -- callers must supply the series
    for those, which they always can, because they chose which button to click.
    """
    normalised = {h.strip().lower().lstrip("﻿") for h in headers if h}
    if any(key in normalised for key in _ON_PEAK_KW_KEYS):
        return Series.DEMAND
    return None


def parse_csv(text: str, series: Series | None = None) -> list[DailyReading]:
    """Parse an SRP CSV export into daily readings.

    Args:
        text: Raw CSV content. A UTF-8 BOM is tolerated.
        series: Which series the file holds. Required for usage/generation/net,
            which are structurally identical; inferred for demand.

    Raises:
        ParseError: if the file has no recognisable date column, or if the
            series is ambiguous and was not supplied.
    """
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    headers = reader.fieldnames or []
    if not headers:
        raise ParseError("CSV is empty")

    inferred = infer_series(headers)
    if series is None:
        if inferred is None:
            raise ParseError(
                "cannot infer series from headers; the usage, generation and net "
                "exports are structurally identical, so pass series= explicitly"
            )
        series = inferred
    elif inferred is Series.DEMAND and series is not Series.DEMAND:
        raise ParseError(
            f"file looks like a demand export (has a kW column) but was parsed as {series.value}"
        )

    readings: list[DailyReading] = []
    saw_date_column = False

    for raw_row in reader:
        row = _normalise(raw_row)
        if not row:
            continue
        # SRP appends a "Combined total" summary line to some exports.
        if any("combined total" in value.lower() for value in row.values()):
            continue

        usage_date = _to_date(_pick(row, _USAGE_DATE_KEYS))
        meter_date = _to_date(_pick(row, _METER_DATE_KEYS))
        if usage_date is None and meter_date is None:
            continue
        saw_date_column = True
        # Older exports carry only the meter read date.
        if usage_date is None:
            usage_date = meter_date

        if series is Series.DEMAND:
            off_peak = None
            on_peak = _to_float(_pick(row, _ON_PEAK_KW_KEYS))
        else:
            off_peak = _to_float(_pick(row, _OFF_PEAK_KEYS))
            on_peak = _to_float(_pick(row, _ON_PEAK_KWH_KEYS))

        readings.append(
            DailyReading(
                series=series,
                usage_date=usage_date,
                meter_read_date=meter_date,
                off_peak=off_peak,
                on_peak=on_peak,
                high_temp_f=_to_float(_pick(row, _HIGH_TEMP_KEYS)),
                low_temp_f=_to_float(_pick(row, _LOW_TEMP_KEYS)),
            )
        )

    if not saw_date_column:
        raise ParseError(f"no usable date column found; headers were {headers!r}")

    readings.sort(key=lambda r: r.usage_date)
    return readings


def parse_file(path: str | Path, series: Series | None = None) -> list[DailyReading]:
    """Parse an SRP CSV from disk, tolerating the BOM SRP always emits."""
    path = Path(path)
    if series is None:
        series = series_from_filename(path.name)
    return parse_csv(path.read_text(encoding="utf-8-sig"), series)


def series_from_filename(name: str) -> Series | None:
    """Recover the series from a ``srp_<series>_<timestamp>.csv`` filename."""
    stem = Path(name).stem.lower()
    if not stem.startswith("srp_"):
        return None
    for candidate in Series:
        if stem.startswith(f"srp_{candidate.value}_") or stem == f"srp_{candidate.value}":
            return candidate
    return None


def iter_downloads(directory: str | Path) -> Iterator[tuple[Path, Series]]:
    """Yield ``(path, series)`` for every recognisable SRP export in a folder.

    Useful for backfilling a database from a directory of previously
    downloaded CSVs.
    """
    for path in sorted(Path(directory).glob("srp_*.csv")):
        series = series_from_filename(path.name)
        if series is not None:
            yield path, series


# --------------------------------------------------------------------------
# Hourly / interval exports
# --------------------------------------------------------------------------

# The hourly export has its own shape:
#
#     Usage date,Interval,Off-peak kWh,On-peak kWh
#     8/25/2026,12:0 AM,"15.4","0"
#
# Note the ragged clock format -- SRP writes "12:0 AM", not "12:00 AM" -- so
# the minute field cannot be assumed two digits.
_INTERVAL_KEYS = ("interval", "time", "hour")

_TIME_FORMATS = ("%I:%M %p", "%I:%M%p", "%H:%M", "%I %p")


def _to_time(raw: str | None):
    """Parse an SRP interval label into a time.

    Accepts the zero-padded and unpadded variants SRP mixes ("12:0 AM",
    "12:00 AM", "13:00").
    """
    from datetime import time as _time

    if not raw:
        return None
    text = raw.strip().strip('"').upper().replace(".", "")
    text = " ".join(text.split())
    # Normalise "12:0 AM" -> "12:00 AM" so %M matches.
    if ":" in text:
        head, _, tail = text.partition(":")
        minute, _, suffix = tail.partition(" ")
        if minute.isdigit():
            text = f"{head}:{int(minute):02d}" + (f" {suffix}" if suffix else "")
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    return None


def parse_hourly_csv(text: str, series: Series) -> list[IntervalReading]:
    """Parse an SRP Hourly/Interval export into interval readings.

    The hourly export carries no marker of which chart produced it, so the
    caller must say. That is always knowable, because the caller chose which
    chart to select before exporting.

    Raises:
        ParseError: if the file has no recognisable date or interval column.
    """
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    headers = reader.fieldnames or []
    if not headers:
        raise ParseError("CSV is empty")

    readings: list[IntervalReading] = []
    saw_interval = False

    for raw_row in reader:
        row = _normalise(raw_row)
        if not row:
            continue
        if any("combined total" in value.lower() for value in row.values()):
            continue

        day = _to_date(_pick(row, _USAGE_DATE_KEYS) or _pick(row, _METER_DATE_KEYS))
        clock = _to_time(_pick(row, _INTERVAL_KEYS))
        if day is None or clock is None:
            continue
        saw_interval = True

        readings.append(
            IntervalReading(
                series=series,
                interval_start=datetime.combine(day, clock),
                off_peak=_to_float(_pick(row, _OFF_PEAK_KEYS)),
                on_peak=_to_float(_pick(row, _ON_PEAK_KWH_KEYS)),
            )
        )

    if not saw_interval:
        raise ParseError(
            f"no usable date+interval columns found; headers were {headers!r}"
        )

    readings.sort(key=lambda r: r.interval_start)
    return readings


def parse_hourly_file(path: str | Path, series: Series | None = None) -> list[IntervalReading]:
    """Parse an hourly export, recovering the series from the filename if needed."""
    path = Path(path)
    if series is None:
        series = series_from_hourly_filename(path.name)
    if series is None:
        raise ParseError(
            f"cannot tell which chart {path.name} came from; pass series= explicitly"
        )
    return parse_hourly_csv(path.read_text(encoding="utf-8-sig"), series)


def series_from_hourly_filename(name: str) -> Series | None:
    """Recover the series from ``srp_hourly_<series>_<start>_<end>.csv``."""
    stem = Path(name).stem.lower()
    if not stem.startswith("srp_hourly_"):
        return None
    rest = stem[len("srp_hourly_"):]
    for candidate in Series:
        if rest.startswith(f"{candidate.value}_"):
            return candidate
    return None
