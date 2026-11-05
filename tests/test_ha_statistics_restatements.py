"""Real coordinator methods against HA's documented exact-start import contract.

The fake import upserts the supplied state/sum at each start. It deliberately
does not fix following rows: Core 2026.10.4 _import_statistics_with_session
does not do that either. No network, HA installation or database is required.
"""

from __future__ import annotations

import asyncio
import copy
import importlib
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

PHOENIX = ZoneInfo("America/Phoenix")
COMPONENT = Path(__file__).resolve().parents[1] / "custom_components/srp_energy_monitor"


def at(day=3, hour=0, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=PHOENIX)


class Generic:
    def __class_getitem__(cls, _item):
        return cls


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return at(4, 17, 15).astimezone(tz) if tz else at(4, 17, 15).replace(tzinfo=None)


class Recorder:
    def __init__(self):
        self.rows = {}
        self.imports = []
        self.reads = []

    async def async_add_executor_job(self, callback, *args):
        return callback(*args)

    def latest(self, _hass, count, statistic_id, convert_units, requested_types):
        self.reads.append((count, convert_units, requested_types))
        rows = sorted(self.rows.values(), key=lambda r: r["start"])[-count:]
        return {statistic_id: copy.deepcopy(rows)}

    def import_rows(self, _hass, metadata, rows):
        rows = list(rows)
        self.imports.append((metadata, copy.deepcopy(rows)))
        for row in rows:
            stamp = row["start"].timestamp()
            self.rows[stamp] = {**row, "start": stamp}

    def seed(self, stamp, state, total):
        self.rows[stamp.timestamp()] = {"start": stamp.timestamp(), "state": state, "sum": total}


@pytest.fixture
def component(monkeypatch):
    recorder = Recorder()

    def module(name, **attributes):
        obj = types.ModuleType(name)
        obj.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, obj)
        return obj

    for name in (
        "homeassistant",
        "homeassistant.components",
        "homeassistant.helpers",
        "homeassistant.util",
    ):
        module(name)
    module("homeassistant.components.recorder", get_instance=lambda _h: recorder)
    module(
        "homeassistant.components.recorder.models",
        StatisticData=dict,
        StatisticMetaData=dict,
        StatisticMeanType=types.SimpleNamespace(NONE=0, ARITHMETIC=1),
    )
    module(
        "homeassistant.components.recorder.statistics",
        get_last_statistics=recorder.latest,
        async_add_external_statistics=recorder.import_rows,
    )
    module("homeassistant.config_entries", ConfigEntry=Generic)
    module(
        "homeassistant.const",
        CONF_PASSWORD="password",
        CONF_USERNAME="username",
        UnitOfEnergy=types.SimpleNamespace(KILO_WATT_HOUR="kWh"),
        UnitOfPower=types.SimpleNamespace(KILO_WATT="kW"),
    )
    module("homeassistant.core", HomeAssistant=object)
    module("homeassistant.exceptions", ConfigEntryAuthFailed=type("AuthFailed", (Exception,), {}))
    module("homeassistant.helpers.aiohttp_client", async_create_clientsession=lambda _h: None)
    module(
        "homeassistant.helpers.update_coordinator",
        DataUpdateCoordinator=Generic,
        UpdateFailed=type("UpdateFailed", (Exception,), {}),
    )
    module("homeassistant.util.dt", get_time_zone=ZoneInfo)
    module(
        "homeassistant.util.unit_conversion",
        EnergyConverter=types.SimpleNamespace(UNIT_CLASS="energy"),
        PowerConverter=types.SimpleNamespace(UNIT_CLASS="power"),
    )
    package = module("srp_statistics_test")
    package.__path__ = [str(COMPONENT)]
    for name in ("coordinator", "const", "srp_api"):
        monkeypatch.delitem(sys.modules, "srp_statistics_test." + name, raising=False)
    coordinator = importlib.import_module("srp_statistics_test.coordinator")
    monkeypatch.setattr(coordinator, "datetime", FrozenDatetime)
    owner = object.__new__(coordinator.SRPCoordinator)
    owner.hass = object()
    owner.account_id = "example"
    return coordinator, owner, recorder


