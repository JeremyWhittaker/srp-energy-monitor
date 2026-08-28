"""Async client for SRP's private My Account JSON API.

A Home Assistant custom integration cannot depend on this project's PyPI
package -- it is not published, and HAOS runs on musl where an unavailable
wheel drops the config entry into SETUP_RETRY. So the parsing rules live here
too, in a file with no imports outside the standard library and aiohttp (which
Home Assistant already ships).

That duplication is deliberate and it is guarded: ``tests/test_component_parity.py``
runs this module and ``srp_energy.jsonapi`` over the same fixtures and fails if
they ever disagree about a number. Change one, change both.

The three behaviours worth knowing before editing, all measured against the
live API rather than assumed:

* Ranges wider than :data:`HOURLY_MAX_DAYS` come back truncated to the most
  recent month, with HTTP 200 and no warning. Everything goes through
  :func:`chunk_date_range`.
* Every usage endpoint needs the ``x-xsrf-token`` header echoed from the cookie
  ``login/antiforgerytoken`` sets. Without it they answer 403, which reads
  exactly like the Cloudflare block.
* A 403 is not an authentication failure. The same credentials have been
  refused and accepted minutes apart depending on host and HTTP client.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import aiohttp
from yarl import URL

API_BASE = URL("https://myaccount.srpnet.com/myaccountapi/api/")

#: The widest window ``usage/hourlydetail`` honours before silently truncating.
HOURLY_MAX_DAYS = 31

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=90)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": str(API_BASE),
}

# SRP's list names do not match the series names its own CSV export uses.
# Established by fetching both paths for the same days and diffing every value.
# The third entry is the one that bites: the list called "usage" is NET metered
# energy. Summing it and calling it consumption reports a different quantity
# than the bill's kWh line -- about 8 kWh/day apart on a solar account.
SERIES_KEYS: dict[str, tuple[str, str, str]] = {
    "usage": ("dailyConsumptionList", "hourlyConsumptionList", ""),
    "generation": ("dailyGenerationList", "hourlyGenerationList", ""),
    "net": ("dailyReadList", "hourlyUsageList", "Kwh"),
    "demand": ("dailyDemandList", "demandList", "Demand"),
}


class SRPApiError(Exception):
    """The JSON API refused or returned something unusable."""


class SRPAuthError(SRPApiError):
    """SRP rejected the credentials. This one really is the password."""


class SRPAccessBlocked(SRPApiError):
    """Cloudflare refused the request before SRP saw it.

    Not an authentication problem. Retrying later, or from elsewhere, often
    succeeds with the identical credentials.
    """


@dataclass(frozen=True, slots=True)
class CyclePeak:
    """The demand figure one billing cycle was charged on."""

    cycle_start: date
    cycle_end: date
    on_peak_high_demand_kw: float
    occurred_at: datetime | None
    is_average_demand_rate: bool


@dataclass(frozen=True, slots=True)
class Interval:
    """One metered interval: hourly for energy, half-hourly for demand."""

    series: str
    start: datetime
    off_peak: float | None
    on_peak: float | None

    @property
    def total(self) -> float:
        return (self.off_peak or 0.0) + (self.on_peak or 0.0)


@dataclass(frozen=True, slots=True)
class DailyValue:
    """One day of one series."""

    series: str
    day: date
    off_peak: float | None
    on_peak: float | None

    @property
    def total(self) -> float:
        return (self.off_peak or 0.0) + (self.on_peak or 0.0)


# ------------------------------------------------------------------ helpers


def _peak_fields(suffix: str) -> tuple[str, str]:
    if not suffix:
        return "offPeak", "onPeak"
    return f"offPeak{suffix}", f"onPeak{suffix}"


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_date(value: Any) -> date | None:
    """First ten characters are the local date in all of SRP's date spellings.

    Safe only because Arizona is UTC-7 year round: ``2026-08-26T07:00:00Z`` is
    midnight MST on the same calendar day. This would be wrong anywhere that
    observes daylight saving.
    """
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _as_datetime(value: Any) -> datetime | None:
    """Parse an interval timestamp to naive Arizona local time."""
    if not value:
        return None
    text = str(value).removesuffix("Z")
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        return None


def chunk_date_range(
    start: date, end: date, max_days: int = HOURLY_MAX_DAYS
) -> list[tuple[date, date]]:
    """Split an inclusive range into windows the API will honour, oldest first.

    Guards a silent failure: a two-year request returns 200 OK carrying one
    month, so an unchunked backfill looks complete and is not.
    """
    if max_days < 1:
        raise ValueError("max_days must be at least 1")
    if end < start:
        raise ValueError(f"end {end} is before start {start}")
    windows: list[tuple[date, date]] = []
    cursor, step = start, timedelta(days=max_days - 1)
    while cursor <= end:
        window_end = min(cursor + step, end)
        windows.append((cursor, window_end))
        cursor = window_end + timedelta(days=1)
    return windows


# ------------------------------------------------------------------ parsing


def parse_intervals(payload: dict[str, Any], series: list[str] | None = None) -> list[Interval]:
    """Interval readings from a ``usage/hourlydetail`` response."""
    out: list[Interval] = []
    for name in series or list(SERIES_KEYS):
        _, hourly_key, suffix = SERIES_KEYS[name]
        off_field, on_field = _peak_fields(suffix)
        for row in payload.get(hourly_key) or []:
            start = _as_datetime(row.get("date") or row.get("hour"))
            if start is None:
                continue
            out.append(
                Interval(
                    series=name,
                    start=start,
                    off_peak=_as_float(row.get(off_field)),
                    on_peak=_as_float(row.get(on_field)),
                )
            )
    return out


def parse_daily(payload: dict[str, Any], series: list[str] | None = None) -> list[DailyValue]:
    """Daily values from a ``usage/dailyusagedetail`` response."""
    out: list[DailyValue] = []
    for name in series or list(SERIES_KEYS):
        daily_key, _, suffix = SERIES_KEYS[name]
        off_field, on_field = _peak_fields(suffix)
        for row in payload.get(daily_key) or []:
            day = _as_date(row.get("date") or row.get("day"))
            if day is None:
                continue
            off = _as_float(row.get(off_field))
            # SRP reports an off-peak demand figure but bills only the on-peak
            # one, and its own CSV export omits it entirely.
            out.append(
                DailyValue(
                    series=name,
                    day=day,
                    off_peak=None if name == "demand" else off,
                    on_peak=_as_float(row.get(on_field)),
                )
            )
    return out


def parse_cycle_peaks(payload: dict[str, Any]) -> list[CyclePeak]:
    """Billed demand peaks, oldest cycle first."""
    is_average = bool(payload.get("isAverageDemandRate"))
    out: list[CyclePeak] = []
    for row in payload.get("highDemandInfoList") or []:
        start, end = _as_date(row.get("billStartDate")), _as_date(row.get("billEndDate"))
        peak = _as_float(row.get("onPeakHighDemand"))
        if start is None or end is None or peak is None:
            continue
        out.append(
            CyclePeak(
                cycle_start=start,
                cycle_end=end,
                on_peak_high_demand_kw=peak,
                occurred_at=_as_datetime(row.get("onPeakHighDemandDate")),
                is_average_demand_rate=is_average,
            )
        )
    out.sort(key=lambda c: c.cycle_start)
    return out


def current_demand(payload: dict[str, Any]) -> tuple[float, bool] | None:
    """The headline demand figure for the open cycle, and whether it is a mean.

    Which quantity depends on the plan, and getting this backwards misreports
    the number that drives the demand charge:

    * peak plan (E-27) -- the cycle maximum, published directly
    * average plan (E-15, E-16) -- the mean of the daily on-peak maxima, which
      SRP does *not* publish, reconstructed here over the cycle's **weekdays**.
      Weekends carry no on-peak demand, so averaging their zeros in halves the
      answer.

    Reconstruction was checked against the portal's own printed figure: 5.62 kW
    computed against 5.60 displayed, the residual being daily values published
    to one decimal.
    """
    peaks = parse_cycle_peaks(payload)
    if not peaks:
        return None
    cycle = peaks[-1]
    if not cycle.is_average_demand_rate:
        return cycle.on_peak_high_demand_kw, False

    maxima = [
        d.on_peak
        for d in parse_daily(payload, ["demand"])
        if cycle.cycle_start <= d.day <= cycle.cycle_end
        and d.day.weekday() < 5
        and d.on_peak is not None
    ]
    if not maxima:
        return None
    return round(sum(maxima) / len(maxima), 2), True


def parse_projection(payload: dict[str, Any]) -> float | None:
    """The projected bill amount, or None when SRP has not produced one."""
    if not payload or not payload.get("hasProjection"):
        return None
    return _as_float(payload.get("amount"))


# ---------------------------------------------------------------- transport


class SRPClient:
    """Async JSON client using Home Assistant's shared aiohttp session."""

    def __init__(
        self, session: aiohttp.ClientSession, username: str, password: str, account_id: str
    ) -> None:
        self._session = session
        self._username = username
        self._password = password
        self._account = account_id
        self._xsrf: str | None = None
        self._lock = asyncio.Lock()

    async def _request(
        self, path: str, *, data: dict[str, str] | None = None, params: dict[str, str] | None = None
    ) -> Any:
        headers = dict(_HEADERS)
        if self._xsrf:
            headers["x-xsrf-token"] = self._xsrf
        try:
            async with self._session.request(
                "POST" if data else "GET",
                API_BASE / path,
                data=data,
                params=params,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    self._raise(resp.status, body, path)
                try:
                    return await resp.json(content_type=None)
                except ValueError as exc:
                    raise SRPApiError(f"{path} returned non-JSON: {body[:200]}") from exc
        except TimeoutError as exc:
            raise SRPApiError(f"{path} timed out") from exc
        except aiohttp.ClientError as exc:
            raise SRPApiError(f"{path} failed: {exc}") from exc

    @staticmethod
    def _raise(status: int, body: str, path: str) -> None:
        if status == 403 and "access denied" in body.lower():
            raise SRPAccessBlocked(
                f"Cloudflare refused {path} (403). Your credentials are not the "
                "problem -- this varies by host, client and moment. It usually "
                "clears on its own."
            )
        if status in (400, 401):
            raise SRPAuthError(f"SRP rejected the credentials on {path}")
        raise SRPApiError(f"{path} failed: HTTP {status}: {body[:200]}")

    async def login(self) -> None:
        """Authenticate, then capture the anti-forgery token.

        Both steps are required: without the second, every usage endpoint
        answers 403 in a way that looks like an access problem.
        """
        await self._request(
            "login/authorize", data={"username": self._username, "password": self._password}
        )
        await self._request("login/antiforgerytoken")
        cookies = self._session.cookie_jar.filter_cookies(API_BASE)
        token = cookies.get("xsrf-token")
        if token is None or not token.value:
            raise SRPApiError(
                "no xsrf-token cookie after login; the usage endpoints will refuse every request"
            )
        self._xsrf = URL(token.value).human_repr() if "%" in token.value else token.value

    async def _ensure_session(self) -> None:
        if self._xsrf is None:
            await self.login()

    async def validate(self) -> bool:
        """Round-trip the credentials. Used by the config flow."""
        async with self._lock:
            await self.login()
            return True

    async def daily_detail(self) -> dict[str, Any]:
        """The account's whole retained daily history, plus cycle peaks."""
        async with self._lock:
            await self._ensure_session()
            return await self._request(
                "usage/dailyusagedetail", params={"billaccount": self._account}
            )

    async def projection(self) -> dict[str, Any]:
        async with self._lock:
            await self._ensure_session()
            return await self._request(
                "payments/billprojection", params={"billaccount": self._account}
            )

    async def intervals(self, start: date, end: date) -> list[Interval]:
        """Interval readings over any range, chunked so nothing is dropped."""
        out: list[Interval] = []
        async with self._lock:
            await self._ensure_session()
            for window_start, window_end in chunk_date_range(start, end):
                payload = await self._request(
                    "usage/hourlydetail",
                    params={
                        "billaccount": self._account,
                        "beginDate": window_start.strftime("%m-%d-%Y"),
                        "endDate": window_end.strftime("%m-%d-%Y"),
                    },
                )
                out.extend(parse_intervals(payload))
                # A backfill is ~25 windows against someone else's site.
                await asyncio.sleep(0.5)
        return out
