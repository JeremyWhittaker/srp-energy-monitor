"""The bundled Home Assistant component must not drift from the library.

``custom_components/srp_energy_monitor/srp_api.py`` duplicates the parsing
rules in ``srp_energy.jsonapi``. That duplication is forced: the component
cannot depend on this package (it is not on PyPI, and HAOS runs on musl where
an unavailable wheel puts the config entry into SETUP_RETRY), and it must use
the aiohttp Home Assistant already ships rather than a synchronous transport.

Duplication that nothing checks becomes divergence, and divergence here means
two SRP numbers that disagree with no way to tell which is right. So every rule
that exists in both places is asserted equal against the same fixtures. Change
one, this fails until you change the other.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("aiohttp", reason="the component uses Home Assistant's aiohttp")

from srp_energy import Series, jsonapi

FIXTURES = Path(__file__).parent / "fixtures"
COMPONENT = Path(__file__).parent.parent / "custom_components" / "srp_energy_monitor"


@pytest.fixture(scope="module")
def srp_api():
    """Import the component's module without needing Home Assistant installed."""
    sys.path.insert(0, str(COMPONENT))
    try:
        import srp_api

        return srp_api
    finally:
        sys.path.remove(str(COMPONENT))


@pytest.fixture
def daily():
    return json.loads((FIXTURES / "srp_dailyusagedetail.json").read_text())


@pytest.fixture
def hourly():
    return json.loads((FIXTURES / "srp_hourlydetail.json").read_text())


def test_series_mapping_is_identical(srp_api):
    """The mapping that decides which SRP list is which series."""
    assert {
        series.value: keys for series, keys in jsonapi._SERIES_KEYS.items()
    } == srp_api.SERIES_KEYS


def test_hourly_window_limit_is_identical(srp_api):
    assert srp_api.HOURLY_MAX_DAYS == jsonapi.HOURLY_MAX_DAYS


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (date(2024, 8, 1), date(2026, 8, 26)),
        (date(2026, 8, 1), date(2026, 8, 10)),
        (date(2026, 8, 1), date(2026, 8, 1)),
    ],
)
def test_chunking_agrees(srp_api, start, end):
    assert srp_api.chunk_date_range(start, end) == jsonapi.chunk_date_range(start, end)


def test_interval_values_agree(srp_api, hourly):
    theirs = {(i.series, i.start, i.off_peak, i.on_peak) for i in srp_api.parse_intervals(hourly)}
    ours = {
        (r.series.value, r.interval_start, r.off_peak, r.on_peak)
        for r in jsonapi.parse_hourly_detail(hourly)
    }
    assert theirs == ours


def test_daily_values_agree(srp_api, daily):
    theirs = {(d.series, d.day, d.off_peak, d.on_peak) for d in srp_api.parse_daily(daily)}
    ours = {
        (r.series.value, r.usage_date, r.off_peak, r.on_peak)
        for r in jsonapi.parse_daily_usage_detail(daily)
    }
    assert theirs == ours


def test_cycle_peaks_agree(srp_api, daily):
    theirs = [
        (p.cycle_start, p.cycle_end, p.on_peak_high_demand_kw, p.occurred_at)
        for p in srp_api.parse_cycle_peaks(daily)
    ]
    ours = [
        (p.cycle_start, p.cycle_end, p.on_peak_high_demand_kw, p.occurred_at)
        for p in jsonapi.parse_cycle_peaks(daily)
    ]
    assert theirs == ours


@pytest.mark.parametrize("is_average", [True, False])
def test_current_demand_agrees_on_both_plan_types(srp_api, daily, is_average):
    """The average-vs-maximum rule is the one that must never diverge."""
    payload = dict(daily, isAverageDemandRate=is_average)

    value, flagged_average = srp_api.current_demand(payload)
    observation = jsonapi.parse_demand_observation(payload)

    assert value == pytest.approx(observation.demand_kw)
    assert flagged_average is observation.is_average is is_average


def _demand_payload(rows, *, is_average=True):
    """A synthetic cycle with portal-shaped daily maxima and no account data."""
    days = [day for day, _ in rows]
    return {
        "isAverageDemandRate": is_average,
        "highDemandInfoList": [
            {
                "billStartDate": min(days).isoformat(),
                "billEndDate": max(days).isoformat(),
                "onPeakHighDemand": 19.0,
            }
        ],
        "dailyDemandList": [
            {"date": day.isoformat(), "onPeakDemand": value} for day, value in rows
        ],
    }


@pytest.mark.parametrize(
    "holiday",
    [
        pytest.param(date(2026, 1, 1), id="new-year-weekday"),
        pytest.param(date(2027, 12, 31), id="next-new-year-observed-friday"),
        pytest.param(date(2023, 1, 2), id="new-year-observed-monday"),
        pytest.param(date(2026, 5, 25), id="memorial-last-monday"),
        pytest.param(date(2026, 7, 3), id="independence-observed-friday"),
        pytest.param(date(2027, 7, 5), id="independence-observed-monday"),
        pytest.param(date(2024, 7, 4), id="independence-weekday"),
        pytest.param(date(2026, 9, 7), id="labor-first-monday"),
        pytest.param(date(2026, 11, 26), id="thanksgiving-fourth-thursday"),
        pytest.param(date(2026, 12, 25), id="christmas-weekday"),
        pytest.param(date(2021, 12, 24), id="christmas-observed-friday"),
        pytest.param(date(2022, 12, 26), id="christmas-observed-monday"),
    ],
)
@pytest.mark.parametrize("holiday_demand", [0.0, 12.0])
def test_average_excludes_srp_holidays_in_both(srp_api, holiday, holiday_demand):
    """A holiday is excluded by its date, including cross-year observance."""
    payload = _demand_payload([(holiday, holiday_demand), (date(2026, 2, 3), 6.0)])
    assert srp_api.current_demand(payload) == (6.0, True)
    assert jsonapi.parse_demand_observation(payload).demand_kw == 6.0


