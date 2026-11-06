"""Demand checks use complete real-shaped intervals, never device or bill writes."""

from __future__ import annotations

import asyncio
import copy
import importlib
import json
import sys
import types
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

COMPONENT = Path(__file__).parents[1] / "custom_components/srp_energy_monitor"
ZONE = ZoneInfo("America/Phoenix")


@pytest.fixture(scope="module")
def modules():
    package = types.ModuleType("audit_component")
    package.__path__ = [str(COMPONENT)]
    sys.modules[package.__name__] = package
    try:
        yield (importlib.import_module("audit_component.demand_audit"),
               importlib.import_module("audit_component.srp_api"))
    finally:
        for name in list(sys.modules):
            if name.startswith("audit_component"):
                del sys.modules[name]


def evidence(day=date(2026, 11, 4), maximum=5.0):
    # Independent explicit tariff windows, with one winning half-hour.
    hours = range(14, 20) if 5 <= day.month <= 10 else [5, 6, 7, 8, 17, 18, 19, 20]
    rows = []
    for hour in hours:
        for minute in (0, 30):
            stamp = datetime(day.year, day.month, day.day, hour, minute)
            rows.append({"date": stamp.isoformat(), "hour": stamp.isoformat(),
                         "intervalTimeInMinutes": 30, "onPeakDemand": 0.0})
    rows[1]["onPeakDemand"] = maximum
    daily = {"isAverageDemandRate": True, "dailyDemandList": [
        {"date": day.isoformat(), "onPeakDemand": maximum}], "highDemandInfoList": [
            {"billStartDate": day.isoformat(), "billEndDate": day.isoformat(),
             "onPeakHighDemand": maximum}]}
    return daily, rows


def audit(modules, daily, rows, day=date(2026, 11, 4), **kwargs):
    params = {"plan_code": "E15", "timestamp_start_verified": True, **kwargs}
    return modules[0].build_demand_audit(
        daily, rows, day, day,
        datetime.combine(day + timedelta(days=1), datetime.min.time(), ZONE), **params)


@pytest.mark.parametrize(("day", "count", "first", "last"), [
    (date(2026, 11, 4), 16, "05:00", "20:30"),
    (date(2026, 5, 4), 12, "14:00", "19:30"),
    (date(2026, 1, 19), 16, "05:00", "20:30"),  # MLK is not an SRP holiday
    (date(2026, 11, 27), 16, "05:00", "20:30"),  # Black Friday is not either
])
def test_complete_peak_slots_and_exact_winner(modules, day, count, first, last):
    daily, rows = evidence(day)
    result = audit(modules, daily, rows, day)
    assert result["state"] == "matches"
    assert result["days"][0]["present_slots"] == count
    assert rows[0]["date"][11:16] == first
    assert rows[-1]["date"][11:16] == last
    winner = result["events"][0]
    assert winner["start"][11:16] == ("14:30" if count == 12 else "05:30")
    assert winner["end"][11:16] == ("15:00" if count == 12 else "06:00")
    assert winner["demand_kw"] == 5
    assert result["billed_cost_verified"] is False


def test_zero_weekday_remains_complete_and_cycle_mean_includes_zero(modules):
    daily, rows = evidence(maximum=0)
    result = audit(modules, daily, rows)
    assert result["state"] == "matches"
    assert result["latest_daily_max_kw"] == 0
    assert result["derived_cycle_means"][0]["derived_average_kw"] == 0


@pytest.mark.parametrize("day", [date(2026, 1, 1), date(2026, 5, 25), date(2026, 7, 3),
                                date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25),
                                date(2021, 12, 31), date(2026, 11, 7)])
def test_holiday_weekend_no_slots_and_not_zero_demand_day(modules, day):
    result = audit(modules, {"isAverageDemandRate": True}, [], day)
    assert result["days"][0]["status"] == "not_applicable"
    assert result["days"][0]["expected_slots"] == 0
    assert result["latest_complete_day"] is None


@pytest.mark.parametrize("plan", [None, "E16", "E27", "unverified"])
def test_average_flag_does_not_prove_e15(modules, plan):
    daily, rows = evidence()
    assert audit(modules, daily, rows, plan_code=plan)["state"] == "unsupported"


@pytest.mark.parametrize("change", [
    {"onPeakDemand": None}, {"onPeakDemand": float("nan")},
    {"onPeakDemand": float("inf")}, {"onPeakDemand": -1}, {"onPeakDemand": True},
    {"intervalTimeInMinutes": 60}, {"intervalTimeInMinutes": True}, {"unit": "W"},
    {"hour": "2026-11-04T05:30:00"},
])
def test_invalid_peak_evidence_is_incomplete_and_json_safe(modules, change):
    daily, rows = evidence()
    rows[0].update(change)
    result = audit(modules, daily, rows)
    assert result["state"] == "incomplete"
    assert result["events"] == []
    json.dumps(result, allow_nan=False)


def test_missing_slot_not_zero_and_conflicting_duplicate_refused(modules):
    daily, rows = evidence()
    assert audit(modules, daily, rows[1:])["state"] == "incomplete"
    assert audit(modules, daily, [*rows, rows[0]])["state"] == "matches"
    duplicate = dict(rows[0], onPeakDemand=9)
    assert audit(modules, daily, [*rows, duplicate])["state"] == "incomplete"


def test_missing_nonfinite_or_conflicting_daily_maximum_refused(modules):
    daily, rows = evidence()
    for value in [None, float("nan"), -1, True]:
        daily["dailyDemandList"][0]["onPeakDemand"] = value
        assert audit(modules, daily, rows)["state"] == "incomplete"
    daily["dailyDemandList"] = [{"date": "2026-11-04", "onPeakDemand": v} for v in (5, 8)]
    assert audit(modules, daily, rows)["state"] == "incomplete"


