"""Tests for the browser-free JSON backend.

Fixtures mirror the real response shape -- field names, the three date
spellings, half-hourly demand alongside hourly energy -- with invented numbers.
No network and no credentials, same as the CSV suite.

Several tests here exist because of specific ways this API misleads you rather
than fails: a range that silently truncates, four lists whose names do not mean
what they say, and a demand figure whose meaning depends on the price plan.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from itertools import pairwise
from pathlib import Path

import pytest

from srp_energy import Series, jsonapi

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def daily_payload():
    return _load("srp_dailyusagedetail.json")


@pytest.fixture
def hourly_payload():
    return _load("srp_hourlydetail.json")


@pytest.fixture
def weather_payload():
    return _load("srp_dailyweather.json")


# ------------------------------------------------------------------ chunking


class TestChunkDateRange:
    """The API answers an over-wide request with 200 OK and silent truncation.

    A two-year request returns one month. Nothing raises, nothing warns, and a
    backfill built on it looks complete. These tests pin the guard.
    """

    def test_two_year_range_is_split_into_honourable_windows(self):
        windows = jsonapi.chunk_date_range(date(2024, 8, 1), date(2026, 8, 26))
        assert len(windows) > 20
        assert all((end - start).days < jsonapi.HOURLY_MAX_DAYS for start, end in windows)

    def test_windows_are_contiguous_and_cover_the_whole_range(self):
        start, end = date(2024, 8, 1), date(2026, 8, 26)
        windows = jsonapi.chunk_date_range(start, end)
        assert windows[0][0] == start
        assert windows[-1][1] == end
        for (_, prev_end), (next_start, _) in pairwise(windows):
            assert (next_start - prev_end).days == 1, "gap or overlap between windows"

    def test_short_range_is_left_alone(self):
        windows = jsonapi.chunk_date_range(date(2026, 8, 1), date(2026, 8, 10))
        assert windows == [(date(2026, 8, 1), date(2026, 8, 10))]

    def test_single_day(self):
        day = date(2026, 8, 1)
        assert jsonapi.chunk_date_range(day, day) == [(day, day)]

    def test_exactly_the_limit_stays_one_window(self):
        start = date(2026, 8, 1)
        end = start.replace(day=1) + (date(2026, 8, 31) - date(2026, 8, 1))
        windows = jsonapi.chunk_date_range(start, end)
        assert len(windows) == 1

    def test_reversed_range_is_rejected(self):
        with pytest.raises(ValueError, match="before start"):
            jsonapi.chunk_date_range(date(2026, 8, 10), date(2026, 8, 1))

    def test_nonsense_window_size_is_rejected(self):
        with pytest.raises(ValueError, match="at least 1"):
            jsonapi.chunk_date_range(date(2026, 8, 1), date(2026, 8, 2), max_days=0)


# ------------------------------------------------------------- date handling


class TestDateParsing:
    """One response carries three different date spellings."""

    @pytest.mark.parametrize(
        "raw",
        [
            "2026-08-26T00:00:00",  # naive, already Arizona local
            "2026-08-26T07:00:00Z",  # UTC -- 07:00Z is midnight MST
            "2026-08-26T00:00:00-07:00",  # explicit Arizona offset
        ],
    )
    def test_all_three_spellings_give_the_same_local_date(self, raw):
        assert jsonapi._as_date(raw) == date(2026, 8, 26)

    def test_unparseable_values_are_dropped_not_guessed(self):
        assert jsonapi._as_date("not a date") is None
        assert jsonapi._as_date(None) is None

    def test_interval_timestamps_stay_naive(self):
        parsed = jsonapi._as_naive_datetime("2026-08-26T19:30:00")
        assert parsed == datetime(2026, 8, 26, 19, 30)
        assert parsed.tzinfo is None


# ------------------------------------------------------------ series mapping


class TestSeriesMapping:
    """SRP's list names do not match its own CSV export's series names.

    Most importantly the list called "usage" is *net* metered energy. Summing
    it and calling it consumption reports a different quantity than the one the
    bill's kWh line is built from.
    """

    def test_net_comes_from_the_list_srp_calls_usage(self, hourly_payload):
        readings = jsonapi.parse_hourly_detail(hourly_payload, [Series.NET])
        # hourlyUsageList's off-peak leg is 4.0 in the fixture; consumption is 3.0
        off_peaks = {r.off_peak for r in readings}
        assert 4.0 in off_peaks
        assert 3.0 not in off_peaks

    def test_usage_comes_from_the_list_srp_calls_consumption(self, hourly_payload):
        readings = jsonapi.parse_hourly_detail(hourly_payload, [Series.USAGE])
        assert 3.0 in {r.off_peak for r in readings}

    def test_every_series_is_parsed_by_default(self, hourly_payload):
        readings = jsonapi.parse_hourly_detail(hourly_payload)
        assert {r.series for r in readings} == set(Series)


# ------------------------------------------------------------------ hourly


class TestParseHourlyDetail:
    def test_energy_is_hourly_and_demand_is_half_hourly(self, hourly_payload):
        readings = jsonapi.parse_hourly_detail(hourly_payload)
        by_series: dict[Series, list] = {}
        for r in readings:
            by_series.setdefault(r.series, []).append(r)

        assert len(by_series[Series.USAGE]) == 24
        assert len(by_series[Series.DEMAND]) == 48
        assert {r.interval_start.minute for r in by_series[Series.DEMAND]} == {0, 30}
        assert {r.interval_start.minute for r in by_series[Series.USAGE]} == {0}

    def test_demand_intervals_carry_kw(self, hourly_payload):
        readings = jsonapi.parse_hourly_detail(hourly_payload, [Series.DEMAND])
        assert readings[0].series.unit == "kW"

    def test_missing_list_yields_nothing_rather_than_raising(self):
        assert jsonapi.parse_hourly_detail({}) == []

    def test_row_without_a_timestamp_is_skipped(self):
        payload = {"demandList": [{"onPeakDemand": 5.0}, {"date": None}]}
        assert jsonapi.parse_hourly_detail(payload, [Series.DEMAND]) == []


# ------------------------------------------------------------------- daily


class TestParseDailyUsageDetail:
    def test_all_four_series_are_produced(self, daily_payload):
        readings = jsonapi.parse_daily_usage_detail(daily_payload)
        assert {r.series for r in readings} == set(Series)

    def test_demand_has_no_off_peak_leg(self, daily_payload):
        """SRP reports an off-peak demand figure but bills only the on-peak one.

        The CSV export omits it entirely, so dropping it keeps both backends
        writing byte-identical rows.
        """
        demand = jsonapi.parse_daily_usage_detail(daily_payload, series=[Series.DEMAND])
        assert all(r.off_peak is None for r in demand)
        assert any(r.on_peak for r in demand)

    def test_meter_read_date_is_parsed_from_the_offset_form(self, daily_payload):
        readings = jsonapi.parse_daily_usage_detail(daily_payload, series=[Series.USAGE])
        first = min(readings, key=lambda r: r.usage_date)
        assert first.meter_read_date is not None
        assert first.meter_read_date > first.usage_date

    def test_weather_is_merged_when_supplied(self, daily_payload, weather_payload):
        weather = jsonapi.parse_daily_weather(weather_payload)
        readings = jsonapi.parse_daily_usage_detail(
            daily_payload, weather=weather, series=[Series.USAGE]
        )
        assert all(r.high_temp_f is not None for r in readings)

    def test_weather_is_optional(self, daily_payload):
        readings = jsonapi.parse_daily_usage_detail(daily_payload, series=[Series.USAGE])
        assert all(r.high_temp_f is None for r in readings)

    def test_total_is_none_for_demand(self, daily_payload):
        demand = jsonapi.parse_daily_usage_detail(daily_payload, series=[Series.DEMAND])
        assert all(r.total is None for r in demand)


# ------------------------------------------------------------- cycle peaks


class TestParseCyclePeaks:
    def test_peaks_are_returned_oldest_first(self, daily_payload):
        peaks = jsonapi.parse_cycle_peaks(daily_payload)
        assert [p.cycle_start for p in peaks] == sorted(p.cycle_start for p in peaks)

    def test_rows_without_dates_are_skipped(self, daily_payload):
        """The fixture carries a 99 kW row with null dates -- it must not win."""
        peaks = jsonapi.parse_cycle_peaks(daily_payload)
        assert len(peaks) == 2
        assert all(p.on_peak_high_demand_kw != 99.0 for p in peaks)

    def test_the_plan_type_rides_along(self, daily_payload):
        peaks = jsonapi.parse_cycle_peaks(daily_payload)
        assert all(p.is_average_demand_rate for p in peaks)

    def test_occurrence_timestamp_is_captured(self, daily_payload):
        latest = jsonapi.parse_cycle_peaks(daily_payload)[-1]
        assert latest.occurred_at == datetime(2026, 3, 3, 20, 0)

    def test_empty_payload(self):
        assert jsonapi.parse_cycle_peaks({}) == []


# ------------------------------------------------------ demand observation


class TestParseDemandObservation:
    """The headline figure means different things on different price plans.

    Reporting one as the other is the mistake this project exists to avoid:
    on this account a cycle mean of 5.6 kW sat alongside a maximum of 12.7.
    """

    def test_average_plan_yields_the_weekday_mean(self, daily_payload):
        obs = jsonapi.parse_demand_observation(daily_payload)
        # weekday maxima in the fixture: 6.0, 9.0, 0.0, 8.0, 7.0 -> mean 6.0
        assert obs.demand_kw == pytest.approx(6.0)
        assert obs.is_average is True

    def test_average_plan_ignores_weekends(self, daily_payload):
        """Weekends carry no on-peak demand; averaging their zeros halves it."""
        obs = jsonapi.parse_demand_observation(daily_payload)
        all_days = [
            r.on_peak
            for r in jsonapi.parse_daily_usage_detail(daily_payload, series=[Series.DEMAND])
        ]
        naive_mean = sum(all_days) / len(all_days)
        assert obs.demand_kw > naive_mean

    def test_peak_plan_yields_the_cycle_maximum(self, daily_payload):
        payload = dict(daily_payload, isAverageDemandRate=False)
        obs = jsonapi.parse_demand_observation(payload)
        assert obs.demand_kw == 9.0
        assert obs.is_average is False
        assert "High" in obs.label

    def test_label_and_value_never_disagree(self, daily_payload):
        for is_average in (True, False):
            obs = jsonapi.parse_demand_observation(
                dict(daily_payload, isAverageDemandRate=is_average)
            )
            assert obs.is_average is is_average

    def test_observed_at_can_be_pinned(self, daily_payload):
        stamp = datetime(2026, 3, 9, 7, 30)
        obs = jsonapi.parse_demand_observation(daily_payload, observed_at=stamp)
        assert obs.observed_at == stamp

    def test_empty_payload(self):
        assert jsonapi.parse_demand_observation({}) is None


# ------------------------------------------------------------- projection


class TestParseBillProjection:
    def test_projection_is_parsed(self):
        payload = {"amount": 435.0, "month": "September", "hasProjection": True}
        proj = jsonapi.parse_bill_projection(payload)
        assert proj.amount_usd == 435.0
        assert "September" in proj.basis

    def test_absent_projection_yields_none(self):
        assert jsonapi.parse_bill_projection({"hasProjection": False}) is None
        assert jsonapi.parse_bill_projection({}) is None

    def test_projection_without_an_amount_yields_none(self):
        assert jsonapi.parse_bill_projection({"hasProjection": True, "amount": None}) is None


# ------------------------------------------------------------------ client


class TestClientValidation:
    def test_credentials_are_required(self):
        with pytest.raises(ValueError, match="username and password"):
            jsonapi.SRPJsonClient("", "pw", "123456789")

    def test_account_id_is_required(self):
        with pytest.raises(ValueError, match="account id"):
            jsonapi.SRPJsonClient("user", "pw", "")


class TestErrorClassification:
    """A 403 from Cloudflare is not an authentication failure.

    The same credentials have been observed refused and accepted minutes
    apart, varying by host and by HTTP client. Reporting it as bad credentials
    sends people to fix the one thing that is definitely fine.
    """

    def test_cloudflare_block_is_distinguished_from_bad_credentials(self):
        with pytest.raises(jsonapi.AccessBlocked, match="credentials are not the problem"):
            jsonapi.SRPJsonClient._raise_for_error(
                403, b"<html><title>Access Denied</title></html>", "login/authorize"
            )

    def test_rejected_credentials_raise_auth_error(self):
        with pytest.raises(jsonapi.AuthError):
            jsonapi.SRPJsonClient._raise_for_error(
                400, b'{"error":"invalid_grant"}', "login/authorize"
            )

    def test_other_failures_stay_generic(self):
        with pytest.raises(jsonapi.SRPApiError):
            jsonapi.SRPJsonClient._raise_for_error(500, b"boom", "usage/hourlydetail")

    def test_auth_and_access_errors_are_both_api_errors(self):
        assert issubclass(jsonapi.AuthError, jsonapi.SRPApiError)
        assert issubclass(jsonapi.AccessBlocked, jsonapi.SRPApiError)


def test_module_does_not_require_playwright(monkeypatch):
    """The whole point of this backend: no browser anywhere in the import path."""
    monkeypatch.setitem(__import__("sys").modules, "playwright", None)
    import importlib

    importlib.reload(jsonapi)
    assert jsonapi.HOURLY_MAX_DAYS == 31
