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


def test_component_declares_no_requirements():
    """HAOS is musl; a dependency without a matching wheel breaks setup.

    The component is written against the standard library plus the aiohttp
    Home Assistant already ships, so this list stays empty.
    """
    manifest = json.loads((COMPONENT / "manifest.json").read_text())
    assert manifest["requirements"] == []
    assert manifest["version"], "custom integrations are blocked from loading without a version"
    assert manifest["domain"] == COMPONENT.name
