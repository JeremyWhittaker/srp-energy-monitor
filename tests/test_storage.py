"""Storage tests. The important property is idempotence: SRP restates recent
days as meter data settles, so re-running a fetch must correct history rather
than duplicate it.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from srp_energy.models import DailyReading, PeakDemandObservation, Series
from srp_energy.storage import Store


@pytest.fixture()
def store(tmp_path):
    return Store(tmp_path / "test.db")


def reading(day: int, series: Series = Series.USAGE, **kwargs) -> DailyReading:
    return DailyReading(
        series=series,
        usage_date=date(2025, 7, day),
        off_peak=kwargs.pop("off_peak", 10.0),
        on_peak=kwargs.pop("on_peak", 5.0),
        **kwargs,
    )


def test_upsert_then_read_back(store):
    assert store.upsert_readings([reading(1), reading(2)]) == 2

    rows = store.readings()
    assert len(rows) == 2
    assert rows[0].usage_date == date(2025, 7, 1)
    assert rows[0].series is Series.USAGE


def test_reimporting_the_same_day_updates_instead_of_duplicating(store):
    store.upsert_readings([reading(1, off_peak=10.0)])
    store.upsert_readings([reading(1, off_peak=12.5)])

    rows = store.readings()
    assert len(rows) == 1, "same (series, date) must collapse to one row"
    assert rows[0].off_peak == 12.5


def test_same_date_in_different_series_are_separate_rows(store):
    store.upsert_readings([reading(1, Series.USAGE), reading(1, Series.NET)])
    assert len(store.readings()) == 2


def test_filters_by_series_and_date_range(store):
    store.upsert_readings(
        [reading(d) for d in (1, 2, 3)] + [reading(2, Series.DEMAND, on_peak=4.0)]
    )

    assert len(store.readings(series=Series.DEMAND)) == 1
    assert len(store.readings(series=[Series.USAGE, Series.DEMAND])) == 4
    assert len(store.readings(start=date(2025, 7, 2))) == 3
    assert len(store.readings(start=date(2025, 7, 2), end=date(2025, 7, 2))) == 2


def test_limit_keeps_the_most_recent_readings_in_chronological_order(store):
    """Asking for N must mean the newest N, not the oldest N."""
    store.upsert_readings([reading(d) for d in (1, 2, 3, 4, 5)])

    rows = store.readings(limit=2)

    assert [r.usage_date for r in rows] == [date(2025, 7, 4), date(2025, 7, 5)]


def test_upserting_nothing_is_a_no_op(store):
    assert store.upsert_readings([]) == 0
    assert store.readings() == []


def test_peak_demand_latest_and_history(store):
    now = datetime(2025, 7, 1, 12, 0, 0)
    for i, kw in enumerate([3.1, 4.2, 5.3]):
        store.record_peak_demand(
            PeakDemandObservation(observed_at=now + timedelta(hours=i), demand_kw=kw)
        )

    latest = store.latest_peak_demand()
    assert latest is not None and latest.demand_kw == 5.3
    assert [o.demand_kw for o in store.peak_demand_history(limit=2)] == [5.3, 4.2]


def test_latest_peak_demand_is_none_on_empty_database(store):
    assert store.latest_peak_demand() is None
    assert store.peak_demand_history() == []


def test_summary_reports_coverage(store):
    store.upsert_readings([reading(1), reading(5)])
    store.record_peak_demand(
        PeakDemandObservation(observed_at=datetime(2025, 7, 1), demand_kw=2.0)
    )

    summary = store.summary()
    assert summary["series"]["usage"]["days"] == 2
    assert summary["series"]["usage"]["first_date"] == "2025-07-01"
    assert summary["series"]["usage"]["last_date"] == "2025-07-05"
    assert summary["peak_demand_observations"] == 1
    assert summary["latest_peak_demand"]["demand_kw"] == 2.0


def test_reopening_an_existing_database_preserves_data(tmp_path):
    path = tmp_path / "persist.db"
    Store(path).upsert_readings([reading(1)])

    assert len(Store(path).readings()) == 1, "re-init must not wipe the schema"
