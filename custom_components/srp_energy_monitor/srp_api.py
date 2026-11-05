"""Async client for SRP's private My Account JSON API.

A Home Assistant custom integration cannot depend on this project's PyPI
package -- it is not published, and HAOS runs on musl where an unavailable
wheel drops the config entry into SETUP_RETRY. So the parsing rules live here
too, in a file with no imports outside the standard library and aiohttp (which
Home Assistant already ships).

That duplication is deliberate and it is guarded: ``tests/test_component_parity.py``
runs this module and ``srp_energy.jsonapi`` over the same fixtures and fails if
they ever disagree about a number. Change one, change both.

The four behaviours worth knowing before editing, all measured against the
live API rather than assumed:

* Ranges wider than :data:`HOURLY_MAX_DAYS` come back truncated to the most
  recent month, with HTTP 200 and no warning. Everything goes through
  :func:`chunk_date_range`.
* Every usage endpoint needs the ``x-xsrf-token`` header echoed from the cookie
  ``login/antiforgerytoken`` sets. Without it they answer 403, which reads
  exactly like the Cloudflare block.
* A 403 is not an authentication failure. The same credentials have been
  refused and accepted minutes apart depending on host and HTTP client.
* **No status code, on any path, is by itself an authentication failure.**
  This one cost eighteen hours of dead sensors on 2026-09-02. Home Assistant
  restarted, the first refresh got 401 from ``usage/dailyusagedetail``, the
  client reported that as a bad password, the coordinator turned it into
  ``ConfigEntryAuthFailed``, and Home Assistant stopped polling *permanently*
  while it waited for a reauth the user never saw. The password was
  byte-for-byte correct the whole time.

  What a 401 from a data endpoint actually means is that the ``.AspNet.MyAccount``
  session cookie is gone or stale. Proven by calling ``usage/dailyusagedetail``
  with no session at all: 401 ``{"message":"Authorization has been denied for
  this request."}`` -- the exact response a genuinely wrong password never
  produces. A wrong password fails earlier, elsewhere, and in different words:
  at ``login/authorize``, with 400 ``{"error":"invalid_grant", ...}``.

  So the verdict "the password is wrong" is reached only when SRP says so, in
  its own words -- ``invalid_grant``, on ``login/authorize``. Everything else
  is retryable, including an unexplained 400 or 401 from ``login/authorize``
  itself, which is what a rate limit, a WAF rule, a maintenance window or a
  temporary lockout looks like. Those heal on their own; a wrongly latched
  ``ConfigEntryAuthFailed`` does not. See :meth:`SRPClient._raise`.
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

#: The one endpoint that checks a username and a password, and therefore the
#: only path on which a credential verdict can honestly be reached at all. It is
#: necessary but not sufficient: the body has to carry :data:`_INVALID_GRANT`
#: too. Everywhere else, and here without those words, a 4xx means something the
#: user cannot fix by retyping anything.
LOGIN_PATH = "login/authorize"

#: The second half of the login handshake. It needs no credentials of its own
#: (it rides the cookie ``login/authorize`` just set), so a failure here is a
#: broken handshake, never a bad password. Named so :meth:`SRPClient._request`
#: can refuse to start a re-login in response to a request that *is* a login.
ANTIFORGERY_PATH = "login/antiforgerytoken"

#: SRP's OAuth-flavoured spelling of "that password is wrong". Present in the
#: body of the 400 from ``login/authorize`` and nowhere else, which makes it the
#: definitive signal rather than an inference from a status code -- and so this
#: module *requires* it before it will blame anybody's password. Observed live:
#: ``{"error":"invalid_grant","error_description":"The user name or password is
#: incorrect."}``
_INVALID_GRANT = "invalid_grant"

#: Indirection over ``asyncio.sleep`` purely so a test can replace the politeness
#: delay between interval windows without reaching into the stdlib. Patching
#: ``srp_api.asyncio`` -- which is the real :mod:`asyncio` module, shared with
#: every other importer in the process -- worked by accident and leaked into
#: anything else running in the same interpreter.
_SLEEP = asyncio.sleep

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
    """SRP rejected the credentials. This one really is the password.

    Raised *only* when :data:`LOGIN_PATH` -- the one request in the whole API
    that submits a username and a password -- comes back 400 or 401 **and** the
    body carries :data:`_INVALID_GRANT`, SRP's own words for a bad password.

    The narrowness is the point. The coordinator escalates this to
    ``ConfigEntryAuthFailed``, which stops polling until a human retypes the
    password, so anything raised here must be genuinely unfixable by retrying.
    Widen this class and you re-create the outage described at the top of this
    module.
    """


class SRPSessionExpired(SRPApiError):
    """The session cookie is gone or stale. Log in again and try once more.

    :meth:`SRPClient._request` repairs this for a data endpoint, so most of
    these never reach a caller. Some do, and the docstring used to claim
    otherwise. ``login/antiforgerytoken`` is the leg that produces them: it
    carries no credentials, so a 401 there is not a login failure and is not
    special-cased, and :meth:`SRPClient._login` runs on
    :meth:`SRPClient._send`, which has no retry. Two ways out from there:

    * :meth:`SRPClient._ensure_session` calls :meth:`SRPClient._login` before
      :meth:`SRPClient._request` is ever involved, so one escapes straight to
      the public method's caller.
    * the repair inside :meth:`SRPClient._request` calls
      :meth:`SRPClient._login` too, and does not wrap it, so one escapes that
      method as well.

    Which is why this is a subclass of :class:`SRPApiError` and deliberately
    *not* of :class:`SRPAuthError`: whatever route it escapes by, the
    coordinator maps it to ``UpdateFailed`` and Home Assistant retries on the
    next poll. Failing retryably is the whole point.
    """


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
            off = _as_float(row.get(off_field))
            # Off-peak demand is published but never billed -- and on this
            # account it runs about twice the on-peak figure, so keeping it
            # invites a statistic that reports 25 kW for a 12.7 kW peak.
            out.append(
                Interval(
                    series=name,
                    start=start,
                    off_peak=None if name == "demand" else off,
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
    """Reported cycle maxima, oldest first; average plans bill a separate mean."""
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


def _observed_holiday(day: date) -> date:
    """Observe a Saturday holiday on Friday and a Sunday holiday on Monday."""
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _is_srp_on_peak_day(day: date) -> bool:
    """E-15/E-16 weekdays excluding the six SRP holidays (2026 ratebook)."""
    if day.weekday() >= 5:
        return False
    year = day.year
    holidays = {
        _observed_holiday(date(year, 1, 1)),
        _observed_holiday(date(year, 7, 4)),
        _observed_holiday(date(year, 12, 25)),
    }
    # A Saturday New Year's Day is observed in the preceding calendar year.
    if year < date.max.year:
        holidays.add(_observed_holiday(date(year + 1, 1, 1)))
    may_end = date(year, 5, 31)
    september_start = date(year, 9, 1)
    november_start = date(year, 11, 1)
    holidays.update(
        {
            may_end - timedelta(days=may_end.weekday()),
            september_start + timedelta(days=(-september_start.weekday()) % 7),
            november_start + timedelta(days=(3 - november_start.weekday()) % 7 + 21),
        }
    )
    return day not in holidays


def current_demand(payload: dict[str, Any]) -> tuple[float, bool] | None:
    """The headline demand figure for the open cycle, and whether it is a mean.

    Which quantity depends on the plan, and getting this backwards misreports
    the number that drives the demand charge:

    * peak plan (E-27) -- the cycle maximum, published directly
    * average plan (E-15, E-16) -- the mean of the daily on-peak maxima (30-minute
      demand on E-15, 60-minute on E-16). SRP does *not* publish the mean, so it
      is reconstructed from its daily maxima over the cycle's on-peak
      weekdays, excluding SRP's six holidays. Ordinary weekday zeros count.
      This denominator follows the tariff's on-peak definition and demand
      calculation; no holiday-containing cycle has yet been reconciled.

    Reconstruction was checked against the portal's own printed figure: 5.62 kW
    computed against 5.60 displayed in the non-holiday 2026-08-10 cycle. Daily
    values are published to one decimal, consistent with the rounding gap.
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
        and _is_srp_on_peak_day(d.day)
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
    """Async JSON client over an aiohttp session this integration owns.

    The session must not be Home Assistant's shared one. See the comment in
    ``coordinator.py`` where it is built: SRP's whole notion of "logged in" is a
    single cookie, and putting that cookie in a jar every other integration also
    writes to is how it went missing in the first place.

    Concurrency is one lock, held by the public methods for the length of a
    logical operation. ``asyncio.Lock`` is **not** reentrant, so everything
    below the public surface -- ``_request``, ``login``, ``_send`` -- runs
    lock-free and assumes the caller already holds it.
    """

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
        """One API call, repairing an expired session at most once.

        Why the retry lives here rather than in the coordinator: a lost session
        cookie is invisible from the outside. The caller asked for daily usage,
        not for a login, and re-logging in is a repair the caller should never
        have to know about. Doing it a layer up would mean teaching every call
        site the difference between "SRP said no" and "our cookie went stale".

        Two structural properties keep the failure modes here -- a deadlock and
        an infinite loop -- out of reach, and neither is a runtime check:

        * **No recursion.** The retry calls :meth:`_send` directly, and
          :meth:`_login` does too. Nothing on the re-login path can re-enter
          this method, so "at most once" is a property of the shape of the code
          rather than of a counter someone has to remember to decrement.
        * **No second acquire of** ``self._lock``. The public methods hold the
          lock across their whole operation and it is not reentrant, so a
          re-login triggered from in here must not reach for it. It does not:
          :meth:`_login` is lock-free, asserts as much, and is called from
          :meth:`validate` only while the lock is already held.

        The ``path in (LOGIN_PATH, ANTIFORGERY_PATH)`` branch below is a
        consequence of the first property rather than an enforcer of it: because
        :meth:`_login` goes through :meth:`_send`, no login request ever reaches
        this method today, and that branch is currently unreachable. It is kept
        as belt and braces for a future caller who routes a login through here
        by mistake -- it would be a cheap thing to delete and an expensive thing
        to have to rediscover -- but it is not what makes the code safe, and no
        test can cover it while it stays unreachable.

        A second consecutive 401 is reported as a plain :class:`SRPApiError`.
        That is a retryable failure -- Home Assistant backs off and tries again
        on the next poll -- and specifically not a :class:`SRPAuthError`, which
        would suspend polling until a human intervened.
        """
        try:
            return await self._send(path, data=data, params=params)
        except SRPSessionExpired:
            # Belt and braces, unreachable today: _login uses _send, so no login
            # request arrives here. If one ever did, it could not repair itself
            # by logging in again -- that recurses -- so let it out. It is
            # retryable as it stands. See this method's docstring.
            if path in (LOGIN_PATH, ANTIFORGERY_PATH):
                raise

        # The cookie is stale or gone. Drop the cached token so nothing can use
        # it, establish a fresh session, and replay the one request that failed.
        self._xsrf = None
        await self._login()
        try:
            return await self._send(path, data=data, params=params)
        except SRPSessionExpired as exc:
            raise SRPApiError(
                f"{path} returned 401 again immediately after a successful "
                "re-login. The session is not sticking -- this is SRP's side or "
                "the network's, not the password. Home Assistant will retry."
            ) from exc

    async def _send(
        self, path: str, *, data: dict[str, str] | None = None, params: dict[str, str] | None = None
    ) -> Any:
        """A single HTTP round trip with no retry and no lock of its own."""
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
        """Turn an HTTP error into the narrowest exception that fits.

        Ordered from most specific to least, and the ordering matters:

        1. 403 carrying "access denied" -- Cloudflare, not SRP. Unchanged.
        2. 400 or 401 from :data:`LOGIN_PATH` **whose body says invalid_grant**
           -- the password, in SRP's own words, on the only request that
           submits one. The single non-retryable verdict this client issues.
        3. 400 or 401 from :data:`LOGIN_PATH` *without* those words -- a login
           refused for a reason nobody has identified. Retryable, deliberately.
        4. 401 from anywhere else -- an expired session, repaired upstream.
        5. everything else: a 400 from a data endpoint (a malformed request
           rather than a rejected identity), a 5xx, a 404.

        Rule 2 used to be ``path == LOGIN_PATH and status in (400, 401)``, with
        no look at the body. That was the last route by which a *transient*
        failure could kill the integration permanently, and it is a live route,
        not a hypothetical: on a fresh Home Assistant start ``_xsrf`` is None, so
        :meth:`_ensure_session` goes straight to :meth:`_login` with no data
        request at all. A rate limit, a WAF rule, a maintenance window or a
        temporary lockout answering that login with a bare 400 or 401 became
        ``SRPAuthError`` -> ``ConfigEntryAuthFailed`` -> polling stopped for
        good. That is the exact shape of the 2026-09-02 outage, one layer down.

        Requiring the ``invalid_grant`` body costs nothing in coverage: a
        genuinely wrong password *always* produces it, so the real case is still
        caught. The previous author's stated worry was the opposite risk --
        retrying a wrong password forever, which is how accounts get locked --
        and that worry does not survive this gate, because a wrong password is
        still classified as a wrong password on the first try and never retried.
        What retries now is only an **unrecognised** login failure, which is the
        safer default of the two: the cost asymmetry is stark. A false "auth
        failed" cost eighteen hours of dead sensors and needed a human to
        notice. A false "retry" costs one extra request every four hours.
        """
        lowered = body.lower()
        if status == 403 and "access denied" in lowered:
            raise SRPAccessBlocked(
                f"Cloudflare refused {path} (403). Your credentials are not the "
                "problem -- this varies by host, client and moment. It usually "
                "clears on its own."
            )
        if path == LOGIN_PATH and status in (400, 401):
            if _INVALID_GRANT in lowered:
                raise SRPAuthError(
                    f"SRP says the username or password is incorrect (invalid_grant) on {path}"
                )
            # Refused, but not in the words SRP uses for a bad password. Handled
            # here rather than falling through to the 401 branch below purely so
            # the message is true: a login request has no session to expire, and
            # saying it does would send the next person debugging this down the
            # wrong path. The class is what matters, and it is retryable.
            raise SRPApiError(
                f"{path} refused the login with HTTP {status} and no invalid_grant "
                f"in the body: {body[:200]}. A rate limit, a WAF rule, a "
                "maintenance window or a temporary lockout all look like this. "
                "Not a credential failure -- Home Assistant will retry."
            )
        if status == 401:
            raise SRPSessionExpired(
                f"{path} answered 401: the SRP session has expired or was "
                "evicted. This is not a credential failure."
            )
        raise SRPApiError(f"{path} failed: HTTP {status}: {body[:200]}")

    async def _login(self) -> None:
        """Authenticate, then capture the anti-forgery token.

        Both steps are required: without the second, every usage endpoint
        answers 403 in a way that looks like an access problem.

        Private, and named so. It used to be ``login()``, which read like a
        public entry point while carrying a contract only its docstring
        mentioned -- the sort of mismatch that gets discovered by a deadlock.
        The public surface for "prove these credentials work" is
        :meth:`validate`; everything under it, this included, is lock-free.

        Two contracts, and the type system enforces neither:

        * **The caller holds** ``self._lock``. This method never takes it. That
          is what lets :meth:`_request` re-login mid-operation without
          deadlocking on a lock the public method already owns. The name now
          says so, and the assertion below actually checks it -- a convention
          is not a guard, and the failure it would otherwise cause is a hang
          rather than a traceback, which is the worst kind to debug.
        * **It calls** :meth:`_send`, **never** :meth:`_request`. A 401 during
          login must not trigger a login; going through ``_request`` would set
          up exactly that loop.
        """
        # asyncio.Lock cannot report *who* holds it, so this cannot distinguish
        # our own caller from another task. It does not need to: the mistake
        # worth catching is calling this from outside the public surface
        # entirely, and that leaves the lock unheld.
        assert self._lock.locked(), (
            "SRPClient._login requires the caller to hold self._lock; it is "
            "lock-free by design so _request can re-login mid-operation"
        )
        # Clear first, so a login that dies between the two calls leaves the
        # client in the "no session" state rather than holding a token whose
        # cookie no longer exists -- which is the state that used to persist
        # forever, because _ensure_session only ever logs in when this is None.
        self._xsrf = None
        await self._send(LOGIN_PATH, data={"username": self._username, "password": self._password})
        await self._send(ANTIFORGERY_PATH)
        cookies = self._session.cookie_jar.filter_cookies(API_BASE)
        token = cookies.get("xsrf-token")
        if token is None or not token.value:
            raise SRPApiError(
                "no xsrf-token cookie after login; the usage endpoints will refuse every request"
            )
        self._xsrf = URL(token.value).human_repr() if "%" in token.value else token.value

    async def _ensure_session(self) -> None:
        """Log in if we have never logged in, or if the last session was dropped.

        This is only half the story, and the weaker half. It cannot tell a live
        session from a dead one -- SRP does not say, and probing costs a request
        -- so it optimistically reuses whatever token it has. The other half is
        :meth:`_request`, which notices the 401 that proves the token is dead and
        clears it. Before that existed, a single evicted cookie was terminal:
        ``_xsrf`` stayed set, this method stayed satisfied, and every subsequent
        call 401'd forever.

        Assumes ``self._lock`` is held, like everything else below the public
        surface.
        """
        if self._xsrf is None:
            await self._login()

    async def validate(self) -> bool:
        """Round-trip the credentials. The public entry point for a login.

        Used by the config flow, for both first-time setup and the reauth
        prompt. Takes the lock, which is what :meth:`_login` requires and will
        not do for itself.
        """
        async with self._lock:
            await self._login()
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
                await _SLEEP(0.5)
        return out
