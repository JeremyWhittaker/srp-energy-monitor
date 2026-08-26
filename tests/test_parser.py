"""Parser tests against fixtures that mirror SRP's real export format.

The fixture numbers are invented, but the *shape* is copied exactly from real
SRP exports: a UTF-8 BOM, CRLF on the header line, quoted numeric cells,
m/d/Y dates, and a trailing "Combined total" row on some files.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from srp_energy.models import Series
from srp_energy.parser import (
    ParseError,
    infer_series,
    parse_csv,
    parse_file,
    series_from_filename,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_parses_usage_export():
    readings = parse_file(FIXTURES / "srp_usage_20250731_120840.csv", Series.USAGE)

    assert len(readings) == 4
    first = readings[0]
    assert first.series is Series.USAGE
    assert first.usage_date == date(2025, 6, 30)
    assert first.meter_read_date == date(2025, 7, 1)
    assert first.off_peak == 89.0
    assert first.on_peak == 22.0
    assert first.high_temp_f == 114.0
    assert first.low_temp_f == 86.0
    assert first.total == 111.0


def test_parses_demand_export_into_on_peak_kw():
    """Demand has one value column and it belongs in on_peak, measured in kW."""
    readings = parse_file(FIXTURES / "srp_demand_20250731_120846.csv", Series.DEMAND)

    assert [r.on_peak for r in readings] == [0.1, 0.2, 0.0, 2.4]
    assert all(r.off_peak is None for r in readings)
    assert readings[0].series.unit == "kW"
    # A peak is a maximum, not a sum, so total is meaningless here.
    assert readings[0].total is None


def test_net_export_keeps_negative_on_peak_and_drops_summary_row():
    """Net energy goes negative when solar exports exceed consumption."""
    readings = parse_file(FIXTURES / "srp_net_20250731_120830.csv", Series.NET)

    assert len(readings) == 3, "the 'Combined total' row must not become a reading"
    assert [r.on_peak for r in readings] == [-14.0, -19.0, -16.0]
    assert readings[0].off_peak == 73.0


def test_parses_generation_export():
    readings = parse_file(FIXTURES / "srp_generation_20250731_120835.csv", Series.GENERATION)

    assert len(readings) == 3
    assert readings[-1].off_peak == 5.0
    assert readings[-1].on_peak == 42.0


def test_readings_come_back_sorted_by_date():
    text = (
        "Meter read date,Usage date,Off-peak kWh,On-peak kWh\n"
        '7/4/2025,7/3/2025,"1","2"\n'
        '7/2/2025,7/1/2025,"3","4"\n'
    )
    readings = parse_csv(text, Series.USAGE)

    assert [r.usage_date for r in readings] == [date(2025, 7, 1), date(2025, 7, 3)]


def test_demand_series_is_inferred_from_headers():
    headers = ["Meter read date", "Usage date", "On-peak kW"]
    assert infer_series(headers) is Series.DEMAND


def test_energy_series_cannot_be_inferred():
    """Usage, generation and net are structurally identical -- by design."""
    headers = ["Meter read date", "Usage date", "Off-peak kWh", "On-peak kWh"]
    assert infer_series(headers) is None

    with pytest.raises(ParseError, match="cannot infer series"):
        parse_csv("Meter read date,Usage date,Off-peak kWh,On-peak kWh\n", None)


def test_mislabelled_demand_file_is_rejected():
    """Parsing a kW export as an energy series would silently corrupt units."""
    text = 'Meter read date,Usage date,On-peak kW\n7/1/2025,6/30/2025,"1.5"\n'

    with pytest.raises(ParseError, match="looks like a demand export"):
        parse_csv(text, Series.USAGE)


def test_rejects_a_file_with_no_date_column():
    with pytest.raises(ParseError, match="no usable date column"):
        parse_csv("Something,Else\n1,2\n", Series.USAGE)


def test_rejects_empty_file():
    with pytest.raises(ParseError, match="empty"):
        parse_csv("", Series.USAGE)


@pytest.mark.parametrize(
    "cell,expected",
    [
        ('"1,234"', 1234.0),      # thousands separator
        ('"2.4 kW"', 2.4),        # trailing unit
        ('"(1.5)"', -1.5),        # parenthesised negative
        ('"-19"', -19.0),
        ('""', None),
        ('"N/A"', None),
        ('"--"', None),
    ],
)
def test_numeric_cell_variants(cell, expected):
    text = f"Meter read date,Usage date,Off-peak kWh\n7/1/2025,6/30/2025,{cell}\n"
    assert parse_csv(text, Series.USAGE)[0].off_peak == expected


@pytest.mark.parametrize("fmt,raw", [("m/d/Y", "7/1/2025"), ("ISO", "2025-07-01"), ("m/d/y", "7/1/25")])
def test_date_format_variants(fmt, raw):
    text = f"Usage date,Off-peak kWh\n{raw},\"5\"\n"
    assert parse_csv(text, Series.USAGE)[0].usage_date == date(2025, 7, 1)


def test_falls_back_to_meter_read_date_when_usage_date_missing():
    text = 'Meter read date,Off-peak kWh\n7/1/2025,"5"\n'
    assert parse_csv(text, Series.USAGE)[0].usage_date == date(2025, 7, 1)


def test_tolerates_header_whitespace_and_case():
    text = 'Usage Date , OFF-PEAK KWH \n7/1/2025,"5"\n'
    assert parse_csv(text, Series.USAGE)[0].off_peak == 5.0


@pytest.mark.parametrize(
    "name,expected",
    [
        ("srp_usage_20250731_120840.csv", Series.USAGE),
        ("srp_demand_20250731_120846.csv", Series.DEMAND),
        ("srp_generation_20250731_120835.csv", Series.GENERATION),
        ("srp_net_20250731_120830.csv", Series.NET),
        ("something_else.csv", None),
        ("srp_unknown_20250731_120830.csv", None),
    ],
)
def test_series_from_filename(name, expected):
    assert series_from_filename(name) is expected


def test_parse_file_infers_series_from_filename():
    """`parse_file` with no series should use the filename convention."""
    readings = parse_file(FIXTURES / "srp_generation_20250731_120835.csv")
    assert readings[0].series is Series.GENERATION
