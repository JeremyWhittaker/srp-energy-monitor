"""Hourly / interval export tests.

The fixture reproduces SRP's real hourly format exactly -- BOM, CRLF header,
quoted numerics, and the ragged ``12:0 AM`` clock SRP actually emits -- with
invented numbers.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from srp_energy.models import IntervalReading, Series
from srp_energy.parser import (
    ParseError,
    parse_hourly_csv,
    parse_hourly_file,
    series_from_hourly_filename,
)
from srp_energy.storage import Store

FIXTURE = Path(__file__).parent / "fixtures" / "srp_hourly_usage_20260824_20260824.csv"


def test_parses_a_full_day_of_intervals():
    readings = parse_hourly_file(FIXTURE, Series.USAGE)

    assert len(readings) == 24
    assert readings[0].interval_start == datetime(2026, 8, 24, 0, 0)
    assert readings[-1].interval_start == datetime(2026, 8, 24, 23, 0)
    assert all(r.series is Series.USAGE for r in readings)


def test_handles_srps_ragged_clock_format():
    """SRP writes "12:0 AM", not "12:00 AM"."""
    text = (
        "Usage date,Interval,Off-peak kWh,On-peak kWh\n"
        '8/24/2026,12:0 AM,"1","0"\n'
        '8/24/2026,1:0 PM,"2","0"\n'
        '8/24/2026,11:00 PM,"3","0"\n'
    )
    readings = parse_hourly_csv(text, Series.USAGE)

    assert [r.interval_start.hour for r in readings] == [0, 13, 23]


def test_am_pm_boundaries_are_not_off_by_twelve():
    text = (
        "Usage date,Interval,Off-peak kWh,On-peak kWh\n"
        '8/24/2026,12:0 AM,"1","0"\n'   # midnight
        '8/24/2026,12:0 PM,"2","0"\n'   # noon
    )
    readings = parse_hourly_csv(text, Series.USAGE)

    assert [r.interval_start.hour for r in readings] == [0, 12]


def test_intervals_are_sorted_chronologically():
    text = (
        "Usage date,Interval,Off-peak kWh,On-peak kWh\n"
        '8/24/2026,5:0 PM,"1","0"\n'
        '8/24/2026,2:0 AM,"2","0"\n'
    )
    assert [r.interval_start.hour for r in parse_hourly_csv(text, Series.USAGE)] == [2, 17]


def test_on_peak_window_lands_in_the_afternoon():
    """Sanity check the fixture models SRP's 2-8pm summer on-peak."""
    readings = parse_hourly_file(FIXTURE, Series.USAGE)
    on_peak_hours = [r.interval_start.hour for r in readings if (r.on_peak or 0) > 0]

    assert on_peak_hours == [14, 15, 16, 17, 18, 19]


def test_rejects_a_file_with_no_interval_column():
    text = "Usage date,Off-peak kWh\n8/24/2026,\"1\"\n"

    with pytest.raises(ParseError, match="date\\+interval"):
        parse_hourly_csv(text, Series.USAGE)


def test_rejects_empty_file():
    with pytest.raises(ParseError, match="empty"):
        parse_hourly_csv("", Series.USAGE)


@pytest.mark.parametrize(
    "name,expected",
    [
        ("srp_hourly_usage_20260824_20260825.csv", Series.USAGE),
        ("srp_hourly_net_20260824_20260825.csv", Series.NET),
        ("srp_hourly_generation_20260824_20260825.csv", Series.GENERATION),
        ("srp_hourly_demand_20260824_20260825.csv", Series.DEMAND),
        ("srp_usage_20260824_120000.csv", None),   # a daily export, not hourly
        ("random.csv", None),
    ],
)
def test_series_from_hourly_filename(name, expected):
    assert series_from_hourly_filename(name) is expected


def test_parse_hourly_file_refuses_to_guess_the_series():
    """The export carries no marker of which chart produced it."""
    with pytest.raises(ParseError, match="cannot tell which chart"):
        parse_hourly_file(Path("srp_hourly_unknownchart_1_2.csv"))


# ------------------------------------------------------------------ storage


@pytest.fixture()
def store(tmp_path):
    return Store(tmp_path / "intervals.db")


