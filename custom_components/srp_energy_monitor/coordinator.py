"""Polling and long-term statistics for SRP Energy Monitor.

Two jobs, on different clocks.

The **sensors** need the current cycle's demand figure, the billed peak and the
bill projection -- a handful of scalars refreshed every few hours.

The **statistics** need history, and this is where the integration earns its
keep. SRP publishes interval data going back years, so a first run backfills
roughly two years of hourly energy and however much half-hourly demand the
account has (demand appears from around mid-2025 on the account this was built
against). Later refreshes revisit seven settled days as well as any collection
gap, because SRP can revise intervals it already published.

Demand is stored as a *mean* statistic with min and max rather than a sum,
because kW is a rate, not a quantity. That also means the hourly `max` column
holds the highest half-hour of each hour, which is the granularity the demand
charge is actually billed on -- an hourly average would understate the real
peak by however hard load ramped inside the hour.

One rule governs failure handling here, and it was learned the hard way: **only
a wrong password may raise ConfigEntryAuthFailed.** Everything else has to fail
retryably. See the comment on that branch in :meth:`SRPCoordinator._async_update_data`,
and the module docstring of ``srp_api.py`` for the outage that established it.
"""

from __future__ import annotations

import math
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
from homeassistant.helpers.aiohttp_client import async_create_clientsession
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
from .demand_audit import build_demand_audit
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

RESTATEMENT_DAYS = 7
_HOUR = timedelta(hours=1)


def _energy_values(
    series: str, intervals: list[Interval], cutoff: datetime
) -> dict[datetime, float]:
    """Accept unambiguous completed hourly intervals, without inventing zeros."""
    values: dict[datetime, float] = {}
    for row in intervals:
        if row.series != series:
            continue
        start = row.start
        start = start.replace(tzinfo=PHOENIX) if start.tzinfo is None else start.astimezone(PHOENIX)
        if start.minute or start.second or start.microsecond or start >= cutoff:
            raise SRPApiError("energy history contains a non-hourly or unsettled/future interval")
        if row.off_peak is None and row.on_peak is None:
            raise SRPApiError("energy history contains an interval without a reading")
        try:
            value = float(row.total)
        except (TypeError, ValueError) as err:
            raise SRPApiError("energy history contains a non-numeric reading") from err
        if not math.isfinite(value):
            raise SRPApiError("energy history contains a non-finite reading")
        if start in values and values[start] != value:
            raise SRPApiError("energy history contains conflicting duplicate hours")
        values[start] = value
    return values


def _rebuild_energy_statistics(
    values: dict[datetime, float], previous: list[dict], cutoff: datetime
) -> list[StatisticData]:
    """Recompute an entire affected tail from a known preceding cumulative sum.

    HA's external import upserts exact starts, but does not repair later sums.
    Retain old states when a response omits an already recorded hour; never
    replace a missing hour with zero or discard a later recorded tail.
    """
    first = min(values)
    stored: dict[datetime, tuple[float | None, float]] = {}
    for row in previous:
        try:
            start = datetime.fromtimestamp(row["start"], PHOENIX)
            total = float(row["sum"])
            state = float(row["state"]) if row.get("state") is not None else None
        except (KeyError, TypeError, ValueError, OverflowError) as err:
            raise SRPApiError("recorded energy history has an invalid baseline/state") from err
        if start.minute or start.second or start.microsecond or start >= cutoff:
            raise SRPApiError(
                "recorded energy history contains non-hourly or unsettled/future rows"
            )
        if not math.isfinite(total) or (state is not None and not math.isfinite(state)):
            raise SRPApiError("recorded energy history contains a non-finite baseline/state")
        if start in stored:
            raise SRPApiError("recorded energy history contains duplicate hours")
        stored[start] = (state, total)

    preceding = [start for start in stored if start < first]
    baseline_at = max(preceding) if preceding else None
    if baseline_at is not None:
        running = stored[baseline_at][1]
    elif not stored:
        running = 0.0
    else:
        # A genuinely new series may start inside the fetched window. Only its
        # original zero-origin first point establishes a baseline without a
        # preceding row. A truncated/imported history with an unknown offset
        # needs explicit recovery rather than silently resetting that offset.
        earliest = min(stored)
        state, total = stored[earliest]
        if state is None or not math.isclose(total, state, rel_tol=0, abs_tol=1e-7):
            raise SRPApiError("no known pre-window energy baseline; targeted recovery is required")
        running = 0.0

    merged = {}
    for start, (state, _total) in stored.items():
        if start >= first:
            if state is None:
                raise SRPApiError("recorded energy tail has an hour without a state")
            merged[start] = state
    merged.update(values)
    points = []
    last = baseline_at
    for start in sorted(merged):
        if last is not None and start != last + _HOUR:
            raise SRPApiError(
                "energy replay has an unfilled hourly gap; previous statistics preserved"
            )
        running += merged[start]
        if not math.isfinite(running):
            raise SRPApiError("energy replay cumulative total is not finite")
        points.append(StatisticData(start=start, state=merged[start], sum=running))
        last = start
    return points