def interval(component, stamp, value, series="net"):
    coordinator, _, _ = component
    return coordinator.Interval(series, stamp, value, 0.0)


def write(component, rows, series="net"):
    return asyncio.run(component[1]._async_write_energy(series, "energy_net", "net energy", rows))


def test_restatement_corrects_state_and_corrupt_sum_through_retained_tail(component):
    _, _, recorder = component
    recorder.seed(at(hour=21), 14.8, 100.0)
    recorder.seed(at(hour=22), 7.1, 107.1)
    # The real defect: 23:00's 13.6 kWh was added three times.
    recorder.seed(at(hour=23), 13.6, 147.9)
    write(component, [interval(component, at(hour=22), 14.1)])
    assert recorder.rows[at(hour=22).timestamp()]["state"] == 14.1
    assert recorder.rows[at(hour=23).timestamp()]["sum"] == pytest.approx(127.7)
    assert recorder.rows[at(hour=21).timestamp()]["sum"] == 100
    assert [p["start"] for p in recorder.imports[-1][1]] == [at(hour=22), at(hour=23)]


def test_repeated_import_is_idempotent_including_the_last_equal_hour(component):
    _, _, recorder = component
    recorder.seed(at(hour=21), 2.0, 100.0)
    rows = [interval(component, at(hour=22), 14.1), interval(component, at(hour=23), 13.6)]
    write(component, rows)
    once = copy.deepcopy(recorder.rows)
    write(component, rows)
    assert recorder.rows == once
    assert len(recorder.rows) == 3
    assert recorder.reads[-1][1:] == (False, {"state", "sum"})
    metadata = recorder.imports[-1][0]
    assert metadata["source"] == "srp_energy_monitor"
    assert metadata["statistic_id"] == "srp_energy_monitor:example_energy_net"
    assert metadata["has_sum"] and metadata["unit_class"] == "energy"
    assert metadata["unit_of_measurement"] == "kWh"


def test_two_revised_days_rebase_all_following_sums_from_pre_window_point(component):
    _, _, recorder = component
    recorder.seed(at(1, 23), 2, 100)
    rows = []
    running = 100
    for offset in range(48):
        stamp = at(2) + timedelta(hours=offset)
        old = 6.7 if offset == 22 else 7.1 if offset == 46 else 13.6 if offset == 47 else 1
        revised = 13.6 if offset == 22 else 14.1 if offset == 46 else old
        running += old
        recorder.seed(stamp, old, running + (27.2 if offset == 47 else 0))
        rows.append(interval(component, stamp, revised))
    write(component, list(reversed(rows)))
    assert recorder.reads[-1][0] == 49  # all 48 affected hours plus the known baseline
    assert recorder.rows[at(1, 23).timestamp()]["sum"] == 100
    assert recorder.rows[at(2, 22).timestamp()]["state"] == 13.6
    assert recorder.rows[at(3, 22).timestamp()]["state"] == 14.1
    assert recorder.rows[at(3, 23).timestamp()]["sum"] == pytest.approx(
        100 + sum(r.total for r in rows)
    )


def test_missing_response_hour_preserves_its_existing_state_and_rebases_sum(component):
    _, _, recorder = component
    recorder.seed(at(hour=20), 0, 10)
    recorder.seed(at(hour=21), 1, 11)
    recorder.seed(at(hour=22), -4, 7)
    recorder.seed(at(hour=23), 3, 10)
    write(component, [interval(component, at(hour=23), 3), interval(component, at(hour=21), 2)])
    assert recorder.rows[at(hour=22).timestamp()] == {
        "start": at(hour=22).timestamp(),
        "state": -4,
        "sum": 8,
    }
    assert recorder.rows[at(hour=23).timestamp()]["sum"] == 11


def test_signed_net_midnight_and_identical_duplicates_keep_exact_hours(component):
    _, _, recorder = component
    recorder.seed(at(2, 22), 2, 10)
    rows = [interval(component, at(3, 0), -5), interval(component, at(2, 23), -3)]
    write(component, rows + rows)
    assert recorder.rows[at(2, 23).timestamp()]["sum"] == 7
    assert recorder.rows[at(3, 0).timestamp()]["sum"] == 2
    assert len(recorder.rows) == 3