def _day(series: Series, hours: int = 24) -> list[IntervalReading]:
    return [
        IntervalReading(
            series=series,
            interval_start=datetime(2026, 8, 24, h),
            off_peak=float(h),
            on_peak=0.0,
        )
        for h in range(hours)
    ]


def test_intervals_round_trip(store):
    assert store.upsert_intervals(_day(Series.USAGE)) == 24

    rows = store.intervals()
    assert len(rows) == 24
    assert rows[0].interval_start.hour == 0
    assert rows[-1].interval_start.hour == 23


def test_reimporting_the_same_hours_updates_rather_than_duplicates(store):
    store.upsert_intervals(_day(Series.USAGE))
    store.upsert_intervals(_day(Series.USAGE))

    assert len(store.intervals()) == 24


def test_the_same_hour_in_two_series_are_separate_rows(store):
    """Usage and net report different numbers for the same hour."""
    store.upsert_intervals(_day(Series.USAGE))
    store.upsert_intervals(_day(Series.NET))

    assert len(store.intervals()) == 48
    assert len(store.intervals(series=Series.USAGE)) == 24


def test_intervals_filter_by_date_covers_the_whole_day(store):
    store.upsert_intervals(_day(Series.USAGE))

    same_day = store.intervals(start=date(2026, 8, 24), end=date(2026, 8, 24))
    assert len(same_day) == 24, "an end date must include that day's 23:00 interval"

    assert store.intervals(start=date(2026, 8, 25)) == []


def test_interval_limit_keeps_the_most_recent(store):
    store.upsert_intervals(_day(Series.USAGE))

    rows = store.intervals(limit=3)

    assert [r.interval_start.hour for r in rows] == [21, 22, 23]


def test_summary_reports_interval_coverage(store):
    store.upsert_intervals(_day(Series.USAGE))

    intervals = store.summary()["intervals"]
    assert intervals["count"] == 24
    assert intervals["first"] == "2026-08-24T00:00"
    assert intervals["last"] == "2026-08-24T23:00"


def test_interval_unit_follows_the_series(store):
    usage = IntervalReading(series=Series.USAGE, interval_start=datetime(2026, 8, 24, 1))
    demand = IntervalReading(series=Series.DEMAND, interval_start=datetime(2026, 8, 24, 1))

    assert usage.to_dict()["unit"] == "kWh"
    assert demand.to_dict()["unit"] == "kW"


# ----------------------------------------------------- demand intervals (kW)

DEMAND_FIXTURE = (
    Path(__file__).parent / "fixtures" / "srp_hourly_demand_20260825_20260825.csv"
)


def test_demand_intervals_parse_their_kw_columns():
    """Regression: the demand export is headed kW, not kWh.

    Matching only the kWh spellings parsed every row to None, so 48 half-hours
    of real data were stored blank without any error.
    """
    readings = parse_hourly_file(DEMAND_FIXTURE, Series.DEMAND)

    assert len(readings) == 48
    assert all(
        r.off_peak is not None or r.on_peak is not None for r in readings
    ), "no row may come back with both values empty"
    assert readings[0].off_peak == 5.0
    assert readings[0].to_dict()["unit"] == "kW"


def test_demand_intervals_are_half_hourly():
    """SRP reports demand every 30 minutes, which is how demand charges work."""
    readings = parse_hourly_file(DEMAND_FIXTURE, Series.DEMAND)

    minutes = {r.interval_start.minute for r in readings}
    assert minutes == {0, 30}
    assert readings[1].interval_start == datetime(2026, 8, 25, 0, 30)


def test_a_row_with_no_recognisable_value_column_fails_loudly():
    """Silently storing blanks is what made the kW bug invisible."""
    text = (
        "Usage date,Interval,Some Other Column\n"
        '8/25/2026,12:0 AM,"5"\n'
    )
    with pytest.raises(ParseError, match="no value in"):
        parse_hourly_csv(text, Series.USAGE)


def test_half_hour_intervals_do_not_collide_in_storage(store):
    store.upsert_intervals(parse_hourly_file(DEMAND_FIXTURE, Series.DEMAND))

    assert len(store.intervals(series=Series.DEMAND)) == 48