@dataclass(slots=True)
class SRPData:
    """Everything the sensors read, refreshed together."""

    demand_kw: float | None = None
    demand_is_average: bool = False
    cycle_peak: CyclePeak | None = None
    projection_usd: float | None = None
    latest_day: date | None = None
    daily_totals: dict[str, float | None] | None = None
    demand_audit: dict | None = None


class SRPCoordinator(DataUpdateCoordinator[SRPData]):
    """Fetches SRP data and keeps long-term statistics topped up."""

    config_entry: SRPConfigEntry

    def __init__(self, hass: HomeAssistant, entry: SRPConfigEntry) -> None:
        self.account_id: str = entry.data[CONF_ACCOUNT_ID]
        # Explicit configuration, not an inference from isAverageDemandRate:
        # that flag also covers E-16's different interval and peak contract.
        self._demand_audit_plan = entry.options.get("demand_audit_plan")
        # async_create_clientsession, not async_get_clientsession. The latter
        # hands out Home Assistant's *shared* session, and a shared session
        # means a shared cookie jar. SRP's entire notion of being logged in is
        # one cookie, .AspNet.MyAccount, so parking it in a jar that every other
        # integration also writes to made the integration's authentication state
        # something it did not control. A private session is one where the only
        # thing that can evict our cookie is SRP.
        #
        # Lifecycle, checked against the 2026.8 source rather than assumed
        # (homeassistant/helpers/aiohttp_client.py): with the default
        # auto_cleanup=True this registers _async_register_clientsession_shutdown,
        # which reads the config_entries.current_entry ContextVar and, when it is
        # set, calls entry.async_on_unload(...) to detach the session on unload
        # -- plus a listener for EVENT_HOMEASSISTANT_CLOSE. That ContextVar is
        # set by ConfigEntry.async_setup for the duration of the awaited
        # async_setup_entry, and this constructor runs inside it, in the same
        # task, so the registration lands on our entry. Reloading the entry
        # therefore starts a genuinely fresh session and jar; unloading leaves
        # nothing behind. Nothing to close by hand here -- and note the helper
        # detaches rather than closes, because the underlying connector belongs
        # to Home Assistant and is shared. Closing it would be a bug.
        self._client = SRPClient(
            async_create_clientsession(hass),
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
            #
            # Weigh this branch carefully before widening it. Raised out of
            # async_config_entry_first_refresh, ConfigEntryAuthFailed aborts
            # setup and opens a reauth flow, and Home Assistant then stops
            # polling *permanently* -- no backoff, no retry, six sensors at
            # `unavailable` until somebody notices and clicks something. That is
            # the correct response to a password that is genuinely wrong and a
            # catastrophic one to a blip. srp_api.py now raises SRPAuthError
            # only for a 400/401 from login/authorize, the sole request that
            # submits a password; a 401 from a data endpoint is an expired
            # session, is repaired in the client, and arrives here (if at all)
            # as a retryable SRPApiError instead.
            raise ConfigEntryAuthFailed(str(err)) from err
        except SRPAccessBlocked as err:
            # Transient and not the user's fault; UpdateFailed retries quietly.
            raise UpdateFailed(str(err)) from err
        except SRPApiError as err:
            # Everything else, SRPSessionExpired included: retry on the next
            # poll. Sensors go stale for a few hours at worst and heal without
            # anyone being asked to do anything.
            raise UpdateFailed(f"Error talking to SRP: {err}") from err

        peaks = parse_cycle_peaks(daily_payload)
        demand = current_demand(daily_payload)
        daily = parse_daily(daily_payload)

        latest_day = max((d.day for d in daily), default=None)
        totals = {
            d.series: d.on_peak if d.series == "generation" else d.total
            for d in daily if latest_day is not None and d.day == latest_day
        }

        checked_at = datetime.now(PHOENIX)
        audit = build_demand_audit(
            daily_payload, [], checked_at.date() - timedelta(days=7),
            checked_at.date() - timedelta(days=1), checked_at,
            plan_code=getattr(self, "_demand_audit_plan", None),
            fetch_complete=False, fetch_issue="interval_fetch_not_completed",
        )
        try:
            audit = await self._async_update_statistics(daily_payload)
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
            demand_audit=audit,
        )

    # ------------------------------------------------------------ statistics

    async def _async_update_statistics(self, daily_payload: dict) -> dict:
        """Refresh settled intervals, including recent utility restatements."""
        start = await self._async_statistics_start()
        end = datetime.now(PHOENIX).date() - timedelta(days=1)
        if start > end:
            LOGGER.debug("statistics already current through %s", end)
            return build_demand_audit(
                daily_payload, [], start, end, datetime.now(PHOENIX),
                plan_code=getattr(self, "_demand_audit_plan", None), fetch_complete=False,
                fetch_issue="no_completed_interval_fetch",
            )

        LOGGER.debug("fetching intervals %s .. %s for statistics", start, end)
        intervals = await self._client.intervals(start, end)
        audit = build_demand_audit(
            daily_payload, getattr(self._client, "demand_audit_rows", []),
            start, end, datetime.now(PHOENIX),
            plan_code=getattr(self, "_demand_audit_plan", None),
            # Actual E-15 raw demandList boundary evidence, Nov 5 2026:
            # 05:00/05:30 on-peak; 09:00 off-peak. Cycle peak timestamps
            # are a separate field and must not shift these slot starts.
            timestamp_start_verified=True,
            fetch_complete=getattr(self._client, "demand_audit_fetch_complete", False),
        )
        if not intervals:
            return audit

        for series, (suffix, label) in _ENERGY_STATS.items():
            try:
                await self._async_write_energy(series, suffix, label, intervals)
            except SRPApiError as err:
                LOGGER.warning("statistics replay skipped for %s: %s", suffix, err)
        await self._async_write_demand(intervals)
        return audit

    async def _async_statistics_start(self) -> date:
        """Revisit recent settled days and gaps, or backfill a new installation.

        Consumption is the anchor because it is the one series every account
        has: generation and demand are empty on a plan without solar, so
        keying off the oldest series would refetch two years on every poll.
        A missing individual series regains only the recent fetched window;
        older missing history needs a controlled targeted backfill, preserving
        the existing statistic IDs and known cumulative baseline.
        """
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
        today = datetime.now(PHOENIX).date()
        if not rows:
            LOGGER.info("no SRP statistics yet; backfilling %s days", BACKFILL_DAYS)
            return today - timedelta(days=BACKFILL_DAYS)
        last_day = max(datetime.fromtimestamp(row["start"], PHOENIX).date() for row in rows)
        return min(last_day, today - timedelta(days=RESTATEMENT_DAYS))

    async def _async_write_energy(
        self, series: str, suffix: str, label: str, intervals: list[Interval]
    ) -> None:
        """Upsert revised hourly states and their complete cumulative tail."""
        cutoff = datetime.now(PHOENIX).replace(hour=0, minute=0, second=0, microsecond=0)
        values = _energy_values(series, intervals, cutoff)
        if not values:
            return
        statistic_id = self.statistic_id(suffix)
        # At most one stored row can exist per hour. This count includes every
        # possible settled hour since the first input PLUS a preceding point,
        # so it works across missing hours and retained tails without reading
        # unrelated older history. Native kWh avoids display-unit conversion.
        count = math.ceil((cutoff - min(values)).total_seconds() / 3600) + 1
        last = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, count, statistic_id, False, {"state", "sum"}
        )
        points = _rebuild_energy_statistics(values, (last or {}).get(statistic_id, []), cutoff)
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
        """Write half-hourly **on-peak** kW as an hourly mean/min/max statistic.

        The ``max`` column is the useful one: it preserves the highest half hour
        of each hour, which is the granularity SRP bills demand on. Averaging
        the two half-hours together -- which is all an hourly-resolution source
        can do -- understates the billed peak by however hard load ramped.

        Only the on-peak leg is stored. SRP publishes an off-peak demand figure
        too, and on a solar account it is the larger of the two by a wide margin
        -- 25.3 kW against a 12.7 kW billed peak over one measured cycle -- so a
        statistic that mixed them would look like a demand chart while showing a
        number no bill has ever contained.
        """
        buckets: dict[datetime, list[float]] = {}
        for row in intervals:
            if row.series != "demand":
                continue
            # On-peak only: that is the leg SRP bills, and the parser
            # drops the off-peak one for this series precisely so this
            # cannot silently become max(unbilled, billed).
            value = row.on_peak or 0.0
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
