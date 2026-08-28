"""Polling and long-term statistics for SRP Energy Monitor.

Two jobs, on different clocks.

The **sensors** need the current cycle's demand figure, the billed peak and the
bill projection -- a handful of scalars refreshed every few hours.

The **statistics** need history, and this is where the integration earns its
keep. SRP publishes interval data going back years, so a first run backfills
roughly two years of hourly energy and however much half-hourly demand the
account has (demand appears from around mid-2025 on the account this was built
against). After that only the days since the last recorded statistic are
fetched.

Demand is stored as a *mean* statistic with min and max rather than a sum,
because kW is a rate, not a quantity. That also means the hourly `max` column
holds the highest half-hour of each hour, which is the granularity the demand
charge is actually billed on -- an hourly average would understate the real
peak by however hard load ramped inside the hour.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter, PowerConverter

from .const import (
    BACKFILL_DAYS,
    CONF_ACCOUNT_ID,
    DOMAIN,
    LOGGER,
    STAT_CONSUMPTION,
    STAT_DEMAND,
    STAT_GENERATION,
    STAT_NET,
    UPDATE_INTERVAL,
)
from .srp_api import (
    CyclePeak,
    Interval,
    SRPAccessBlocked,
    SRPApiError,
    SRPAuthError,
    SRPClient,
    current_demand,
    parse_cycle_peaks,
    parse_daily,
    parse_projection,
)

# A plain alias rather than PEP 695 `type`: this repo's CI lints and tests
# down to Python 3.10, while Home Assistant itself runs 3.13. The string
# forward reference keeps both happy.
SRPConfigEntry = ConfigEntry["SRPCoordinator"]

# Resolved once at import. get_time_zone() can block on a cache miss, which
# is safe here only because Home Assistant imports integration modules in an
# executor -- the default whenever a manifest omits `import_executor`. Do not
# set `"import_executor": false` in manifest.json without moving this.
PHOENIX = dt_util.get_time_zone("America/Phoenix")

#: series -> (statistic suffix, friendly name)
_ENERGY_STATS = {
    "usage": (STAT_CONSUMPTION, "consumption"),
    "generation": (STAT_GENERATION, "generation"),
    "net": (STAT_NET, "net energy"),
}


@dataclass(slots=True)
class SRPData:
    """Everything the sensors read, refreshed together."""

    demand_kw: float | None = None
    demand_is_average: bool = False
    cycle_peak: CyclePeak | None = None
    projection_usd: float | None = None
    latest_day: date | None = None
    daily_totals: dict[str, float] | None = None


class SRPCoordinator(DataUpdateCoordinator[SRPData]):
    """Fetches SRP data and keeps long-term statistics topped up."""

    config_entry: SRPConfigEntry

    def __init__(self, hass: HomeAssistant, entry: SRPConfigEntry) -> None:
        self.account_id: str = entry.data[CONF_ACCOUNT_ID]
        self._client = SRPClient(
            async_get_clientsession(hass),
            entry.data[CONF_USERNAME],
            entry.data[CONF_PASSWORD],
            self.account_id,
        )
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )

    def statistic_id(self, suffix: str) -> str:
        """``<domain>:<account>_<suffix>``.

        The recorder rejects any statistic id containing a double underscore,
        and requires the prefix to equal the metadata ``source``.
        """
        return f"{DOMAIN}:{self.account_id}_{suffix}"

    async def _async_update_data(self) -> SRPData:
        try:
            daily_payload = await self._client.daily_detail()
            projection_payload = await self._client.projection()
        except SRPAuthError as err:
            # The one failure that really is the credentials -- surfacing it as
            # a reauth prompt beats retrying a wrong password every four hours.
            raise ConfigEntryAuthFailed(str(err)) from err
        except SRPAccessBlocked as err:
            # Transient and not the user's fault; UpdateFailed retries quietly.
            raise UpdateFailed(str(err)) from err
        except SRPApiError as err:
            raise UpdateFailed(f"Error talking to SRP: {err}") from err

        peaks = parse_cycle_peaks(daily_payload)
        demand = current_demand(daily_payload)
        daily = parse_daily(daily_payload)

        latest_day = max((d.day for d in daily), default=None)
        totals = {
            d.series: d.total for d in daily if latest_day is not None and d.day == latest_day
        }

        try:
            await self._async_update_statistics(daily_payload)
        except SRPApiError as err:
            # History is a bonus; never fail the sensors over it.
            LOGGER.warning("statistics update skipped: %s", err)

        return SRPData(
            demand_kw=demand[0] if demand else None,
            demand_is_average=demand[1] if demand else False,
            cycle_peak=peaks[-1] if peaks else None,
            projection_usd=parse_projection(projection_payload),
            latest_day=latest_day,
            daily_totals=totals,
        )

    # ------------------------------------------------------------ statistics

    async def _async_update_statistics(self, daily_payload: dict) -> None:
        """Fetch whatever interval data we do not already have, and store it."""
        start = await self._async_statistics_start()
        end = date.today()
        if start > end:
            LOGGER.debug("statistics already current through %s", end)
            return

        LOGGER.debug("fetching intervals %s .. %s for statistics", start, end)
        intervals = await self._client.intervals(start, end)
        if not intervals:
            return

        for series, (suffix, label) in _ENERGY_STATS.items():
            await self._async_write_energy(series, suffix, label, intervals)
        await self._async_write_demand(intervals)

    async def _async_statistics_start(self) -> date:
        """Resume from the last recorded hour, or reach back for a first run."""
        last = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics,
            self.hass,
            1,
            self.statistic_id(STAT_CONSUMPTION),
            True,
            # Empty on purpose: "start" is not a member of the `types` Literal,
            # and every returned row carries start/end regardless of what is
            # asked for. Core's own opower integration does the same here.
            set(),
        )
        rows = last.get(self.statistic_id(STAT_CONSUMPTION)) if last else None
        if not rows:
            LOGGER.info("no SRP statistics yet; backfilling %s days", BACKFILL_DAYS)
            return date.today() - timedelta(days=BACKFILL_DAYS)
        # Re-fetch the last stored day: SRP restates recent intervals as meter
        # data settles, and a sum statistic must be rebuilt from a known point
        # rather than patched.
        return datetime.fromtimestamp(rows[0]["start"], PHOENIX).date()

    async def _async_write_energy(
        self, series: str, suffix: str, label: str, intervals: list[Interval]
    ) -> None:
        """Write one hourly kWh series as a cumulative-sum statistic."""
        rows = sorted((i for i in intervals if i.series == series), key=lambda i: i.start)
        if not rows:
            return
        statistic_id = self.statistic_id(suffix)

        last = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, True, {"sum"}
        )
        previous = last.get(statistic_id) if last else None
        running = float(previous[0]["sum"]) if previous else 0.0
        resume_after = datetime.fromtimestamp(previous[0]["start"], PHOENIX) if previous else None

        points: list[StatisticData] = []
        for row in rows:
            start = row.start.replace(minute=0, second=0, microsecond=0, tzinfo=PHOENIX)
            if resume_after is not None and start < resume_after:
                continue
            running += row.total
            points.append(StatisticData(start=start, state=row.total, sum=running))

        if not points:
            return
        async_add_external_statistics(
            self.hass,
            StatisticMetaData(
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                name=f"SRP {label}",
                source=DOMAIN,
                statistic_id=statistic_id,
                unit_class=EnergyConverter.UNIT_CLASS,
                unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            ),
            points,
        )
        LOGGER.debug("wrote %s %s statistics", len(points), statistic_id)

    async def _async_write_demand(self, intervals: list[Interval]) -> None:
        """Write half-hourly kW as an hourly mean/min/max statistic.

        The ``max`` column is the useful one: it preserves the highest half hour
        of each hour, which is the granularity SRP bills demand on. Averaging
        the two half-hours together -- which is all an hourly-resolution source
        can do -- understates the billed peak by however hard load ramped.
        """
        buckets: dict[datetime, list[float]] = {}
        for row in intervals:
            if row.series != "demand":
                continue
            value = max(row.off_peak or 0.0, row.on_peak or 0.0)
            hour = row.start.replace(minute=0, second=0, microsecond=0, tzinfo=PHOENIX)
            buckets.setdefault(hour, []).append(value)

        if not buckets:
            return
        points = [
            StatisticData(
                start=hour,
                mean=sum(values) / len(values),
                min=min(values),
                max=max(values),
            )
            for hour, values in sorted(buckets.items())
        ]
        async_add_external_statistics(
            self.hass,
            StatisticMetaData(
                mean_type=StatisticMeanType.ARITHMETIC,
                has_sum=False,
                name="SRP demand",
                source=DOMAIN,
                statistic_id=self.statistic_id(STAT_DEMAND),
                unit_class=PowerConverter.UNIT_CLASS,
                unit_of_measurement=UnitOfPower.KILO_WATT,
            ),
            points,
        )
        LOGGER.debug("wrote %s demand statistics", len(points))