@pytest.mark.parametrize(
    "ordinary_day",
    [
        pytest.param(date(2026, 2, 3), id="ordinary-weekday"),
        pytest.param(date(2026, 1, 19), id="mlk-day-is-on-peak"),
        pytest.param(date(2026, 11, 27), id="black-friday-is-on-peak"),
        pytest.param(date(2026, 5, 18), id="monday-before-memorial"),
        pytest.param(date(2026, 9, 14), id="monday-after-labor"),
        pytest.param(date(2026, 11, 19), id="thursday-before-thanksgiving"),
    ],
)
@pytest.mark.parametrize("ordinary_demand", [0.0, 10.0])
def test_average_keeps_nonholiday_weekdays_in_both(srp_api, ordinary_day, ordinary_demand):
    """A real zero remains in the denominator, as do non-SRP holidays."""
    payload = _demand_payload([(ordinary_day, ordinary_demand), (date(2026, 2, 4), 6.0)])
    expected = (ordinary_demand + 6.0) / 2
    assert srp_api.current_demand(payload) == (expected, True)
    assert jsonapi.parse_demand_observation(payload).demand_kw == expected


def test_only_srp_holidays_has_no_average_in_both(srp_api):
    payload = _demand_payload(
        [(date(2026, 1, 1), 0.0), (date(2026, 7, 3), 0.0), (date(2026, 11, 26), 0.0)]
    )
    assert srp_api.current_demand(payload) is None
    assert jsonapi.parse_demand_observation(payload) is None


def test_cross_year_cycle_keeps_only_on_peak_weekdays_in_both(srp_api):
    payload = _demand_payload(
        [
            (date(2021, 12, 30), 0.0),  # genuine zero
            (date(2021, 12, 31), 0.0),  # next year's observed New Year's Day
            (date(2022, 1, 1), 0.0),  # Saturday
            (date(2022, 1, 2), 0.0),  # Sunday
            (date(2022, 1, 3), 6.0),
        ]
    )
    assert srp_api.current_demand(payload) == (3.0, True)
    assert jsonapi.parse_demand_observation(payload).demand_kw == 3.0


def test_holiday_filter_does_not_change_peak_plan_in_both(srp_api):
    payload = _demand_payload([(date(2026, 11, 26), 0.0)], is_average=False)
    assert srp_api.current_demand(payload) == (19.0, False)
    observation = jsonapi.parse_demand_observation(payload)
    assert observation.demand_kw == 19.0
    assert observation.is_average is False


def test_bill_projection_agrees(srp_api):
    payload = {"amount": 435.0, "month": "September", "hasProjection": True}
    assert srp_api.parse_projection(payload) == jsonapi.parse_bill_projection(payload).amount_usd


def test_absent_projection_agrees(srp_api):
    payload = {"hasProjection": False}
    assert srp_api.parse_projection(payload) is None
    assert jsonapi.parse_bill_projection(payload) is None


def test_demand_off_peak_is_dropped_in_both(srp_api, daily):
    """SRP publishes an off-peak demand figure; neither path should store it."""
    theirs = srp_api.parse_daily(daily, ["demand"])
    ours = jsonapi.parse_daily_usage_detail(daily, series=[Series.DEMAND])
    assert all(d.off_peak is None for d in theirs)
    assert all(r.off_peak is None for r in ours)


def test_interval_demand_off_peak_is_dropped_in_both(srp_api, hourly):
    """The same rule on the interval path, where it once was not applied.

    ``demandList`` carries both legs, and off-peak demand on a solar account
    runs roughly double the on-peak figure. Anything that collapsed the pair to
    a single number -- the Home Assistant coordinator does exactly that when it
    builds the hourly max -- would publish an unbilled 25 kW as the demand for
    a cycle whose billed peak was 12.7 kW. Dropping the leg at the parser makes
    that unrepresentable rather than merely unlikely.
    """
    fixture_has_off_peak = any(row.get("offPeakDemand") for row in hourly.get("demandList") or [])
    assert fixture_has_off_peak, "fixture must exercise the leg being dropped"

    theirs = srp_api.parse_intervals(hourly, ["demand"])
    ours = jsonapi.parse_hourly_detail(hourly, series=[Series.DEMAND])
    assert theirs and ours
    assert all(i.off_peak is None for i in theirs)
    assert all(r.off_peak is None for r in ours)
    # the on-peak leg must survive untouched
    assert any(i.on_peak for i in theirs)


def test_component_declares_no_requirements():
    """HAOS is musl; a dependency without a matching wheel breaks setup.

    The component is written against the standard library plus the aiohttp
    Home Assistant already ships, so this list stays empty.
    """
    manifest = json.loads((COMPONENT / "manifest.json").read_text())
    assert manifest["requirements"] == []
    assert manifest["version"], "custom integrations are blocked from loading without a version"
    assert manifest["domain"] == COMPONENT.name
