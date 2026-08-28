"""Browser-free backend for SRP's private My Account JSON API.

The scraper in ``client.py`` drives the portal because the CSV export is a
contract SRP publishes to its customers. This module is the other half of that
trade: the same numbers, pulled from the undocumented JSON endpoints the
portal's own front-end calls, with no Chromium anywhere.

Both paths are kept because they fail differently. Markup changes break the
scraper; an unannounced field rename breaks this. Neither can break the parser
or the stored history, which is the point of the split.

Two things were measured rather than assumed, and both are load-bearing:

* **Long requests truncate silently.** ``usage/hourlydetail`` accepts any date
  range and returns HTTP 200, but anything wider than about 31 days comes back
  holding only the *most recent* 31 days. A two-year request returns one month
  and no error. Every range is therefore chunked by ``chunk_date_range`` before
  it goes near the wire -- see :data:`HOURLY_MAX_DAYS`.
* **The anti-forgery header is mandatory.** Without ``x-xsrf-token`` the usage
  endpoints answer 403, which is indistinguishable at a glance from the
  Cloudflare block described below. The token is a cookie set by
  ``login/antiforgerytoken`` and must be echoed back as a header.

Cloudflare fronts the API, and a 403 from it is not an authentication failure.
Observed on one network within the same hour: ``requests`` refused, ``curl``
refused, and this module's stdlib ``urllib`` transport let straight through --
while a second host answered every one of them. So the verdict varies by
client and by moment, not just by account, and the only safe reading of a 403
is "not this time, from here". :class:`AccessBlocked` reports that instead of
sending people to check a password that was never the problem; the Playwright
path in ``client.py`` is the fallback when it persists.

Layering, so this stays usable from an event loop as well as a CLI:

* the parsing and range-chunking helpers are pure -- dicts in, models out, no
  I/O, stdlib only
* :class:`SRPJsonClient` is a thin synchronous transport over
  ``urllib.request`` for the CLI

Anything that already has an HTTP client (a Home Assistant integration, say)
should call the pure helpers and do its own I/O rather than importing the
client. That is why the whole module adds no dependency to the package.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from http.cookiejar import CookieJar
from typing import Any

from .models import (
    BillProjection,
    CyclePeak,
    DailyReading,
    DemandObservation,
    IntervalReading,
    Series,
)

logger = logging.getLogger(__name__)

API_BASE = "https://myaccount.srpnet.com/myaccountapi/api/"

#: The widest window ``usage/hourlydetail`` will honour. Ask for more and it
#: returns the last 31 days with a 200 and no indication it dropped the rest.
HOURLY_MAX_DAYS = 31

#: Presented so Cloudflare serves the API rather than an interstitial. This is
#: presentation, not evasion of an access control -- every request below is
#: authenticated with the account holder's own credentials.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

_DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": API_BASE,
}


class SRPApiError(RuntimeError):
    """The JSON API refused or returned something unusable."""


class AuthError(SRPApiError):
    """SRP rejected the credentials."""


class AccessBlocked(SRPApiError):
    """Cloudflare refused the request before SRP saw it.

    Raised on a 403 carrying an "Access Denied" body. The credentials are not
    the problem: the same ones have been seen refused and accepted minutes
    apart depending on the host and the HTTP client. Retry, try another
    machine, or fall back to the Playwright path in ``client.py``.
    """


# --------------------------------------------------------------------------
# Series mapping
#
# SRP returns four parallel lists per response and names none of them the way
# its own CSV export does. The mapping below was established by fetching both
# paths for the same days and comparing every value:
#
#     CSV "usage"      == dailyConsumptionList / hourlyConsumptionList
#     CSV "generation" == dailyGenerationList  / hourlyGenerationList
#     CSV "net"        == dailyReadList        / hourlyUsageList
#     CSV "demand"     == dailyDemandList      / demandList
#
# The third row is the surprising one and it matters: the list SRP calls
# "usage" is net metered energy, not consumption. Anything summing
# hourlyUsageList and calling it consumption is reporting a different quantity
# than the one the bill's kWh line is built from.
# --------------------------------------------------------------------------

#: series -> (daily list key, hourly list key, field suffix)
_SERIES_KEYS: dict[Series, tuple[str, str, str]] = {
    Series.USAGE: ("dailyConsumptionList", "hourlyConsumptionList", ""),
    Series.GENERATION: ("dailyGenerationList", "hourlyGenerationList", ""),
    Series.NET: ("dailyReadList", "hourlyUsageList", "Kwh"),
    Series.DEMAND: ("dailyDemandList", "demandList", "Demand"),
}


def _peak_fields(suffix: str) -> tuple[str, str]:
    """Return the (off-peak, on-peak) field names for a list's naming scheme.

    SRP uses three: bare (``offPeak``), kWh-suffixed (``offPeakKwh``) and
    demand-suffixed (``offPeakDemand``).
    """
    if not suffix:
        return "offPeak", "onPeak"
    return f"offPeak{suffix}", f"onPeak{suffix}"


# ------------------------------------------------------------------ parsing


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_date(value: Any) -> date | None:
    """Parse any of SRP's date spellings to a local calendar date.

    Three forms appear in one response:

    * ``2026-08-25T00:00:00``          -- naive, already Arizona local
    * ``2026-08-26T07:00:00Z``         -- UTC, and 07:00Z *is* midnight MST
    * ``2026-08-27T00:00:00-07:00``    -- explicit Arizona offset

    In all three the first ten characters are the local date, because Arizona
    is UTC-7 year round and observes no daylight saving. Slicing is therefore
    correct here and would not be anywhere with a DST-observing timezone.
    """
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _as_naive_datetime(value: Any) -> datetime | None:
    """Parse an interval timestamp to naive Arizona local time.

    Interval timestamps arrive naive already. They are kept naive to match
    ``IntervalReading``, which documents its own timestamps as Arizona local --
    unambiguous only because the state has no DST.
    """
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1]
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=None)


def chunk_date_range(
    start: date, end: date, max_days: int = HOURLY_MAX_DAYS
) -> list[tuple[date, date]]:
    """Split an inclusive date range into windows the API will actually honour.

    Returns oldest window first. A range already inside the limit comes back as
    a single window, so callers can chunk unconditionally.

    This exists because the failure it prevents is silent: ``usage/hourlydetail``
    answers a two-year request with 200 OK and one month of data. Without
    chunking a backfill looks like it worked and quietly contains a fraction of
    the history it claims.
    """
    if max_days < 1:
        raise ValueError("max_days must be at least 1")
    if end < start:
        raise ValueError(f"end {end} is before start {start}")

    windows: list[tuple[date, date]] = []
    cursor = start
    step = timedelta(days=max_days - 1)
    while cursor <= end:
        window_end = min(cursor + step, end)
        windows.append((cursor, window_end))
        cursor = window_end + timedelta(days=1)
    return windows


def parse_hourly_detail(
    payload: dict[str, Any], series: Sequence[Series] | None = None
) -> list[IntervalReading]:
    """Turn a ``usage/hourlydetail`` response into interval readings.

    Energy series are hourly kWh; demand is half-hourly kW (SRP sets
    ``intervalTimeInMinutes`` to 30 on that list alone). Rows whose timestamp
    will not parse are skipped, but a row with a good timestamp and no value in
    either peak column is kept as a pair of Nones so the upsert's COALESCE
    leaves any previously stored value alone rather than blanking it.
    """
    wanted = list(series) if series else list(_SERIES_KEYS)
    out: list[IntervalReading] = []

    for name in wanted:
        _, hourly_key, suffix = _SERIES_KEYS[name]
        rows = payload.get(hourly_key) or []
        off_field, on_field = _peak_fields(suffix)
        for row in rows:
            start = _as_naive_datetime(row.get("date") or row.get("hour"))
            if start is None:
                continue
            out.append(
                IntervalReading(
                    series=name,
                    interval_start=start,
                    off_peak=_as_float(row.get(off_field)),
                    on_peak=_as_float(row.get(on_field)),
                )
            )
    return out


def parse_daily_usage_detail(
    payload: dict[str, Any],
    weather: dict[date, tuple[float | None, float | None]] | None = None,
    series: Sequence[Series] | None = None,
) -> list[DailyReading]:
    """Turn a ``usage/dailyusagedetail`` response into daily readings.

    One call covers the account's whole retained history -- over two years on
    the account this was built against -- so unlike the hourly endpoint it
    needs no chunking.

    ``weather`` is the optional ``usage/dailyweather`` result keyed by date; the
    CSV export carries high/low temperature on every row and this keeps the two
    paths producing identical records.
    """
    wanted = list(series) if series else list(_SERIES_KEYS)
    temps = weather or {}
    out: list[DailyReading] = []

    for name in wanted:
        daily_key, _, suffix = _SERIES_KEYS[name]
        rows = payload.get(daily_key) or []
        off_field, on_field = _peak_fields(suffix)
        for row in rows:
            usage_date = _as_date(row.get("date") or row.get("day"))
            if usage_date is None:
                continue
            high, low = temps.get(usage_date, (None, None))
            off = _as_float(row.get(off_field))
            on = _as_float(row.get(on_field))
            # The demand series has no meaningful off-peak leg: SRP reports an
            # off-peak demand figure but bills only the on-peak one, and the
            # CSV export omits it entirely. Dropping it keeps both backends
            # writing the same row.
            if name is Series.DEMAND:
                off = None
            out.append(
                DailyReading(
                    series=name,
                    usage_date=usage_date,
                    meter_read_date=_as_date(row.get("meterReadDate")),
                    off_peak=off,
                    on_peak=on,
                    high_temp_f=high,
                    low_temp_f=low,
                )
            )
    return out


def parse_daily_weather(payload: Any) -> dict[date, tuple[float | None, float | None]]:
    """Index a ``usage/dailyweather`` response by date as (high, low)."""
    out: dict[date, tuple[float | None, float | None]] = {}
    for row in payload or []:
        day = _as_date(row.get("weatherDate"))
        if day is None:
            continue
        out[day] = (_as_float(row.get("high")), _as_float(row.get("low")))
    return out


def parse_cycle_peaks(payload: dict[str, Any]) -> list[CyclePeak]:
    """Extract billed demand peaks from ``highDemandInfoList``.

    This is the figure the demand charge is actually computed from: the single
    highest on-peak half hour of the billing cycle, with the timestamp it
    happened. Nothing else in either backend captures it, and it is not
    derivable from hourly data -- an hourly average understates a half-hourly
    maximum by however hard the load ramped inside the hour.
    """
    is_average = bool(payload.get("isAverageDemandRate"))
    out: list[CyclePeak] = []
    for row in payload.get("highDemandInfoList") or []:
        start = _as_date(row.get("billStartDate"))
        end = _as_date(row.get("billEndDate"))
        if start is None or end is None:
            continue
        peak_kw = _as_float(row.get("onPeakHighDemand"))
        if peak_kw is None:
            continue
        out.append(
            CyclePeak(
                cycle_start=start,
                cycle_end=end,
                on_peak_high_demand_kw=peak_kw,
                occurred_at=_as_naive_datetime(row.get("onPeakHighDemandDate")),
                is_average_demand_rate=is_average,
            )
        )
    out.sort(key=lambda c: c.cycle_start)
    return out


def parse_demand_observation(
    payload: dict[str, Any], observed_at: datetime | None = None
) -> DemandObservation | None:
    """Reconstruct the headline demand figure the portal prints for this cycle.

    The scraper reads this number off the rendered page with its caption,
    because on the page the caption is the only thing telling a cycle mean from
    a cycle maximum. Here the plan type is a boolean -- ``isAverageDemandRate``
    -- so the right quantity can be *computed*, and mislabelling one as the
    other is not possible by accident.

    Which quantity, precisely:

    * **Peak-demand plan** (E-27; ``isAverageDemandRate`` false) -- the billed
      figure is the single highest on-peak half hour of the cycle, which SRP
      hands over directly in ``highDemandInfoList``.
    * **Average-demand plan** (E-15, E-16; ``isAverageDemandRate`` true) -- the
      billed figure is the *mean of the daily on-peak maxima*, and SRP does not
      publish it as a field. It is reconstructed here as the mean of
      ``dailyDemandList`` over the cycle's **weekdays**: weekends carry no
      on-peak demand, so including their zeros halves the answer.

    The reconstruction was checked against the number the portal itself
    displayed for the 2026-08-10 cycle: computed 5.62 kW against a printed
    5.60 kW. The residual is the daily figures being published to one decimal.
    Treat an average-plan value as accurate to about ±0.05 kW, and the scraped
    figure as authoritative if you have both.

    Returns ``None`` when the payload carries no cycle information at all.
    """
    peaks = parse_cycle_peaks(payload)
    if not peaks:
        return None
    cycle = peaks[-1]
    stamp = observed_at or datetime.now().replace(microsecond=0)

    if not cycle.is_average_demand_rate:
        return DemandObservation(
            observed_at=stamp,
            demand_kw=cycle.on_peak_high_demand_kw,
            label="Highest On-Peak Demand",
            source="usage/dailyusagedetail:highDemandInfoList",
        )

    daily = parse_daily_usage_detail(payload, series=[Series.DEMAND])
    weekday_maxima = [
        r.on_peak
        for r in daily
        if cycle.cycle_start <= r.usage_date <= cycle.cycle_end
        and r.usage_date.weekday() < 5
        and r.on_peak is not None
    ]
    if not weekday_maxima:
        return None
    return DemandObservation(
        observed_at=stamp,
        demand_kw=round(sum(weekday_maxima) / len(weekday_maxima), 2),
        label="Current Average On-Peak Demand",
        source="usage/dailyusagedetail:dailyDemandList (weekday mean)",
    )


def parse_bill_projection(
    payload: dict[str, Any], observed_at: datetime | None = None
) -> BillProjection | None:
    """Turn a ``payments/billprojection`` response into a projection record."""
    if not payload or not payload.get("hasProjection"):
        return None
    amount = _as_float(payload.get("amount"))
    if amount is None:
        return None
    month = payload.get("month")
    basis = f"Projected bill for {month}." if month else None
    return BillProjection(
        observed_at=observed_at or datetime.now().replace(microsecond=0),
        amount_usd=amount,
        basis=basis,
        source="payments/billprojection",
    )


# ---------------------------------------------------------------- transport


@dataclass(slots=True)
class _Response:
    status: int
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


class SRPJsonClient:
    """Synchronous JSON client over stdlib ``urllib``.

    Deliberately not async and deliberately dependency-free: it exists so the
    CLI can use this backend without the package growing a runtime dependency.
    Callers already inside an event loop should use the pure parse helpers with
    their own HTTP client instead.

    Usage::

        with SRPJsonClient(username, password, account_id) as srp:
            daily = srp.daily_usage_detail()
            intervals = srp.hourly_detail(date(2026, 8, 1), date(2026, 8, 26))
    """

    def __init__(
        self,
        username: str,
        password: str,
        account_id: str,
        *,
        timeout: float = 60.0,
        base_url: str = API_BASE,
    ) -> None:
        if not username or not password:
            raise ValueError("SRP username and password are both required")
        if not account_id:
            raise ValueError("SRP account id is required")
        self.username = username
        self.password = password
        self.account_id = str(account_id)
        self.timeout = timeout
        self.base_url = base_url.rstrip("/") + "/"

        self._jar = CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._jar))
        self._xsrf: str | None = None

    def __enter__(self) -> SRPJsonClient:
        self.login()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._opener.close()

    # -- plumbing ---------------------------------------------------------

    def _request(
        self, path: str, *, data: bytes | None = None, params: dict[str, str] | None = None
    ) -> _Response:
        url = self.base_url + path.lstrip("/")
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        headers = dict(_DEFAULT_HEADERS)
        if self._xsrf:
            headers["x-xsrf-token"] = self._xsrf
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                return _Response(status=resp.status, body=resp.read())
        except urllib.error.HTTPError as exc:
            body = exc.read()
            self._raise_for_error(exc.code, body, path)
            raise SRPApiError(f"{path} failed: HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise SRPApiError(f"{path} failed: {exc.reason}") from exc

    @staticmethod
    def _raise_for_error(status: int, body: bytes, path: str) -> None:
        text = body.decode("utf-8", "replace")
        if status == 403 and "access denied" in text.lower():
            raise AccessBlocked(
                f"Cloudflare refused {path} (403 Access Denied). Your credentials "
                "are not the problem -- this varies by host, HTTP client and "
                "moment. Retry, try another machine, or use the browser backend "
                "(`srp-energy-monitor fetch`), which gets through when this does not."
            )
        if status in (400, 401):
            raise AuthError(f"SRP rejected the credentials on {path}: {text[:200]}")
        raise SRPApiError(f"{path} failed: HTTP {status}: {text[:200]}")

    def _cookie(self, name: str) -> str | None:
        for cookie in self._jar:
            if cookie.name == name and cookie.value:
                return urllib.parse.unquote(cookie.value)
        return None

    # -- session ----------------------------------------------------------

    def login(self) -> None:
        """Authenticate and capture the anti-forgery token.

        Two steps, in this order: ``login/authorize`` establishes the session
        cookie, then ``login/antiforgerytoken`` sets an ``xsrf-token`` cookie
        that every usage endpoint requires echoed back as a header. Skipping
        the second step yields 403s that read like an access problem.
        """
        logger.info("logging in to the SRP JSON API as %s", self.username)
        body = urllib.parse.urlencode(
            {"username": self.username, "password": self.password}
        ).encode()
        self._request("login/authorize", data=body)

        self._request("login/antiforgerytoken")
        self._xsrf = self._cookie("xsrf-token")
        if not self._xsrf:
            raise SRPApiError(
                "no xsrf-token cookie after login/antiforgerytoken; "
                "the usage endpoints will refuse every request"
            )
        logger.info("SRP JSON login successful")

    # -- endpoints --------------------------------------------------------

    def daily_usage_detail(self) -> dict[str, Any]:
        """The whole retained daily history in one call, plus cycle peaks."""
        return self._request(
            "usage/dailyusagedetail", params={"billaccount": self.account_id}
        ).json()

    def daily_weather(self) -> dict[date, tuple[float | None, float | None]]:
        return parse_daily_weather(self._request("usage/dailyweather").json())

    def bill_projection(self) -> dict[str, Any]:
        return self._request(
            "payments/billprojection", params={"billaccount": self.account_id}
        ).json()

    def hourly_detail_window(self, start: date, end: date) -> dict[str, Any]:
        """One window of interval data. Callers should prefer ``hourly_detail``.

        No chunking happens here, so a range wider than
        :data:`HOURLY_MAX_DAYS` silently returns only its tail.
        """
        return self._request(
            "usage/hourlydetail",
            params={
                "billaccount": self.account_id,
                "beginDate": start.strftime("%m-%d-%Y"),
                "endDate": end.strftime("%m-%d-%Y"),
            },
        ).json()

    def iter_hourly_detail(self, start: date, end: date) -> Iterator[dict[str, Any]]:
        """Yield one response per honoured window, oldest first."""
        for window_start, window_end in chunk_date_range(start, end):
            logger.info("fetching intervals %s .. %s", window_start, window_end)
            yield self.hourly_detail_window(window_start, window_end)

    def hourly_detail(
        self, start: date, end: date, series: Sequence[Series] | None = None
    ) -> list[IntervalReading]:
        """Interval readings across an arbitrarily wide range, chunked safely."""
        out: list[IntervalReading] = []
        for payload in self.iter_hourly_detail(start, end):
            out.extend(parse_hourly_detail(payload, series))
        return out
