"""Storage tests. The important property is idempotence: SRP restates recent
days as meter data settles, so re-running a fetch must correct history rather
than duplicate it.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from srp_energy.models import (
    CyclePeak,
    DailyReading,
    DemandObservation,
    IntervalReading,
    Series,
)
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
        store.record_demand(DemandObservation(observed_at=now + timedelta(hours=i), demand_kw=kw))

    latest = store.latest_demand()
    assert latest is not None and latest.demand_kw == 5.3
    assert [o.demand_kw for o in store.demand_history(limit=2)] == [5.3, 4.2]


def test_latest_peak_demand_is_none_on_empty_database(store):
    assert store.latest_demand() is None
    assert store.demand_history() == []


def test_summary_reports_coverage(store):
    store.upsert_readings([reading(1), reading(5)])
    store.record_demand(DemandObservation(observed_at=datetime(2025, 7, 1), demand_kw=2.0))

    summary = store.summary()
    assert summary["series"]["usage"]["days"] == 2
    assert summary["series"]["usage"]["first_date"] == "2025-07-01"
    assert summary["series"]["usage"]["last_date"] == "2025-07-05"
    assert summary["demand_observations"] == 1
    assert summary["latest_demand"]["demand_kw"] == 2.0


def test_reopening_an_existing_database_preserves_data(tmp_path):
    path = tmp_path / "persist.db"
    Store(path).upsert_readings([reading(1)])

    assert len(Store(path).readings()) == 1, "re-init must not wipe the schema"


def test_v1_database_migrates_without_inventing_a_label(tmp_path):
    """A v1 row's meaning is unknown, so it must not be relabelled by guess."""
    import sqlite3

    path = tmp_path / "v1.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE peak_demand_observation (
            observed_at TEXT PRIMARY KEY,
            demand_kw   REAL NOT NULL,
            source      TEXT
        );
        INSERT INTO peak_demand_observation VALUES
            ('2026-08-26T16:12:47', 5.4, '.srp-red-text strong');
        """
    )
    conn.commit()
    conn.close()

    store = Store(path)

    latest = store.latest_demand()
    assert latest is not None
    assert latest.demand_kw == 5.4
    assert latest.source == ".srp-red-text strong"
    assert latest.label is None, "an unknown v1 value must not be asserted as a peak"
    assert latest.is_average is False

    with store.connect() as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "peak_demand_observation" not in tables, "old table should be dropped"


def test_null_refetch_does_not_erase_stored_values(tmp_path):
    """A blank re-fetch must never destroy history.

    SRP restates recent days, so a *new* value has to win. But a parse that
    yields blanks -- SRP renaming a column is the realistic cause -- used to
    overwrite a month of good readings with NULLs on the next scheduled fetch.
    """
    store = Store(tmp_path / "coalesce.db")
    store.upsert_readings(
        [
            DailyReading(
                series=Series.USAGE,
                usage_date=date(2026, 8, 25),
                off_peak=54.95,
                on_peak=14.17,
                high_temp_f=104.0,
                low_temp_f=79.0,
            )
        ]
    )

    store.upsert_readings([DailyReading(series=Series.USAGE, usage_date=date(2026, 8, 25))])

    kept = store.readings(series=Series.USAGE)[0]
    assert kept.off_peak == 54.95
    assert kept.on_peak == 14.17
    assert kept.high_temp_f == 104.0

    # A real restatement still wins.
    store.upsert_readings(
        [
            DailyReading(
                series=Series.USAGE,
                usage_date=date(2026, 8, 25),
                off_peak=55.10,
                on_peak=14.20,
            )
        ]
    )
    restated = store.readings(series=Series.USAGE)[0]
    assert restated.off_peak == 55.10
    assert restated.high_temp_f == 104.0, "untouched columns survive a partial restatement"


def test_null_refetch_does_not_erase_stored_intervals(tmp_path):
    store = Store(tmp_path / "coalesce-intervals.db")
    store.upsert_intervals(
        [
            IntervalReading(
                series=Series.USAGE,
                interval_start=datetime(2026, 8, 25, 13),
                off_peak=4.8,
            )
        ]
    )
    store.upsert_intervals(
        [IntervalReading(series=Series.USAGE, interval_start=datetime(2026, 8, 25, 13))]
    )

    assert store.intervals(series=Series.USAGE)[0].off_peak == 4.8


def peak(start_day: int, kw: float, **kwargs) -> CyclePeak:
    return CyclePeak(
        cycle_start=date(2026, start_day // 100, start_day % 100),
        cycle_end=date(2026, start_day // 100, start_day % 100) + timedelta(days=29),
        on_peak_high_demand_kw=kw,
        **kwargs,
    )


def test_cycle_peak_round_trips(store):
    store.upsert_cycle_peaks(
        [peak(610, 6.9, occurred_at=datetime(2026, 6, 23, 19, 30), is_average_demand_rate=True)]
    )
    stored = store.latest_cycle_peak()

    assert stored.on_peak_high_demand_kw == 6.9
    assert stored.occurred_at == datetime(2026, 6, 23, 19, 30)
    assert stored.is_average_demand_rate is True


def test_open_cycle_peak_is_corrected_not_duplicated(store):
    """The current cycle's peak climbs as the cycle runs.

    Re-fetching mid-cycle must overwrite that row. Appending instead would
    leave the highest figure buried behind whichever row was written last.
    """
    store.upsert_cycle_peaks([peak(810, 8.7, occurred_at=datetime(2026, 8, 17, 19, 30))])
    store.upsert_cycle_peaks([peak(810, 12.7, occurred_at=datetime(2026, 8, 18, 19, 30))])

    peaks = store.cycle_peaks()
    assert len(peaks) == 1, "one row per billing cycle"
    assert peaks[0].on_peak_high_demand_kw == 12.7
    assert peaks[0].occurred_at == datetime(2026, 8, 18, 19, 30)


def test_cycle_peaks_come_back_newest_first(store):
    store.upsert_cycle_peaks([peak(610, 6.9), peak(810, 12.7), peak(710, 12.6)])

    assert [p.on_peak_high_demand_kw for p in store.cycle_peaks()] == [12.7, 12.6, 6.9]


def test_cycle_peak_survives_a_reopened_database(tmp_path):
    """Schema v5 adds a table to an existing database without disturbing it."""
    path = tmp_path / "upgrade.db"
    first = Store(path)
    first.upsert_readings([reading(1)])
    first.upsert_cycle_peaks([peak(810, 12.7)])

    reopened = Store(path)
    assert reopened.latest_cycle_peak().on_peak_high_demand_kw == 12.7
    assert len(reopened.readings()) == 1


def test_no_peaks_is_not_an_error(store):
    assert store.upsert_cycle_peaks([]) == 0
    assert store.cycle_peaks() == []
    assert store.latest_cycle_peak() is None