def test_mismatch_preserves_both_values_and_daily_rounding_tolerance(modules):
    daily, rows = evidence()
    daily["dailyDemandList"][0]["onPeakDemand"] = 5.05
    assert audit(modules, daily, rows)["state"] == "matches"
    daily["dailyDemandList"][0]["onPeakDemand"] = 5.1
    result = audit(modules, daily, rows)
    assert result["state"] == "mismatch"
    assert result["latest_daily_max_kw"] == 5.1
    assert result["latest_interval_max_kw"] == 5


def test_unverified_time_or_partial_fetch_never_matches(modules):
    daily, rows = evidence()
    assert audit(modules, daily, rows, timestamp_start_verified=False)["state"] == "incomplete"
    assert audit(modules, daily, rows, fetch_complete=False)["state"] == "incomplete"


def test_hash_stable_on_repeat_reorder_duplicate_but_changes_for_nonwinner(modules):
    daily, rows = evidence()
    result = audit(modules, daily, rows)
    assert audit(modules, daily, list(reversed(rows)))["content_hash"] == result["content_hash"]
    assert audit(modules, daily, [*rows, rows[0]])["content_hash"] == result["content_hash"]
    rows[-1]["onPeakDemand"] = 1
    assert audit(modules, daily, rows)["content_hash"] != result["content_hash"]


def test_interval_edges_do_not_include_nine_or_twenty_one(modules):
    daily, rows = evidence()
    rows.extend({"date": "2026-11-04T" + hour, "intervalTimeInMinutes": 30, "onPeakDemand": 0}
                for hour in ("04:30:00", "09:00:00", "16:30:00", "21:00:00"))
    result = audit(modules, daily, rows)
    assert result["state"] == "matches"
    assert result["days"][0]["present_slots"] == 16


def test_cycle_coverage_and_calendar_boundary_are_explicit(modules):
    daily, rows = evidence()
    daily["highDemandInfoList"][0]["billStartDate"] = "2026-11-02"
    result = audit(modules, daily, rows)
    cycle = result["derived_cycle_means"][0]
    assert cycle["eligible_days"] == 3
    assert cycle["present_days"] == 1
    assert cycle["derived_average_kw"] is None
    assert cycle["plan_history_verified"] is False
    assert len(modules[0].peak_slots(date(2026, 10, 30))) == 12
    assert len(modules[0].peak_slots(date(2026, 11, 2))) == 16


def test_client_reuses_existing_fetch_and_discards_partial_audit(modules, monkeypatch):
    api = modules[1]
    client = api.SRPClient(None, "unused", "unused", "unused")
    calls = []
    _daily, rows = evidence()

    async def ensure():
        return None

    async def request(path, **_kwargs):
        calls.append(path)
        if len(calls) == 2:
            raise api.SRPApiError("second window failed")
        return {"demandList": copy.deepcopy(rows)}

    async def no_sleep(_delay):
        return None

    client._ensure_session = ensure
    client._request = request
    monkeypatch.setattr(api, "_SLEEP", no_sleep)
    parsed = asyncio.run(client.intervals(date(2026, 11, 4), date(2026, 11, 4)))
    assert isinstance(parsed, list) and len(parsed) == 16
    assert calls == ["usage/hourlydetail"]
    assert client.demand_audit_rows == rows
    assert client.demand_audit_fetch_complete is True
    calls.clear()
    with pytest.raises(api.SRPApiError, match="second window"):
        asyncio.run(client.intervals(date(2026, 9, 1), date(2026, 11, 4)))
    assert client.demand_audit_rows == []
    assert client.demand_audit_fetch_complete is False


def test_coordinator_audits_existing_raw_fetch_even_without_statistics_rows(monkeypatch):
    # Reuse the existing HA/Recorder stand-ins, exercising the actual owner method.
    harness = importlib.import_module("test_ha_statistics_restatements")
    _coordinator, owner, _recorder = harness.component.__wrapped__(monkeypatch)
    day = date(2026, 11, 3)  # The harness freezes now on November 4.
    daily, rows = evidence(day)

    class Client:
        demand_audit_rows = rows
        demand_audit_fetch_complete = True
        calls = 0

        async def intervals(self, _start, _end):
            self.calls += 1
            return []

    async def start():
        return day

    owner._client = Client()
    owner._demand_audit_plan = "E15"
    owner._async_statistics_start = start
    result = asyncio.run(owner._async_update_statistics(daily))
    assert result["state"] == "matches"
    assert result["latest_complete_day"] == day.isoformat()
    assert owner._client.calls == 1
    owner._client.demand_audit_fetch_complete = False
    assert asyncio.run(owner._async_update_statistics(daily))["state"] == "incomplete"


def test_refresh_time_not_hash_and_future_day_never_matches(modules):
    daily, rows = evidence()
    first = modules[0].build_demand_audit(
        daily, rows, date(2026, 11, 4), date(2026, 11, 4),
        datetime(2026, 11, 5, 1, tzinfo=ZONE), plan_code="E15", timestamp_start_verified=True)
    later = modules[0].build_demand_audit(
        daily, rows, date(2026, 11, 4), date(2026, 11, 4),
        datetime(2026, 11, 5, 5, tzinfo=ZONE), plan_code="E15", timestamp_start_verified=True)
    assert first["last_checked"] != later["last_checked"]
    assert first["content_hash"] == later["content_hash"]
    future = modules[0].build_demand_audit(
        daily, rows, date(2026, 11, 4), date(2026, 11, 5),
        datetime(2026, 11, 5, 5, tzinfo=ZONE), plan_code="E15", timestamp_start_verified=True)
    assert future["state"] == "incomplete"
    assert "requested_unsettled_day" in future["issues"]