def test_new_series_zero_origin_and_repeat_work_without_prior_baseline(component):
    _, _, recorder = component
    rows = [interval(component, at(hour=22), -4), interval(component, at(hour=23), 2)]
    write(component, rows)
    once = copy.deepcopy(recorder.rows)
    write(component, rows)
    assert recorder.rows == once
    assert recorder.rows[at(hour=23).timestamp()]["sum"] == -2


@pytest.mark.parametrize(
    "case",
    [
        "conflict",
        "subhour",
        "future_input",
        "missing_reading",
        "nan",
        "gap",
        "baseline_gap",
        "future_tail",
        "unknown_baseline",
        "invalid_tail",
    ],
)
def test_ambiguous_or_incomplete_replay_never_queues_a_write(component, case):
    coordinator, _, recorder = component
    recorder.seed(at(hour=20), 1, 10)
    recorder.seed(at(hour=21), 1, 11)
    recorder.seed(at(hour=22), 1, 12)
    rows = [interval(component, at(hour=22), 2)]
    if case == "conflict":
        rows.append(interval(component, at(hour=22), 3))
    elif case == "subhour":
        rows = [interval(component, at(hour=22, minute=30), 2)]
    elif case == "future_input":
        rows = [interval(component, at(4), 2)]
    elif case == "missing_reading":
        rows = [coordinator.Interval("net", at(hour=22), None, None)]
    elif case == "nan":
        rows = [interval(component, at(hour=22), float("nan"))]
    elif case == "gap":
        # A settled gap: remove the missing hour from both sources.
        recorder.seed(at(hour=19), 1, 9)
        rows = [interval(component, at(hour=20), 2), interval(component, at(hour=22), 3)]
        del recorder.rows[at(hour=21).timestamp()]
    elif case == "baseline_gap":
        del recorder.rows[at(hour=21).timestamp()]
    elif case == "future_tail":
        recorder.seed(at(4), 1, 13)
    elif case == "unknown_baseline":
        recorder.rows.clear()
        recorder.seed(at(hour=22), 1, 99)
    elif case == "invalid_tail":
        recorder.seed(at(hour=23), None, 13)
    before = copy.deepcopy(recorder.rows)
    with pytest.raises(coordinator.SRPApiError):
        write(component, rows)
    assert recorder.imports == []
    assert recorder.rows == before


def test_fetch_revisits_seven_days_and_excludes_unsettled_phoenix_day(component):
    _, owner, recorder = component
    recorder.seed(at(3, 23), 1, 1)

    class Client:
        async def intervals(self, start, end):
            self.window = (start, end)
            return []

    owner._client = Client()
    asyncio.run(owner._async_update_statistics({}))
    assert owner._client.window == (at(4).date() - timedelta(days=7), at(3).date())


def test_collection_gap_starts_from_old_day_and_first_run_keeps_backfill(component):
    coordinator, owner, recorder = component
    old = at(4) - timedelta(days=20)
    recorder.seed(old, 1, 1)
    assert asyncio.run(owner._async_statistics_start()) == old.date()
    recorder.rows.clear()
    assert asyncio.run(owner._async_statistics_start()) == at(4).date() - timedelta(
        days=coordinator.BACKFILL_DAYS
    )


def test_one_unsafe_series_does_not_prevent_other_statistics_refresh(component):
    coordinator, owner, _ = component
    calls = []

    class Client:
        async def intervals(self, _start, _end):
            return [interval(component, at(hour=22), 1)]

    async def energy(series, _suffix, _label, _intervals):
        calls.append(series)
        if series == "usage":
            raise coordinator.SRPApiError("unsafe replay")

    async def demand(_intervals):
        calls.append("demand")

    owner._client = Client()
    owner._async_write_energy = energy
    owner._async_write_demand = demand
    asyncio.run(owner._async_update_statistics({}))
    assert calls == ["usage", "generation", "net", "demand"]
