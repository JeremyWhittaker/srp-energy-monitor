"""Playwright client for the SRP My Account portal.

SRP has no public API for residential accounts, so everything here is browser
automation against https://myaccount.srpnet.com. That makes this module the
fragile part of the project by definition: SRP can change their markup at any
time. Two things follow from that, and both are deliberate design choices:

* Every selector is a *cascade* rather than a single string. The first entry is
  the one currently observed to work; the rest are fallbacks recovered from
  earlier versions of the portal. A markup tweak degrades rather than breaks.
* Nothing else in this package imports Playwright. Parsing, storage and the API
  all work on saved CSVs, so a scraper outage never takes the rest down with it.

Known limitation: the portal's date range is whatever SRP decides to show
(typically the last ~31 days). There is no date-range picker in the export
flow, so historical backfill beyond that window is not possible -- run the
fetch regularly and let the database accumulate history.
"""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import AsyncIterator, Sequence
from urllib.parse import urlparse

from .models import DemandObservation, Series

logger = logging.getLogger(__name__)

BASE_URL = "https://myaccount.srpnet.com/power"
USAGE_URL = f"{BASE_URL}/myaccount/usage"
# An older page that also surfaces demand; used only if the usage page yields
# nothing, since its markup has proven less stable.
DASHBOARD_URL = f"{BASE_URL}/MyAccount/Dashboard"

# The login page ships TWO copies of the form -- a lowercase-named pair that is
# visible on desktop widths, and a capitalised `UserName`/`Password` pair that
# is present but hidden. Both are in the DOM at once, so selectors are tried in
# visibility order and hidden matches are skipped (see `_fill_first`).
USERNAME_SELECTORS = (
    'input[name="username"]',
    "input#username_desktop",
    'input[name="UserName"]',
    "input#txtUserName",
    'input[type="email"]',
)
PASSWORD_SELECTORS = (
    'input[name="password"]',
    "input#password_desktop",
    'input[name="Password"]',
    "input#txtPassword",
    'input[type="password"]',
)
SUBMIT_SELECTORS = (
    'button[type="submit"]',
    "#loginSubmit",
    'button:has-text("Sign in")',
    'button:has-text("Log in")',
    'input[type="submit"]',
)

# Cloudflare fronts the SRP portal and blocks a default Playwright browser
# outright ("Access Denied" with a Ray ID) before any markup is served. A
# realistic fingerprint -- real user agent, desktop viewport, Arizona timezone,
# and no `navigator.webdriver` giveaway -- is required just to reach the login
# page. This is presentation, not evasion of an access control: the scraper
# still signs in with the account holder's own credentials.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)
_STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
window.chrome = window.chrome || { runtime: {} };
"""

EXPORT_SELECTORS = (
    'button:has-text("Export to Excel")',
    "button.btn.srp-btn.btn-lightblue:has-text('Export')",
    'button:has-text("Export")',
    'a:has-text("Export to Excel")',
    'a:has-text("Export")',
)

# How long a login is trusted before being re-established.
SESSION_TTL = timedelta(hours=2)

# Extracts the demand figure and its caption from the usage page. Runs in the
# browser because the value is rendered client-side.
_DEMAND_JS = """
() => {
    const clean = (text) => {
        if (!text) return null;
        const m = String(text).replace(/,/g, '').match(/(-?\\d+(?:\\.\\d+)?)\\s*kW/i)
              || String(text).replace(/,/g, '').match(/(-?\\d+(?:\\.\\d+)?)/);
        return m ? parseFloat(m[1]) : null;
    };

    const selectors = [
        '.srp-red-text strong',
        '.srp-red-text',
        '.srp-card-details',
        '.peak-demand-value',
        '.demand-value',
        '.current-peak strong',
        '[data-testid="peak-demand"]',
        '[class*="demand"]',
    ];

    // Walk up from the value to the card that captions it. The caption is
    // what tells us whether this is an average or a peak, which differs by
    // price plan, so it is data rather than decoration.
    const captionFor = (el) => {
        let node = el;
        for (let i = 0; i < 6 && node; i++) {
            node = node.parentElement;
            if (!node) break;
            const text = (node.textContent || '').replace(/\\s+/g, ' ').trim();
            const m = text.match(/(Current[^0-9]{0,60}?Demand)/i);
            if (m) return m[1].trim();
        }
        return null;
    };

    for (const selector of selectors) {
        const el = document.querySelector(selector);
        const value = el && clean(el.textContent);
        if (value !== null && !Number.isNaN(value)) {
            return { demand: value, source: selector, label: captionFor(el) };
        }
    }

    // Last resort: find a leaf element whose text is a kW reading.
    for (const el of document.querySelectorAll('*')) {
        if (el.children.length !== 0) continue;
        const text = el.textContent || '';
        if (!/kW/i.test(text) || /peak/i.test(text)) continue;
        const value = clean(text);
        if (value !== null && !Number.isNaN(value)) {
            return { demand: value, source: 'kW text scan', label: captionFor(el) };
        }
    }

    return { demand: null, source: null, label: null };
}
"""

# The usage page prints the billing cycle alongside the demand figure. It is
# the context that makes the number meaningful, so capture it when present.
_BILLING_CYCLE_JS = """
() => {
    const match = (document.body.innerText || '').match(/Billing cycle[^.\\n]{0,80}/i);
    return match ? match[0].trim() : null;
}
"""


class SRPError(RuntimeError):
    """Base class for scraper failures."""


class LoginError(SRPError):
    """Credentials were rejected, or the login form could not be found."""


class ScrapeError(SRPError):
    """The portal loaded but the expected data was not on the page."""


class SRPClient:
    """Logs in to SRP My Account and pulls energy data.

    Use it as an async context manager so the browser is always torn down::

        async with SRPClient(username, password) as srp:
            demand = await srp.fetch_current_demand()
            files = await srp.download_all(Path("./downloads"))
    """

    def __init__(
        self,
        username: str,
        password: str,
        *,
        headless: bool = True,
        timeout_ms: int = 120_000,
        download_timeout_ms: int = 60_000,
        slow_mo_ms: int = 0,
    ) -> None:
        if not username or not password:
            raise ValueError("SRP username and password are both required")
        self.username = username
        self.password = password
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.download_timeout_ms = download_timeout_ms
        self.slow_mo_ms = slow_mo_ms

        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._logged_in_at: datetime | None = None

    # ---------------------------------------------------------------- lifecycle

    async def __aenter__(self) -> "SRPClient":
        await self.start()
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    async def start(self) -> None:
        """Launch the browser. Imported lazily so Playwright stays optional."""
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise SRPError(
                "Playwright is not installed. Install it with:\n"
                "    pip install 'srp-energy-monitor[scrape]'\n"
                "    playwright install chromium"
            ) from exc

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            slow_mo=self.slow_mo_ms,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                # Without this Cloudflare rejects the session before SRP sees it.
                "--disable-blink-features=AutomationControlled",
            ],
        )
        self._context = await self._browser.new_context(
            accept_downloads=True,
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            timezone_id="America/Phoenix",
        )
        await self._context.add_init_script(_STEALTH_INIT_SCRIPT)
        self._page = await self._context.new_page()
        self._page.set_default_timeout(self.timeout_ms)

    async def close(self) -> None:
        """Tear down browser resources, tolerating partial startup."""
        for name in ("_context", "_browser", "_playwright"):
            resource = getattr(self, name, None)
            if resource is None:
                continue
            try:
                await (resource.stop() if name == "_playwright" else resource.close())
            except Exception as exc:  # pragma: no cover - best-effort cleanup
                logger.debug("error closing %s: %s", name, exc)
            setattr(self, name, None)
        self._page = None
        self._logged_in_at = None

    @property
    def page(self):
        if self._page is None:
            raise SRPError("client not started; use `async with SRPClient(...)` or call start()")
        return self._page

    # ------------------------------------------------------------------- login

    @property
    def session_is_fresh(self) -> bool:
        return (
            self._logged_in_at is not None
            and datetime.now() - self._logged_in_at < SESSION_TTL
        )

    async def _fill_first(self, selectors: Sequence[str], value: str, what: str) -> str:
        """Fill the first field that exists, returning the selector that matched.

        Callers need the matched selector, not just success: pressing Enter on
        a hard-coded selector would miss the field entirely whenever a fallback
        was the one that actually matched.
        """
        for selector in selectors:
            element = await self.page.query_selector(selector)
            # The page carries hidden duplicates of every login field. Filling
            # one of those succeeds silently and submits an empty form, so a
            # match only counts if the user can actually see it.
            if element is None or not await element.is_visible():
                continue
            await element.fill(value)
            logger.debug("filled %s using %s", what, selector)
            return selector
        raise LoginError(
            f"could not find a visible {what} field (tried: {', '.join(selectors)})"
        )

    async def login(self, *, force: bool = False) -> None:
        """Establish a session, reusing a recent one unless ``force``."""
        if self.session_is_fresh and not force:
            return

        logger.info("logging in to SRP as %s", self.username)
        await self.page.goto(BASE_URL, wait_until="domcontentloaded")
        await asyncio.sleep(2)

        if self._looks_authenticated(self.page.url):
            logger.info("already authenticated")
            self._logged_in_at = datetime.now()
            return

        await self._fill_first(USERNAME_SELECTORS, self.username, "username")
        password_selector = await self._fill_first(
            PASSWORD_SELECTORS, self.password, "password"
        )

        # Enter submits the real form; the button cascade below is a fallback
        # for layouts where the field isn't inside a submitting form.
        await self.page.press(password_selector, "Enter")
        await asyncio.sleep(5)

        if not self._looks_authenticated(self.page.url):
            for selector in SUBMIT_SELECTORS:
                button = await self.page.query_selector(selector)
                if button is not None and await button.is_visible():
                    await button.click()
                    await asyncio.sleep(5)
                    break

        if not self._looks_authenticated(self.page.url):
            raise LoginError(
                "SRP login failed. Check SRP_USERNAME/SRP_PASSWORD, and note that "
                "an account with MFA enabled cannot be automated -- this scraper "
                f"has no MFA support. Landed on: {self.page.url}"
            )

        self._logged_in_at = datetime.now()
        logger.info("SRP login successful")

    @staticmethod
    def _looks_authenticated(url: str) -> bool:
        """Decide from the URL whether we are past the login wall.

        Only the *path* is examined. The portal's hostname is literally
        ``myaccount.srpnet.com``, so matching against the whole URL made every
        page on the site look authenticated -- including the login page --
        and the scraper would skip login entirely and then fail to find data.
        """
        path = urlparse(url).path.lower()
        if "login" in path or "signin" in path:
            return False
        return any(token in path for token in ("dashboard", "myaccount", "usage"))

    async def _ensure_usage_page(self) -> None:
        await self.login()
        if "usage" not in self.page.url.lower():
            await self.page.goto(USAGE_URL, wait_until="networkidle")
            # networkidle alone has proven insufficient for this SPA.
            await asyncio.sleep(3)

    # -------------------------------------------------------------- scraping

    async def fetch_current_demand(self) -> DemandObservation:
        """Read the current bill cycle's headline demand figure, in kW.

        Whether that figure is an *average* or a *peak* depends on the account's
        price plan, so the on-page caption is captured with it rather than
        assumed. Falls back to the legacy dashboard page if the usage page
        yields nothing, since the two render the figure from different markup.
        """
        await self._ensure_usage_page()

        result = await self.page.evaluate(_DEMAND_JS)
        if result.get("demand") is None:
            logger.warning("demand figure not found on usage page, trying dashboard")
            await self.page.goto(DASHBOARD_URL, wait_until="networkidle")
            await asyncio.sleep(3)
            result = await self.page.evaluate(_DEMAND_JS)

        if result.get("demand") is None:
            raise ScrapeError(
                "could not find a demand value on the SRP usage or dashboard "
                "page. SRP most likely changed their markup; update the selector "
                "cascade in srp_energy/client.py"
            )

        observation = DemandObservation(
            observed_at=datetime.now(),
            demand_kw=float(result["demand"]),
            label=result.get("label"),
            source=result.get("source"),
        )
        logger.info(
            "%s: %.2f kW (via %s)",
            observation.label or "current demand",
            observation.demand_kw,
            result.get("source"),
        )
        if observation.label is None:
            # Without the caption we cannot tell an average from a peak, and
            # reporting the wrong one misstates the demand charge.
            logger.warning(
                "could not read the caption next to the demand figure; treat "
                "this value as unlabelled rather than assuming it is a peak"
            )
        return observation

    async def fetch_billing_cycle(self) -> str | None:
        """Return the billing-cycle description shown beside the demand figure."""
        await self._ensure_usage_page()
        try:
            return await self.page.evaluate(_BILLING_CYCLE_JS)
        except Exception as exc:  # pragma: no cover - cosmetic field
            logger.debug("billing cycle scrape failed: %s", exc)
            return None

    # -------------------------------------------------------------- downloads

    async def download_series(self, series: Series, dest_dir: str | Path) -> Path:
        """Export one series to CSV and save it under ``dest_dir``.

        Returns the path written, named ``srp_<series>_<YYYYmmdd_HHMMSS>.csv``
        so a directory of exports stays sortable and self-describing.
        """
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        await self._ensure_usage_page()

        await self._select_chart(series)

        export_button = None
        for selector in EXPORT_SELECTORS:
            export_button = await self.page.query_selector(selector)
            if export_button is not None:
                logger.debug("export button found via %s", selector)
                break
        if export_button is None:
            raise ScrapeError(
                f"no export button found for the {series.label} chart "
                f"(tried: {', '.join(EXPORT_SELECTORS)})"
            )

        async with self.page.expect_download(timeout=self.download_timeout_ms) as info:
            await export_button.click()
            download = await info.value

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = dest / f"srp_{series.value}_{timestamp}.csv"
        await download.save_as(path)
        logger.info("downloaded %s -> %s", series.label, path)
        return path

    async def _select_chart(self, series: Series) -> None:
        """Click the chart-type button so the export reflects the right series."""
        candidates = (
            f'button.chart-type-btn:has-text("{series.label}")',
            f'button.chart-type-btn.button-focus:has-text("{series.label}")',
            f'button:has-text("{series.label}")',
            f'a:has-text("{series.label}")',
            f'[data-chart-type="{series.value}"]',
            f'[title*="{series.label}"]',
        )
        for selector in candidates:
            button = await self.page.query_selector(selector)
            if button is not None:
                await button.click()
                await asyncio.sleep(3)  # let the chart re-render before exporting
                return

        # Exporting without switching yields whichever chart is selected, which
        # would silently write the wrong data under the right filename.
        raise ScrapeError(
            f"could not find the {series.label} chart button; refusing to export "
            "because the download would be mislabelled"
        )

    async def download_hourly(
        self,
        dest_dir: str | Path,
        start: date,
        end: date | None = None,
        series: Series = Series.USAGE,
    ) -> Path:
        """Export the Hourly/Interval view for one series over a date range.

        Unlike the daily exports, this view accepts an explicit range via the
        two MM/DD/YYYY pickers, so arbitrary history can be pulled a window at
        a time rather than only what the page happens to be showing.

        The chart selection is explicit because the export follows whichever
        chart is on screen: without selecting one, the file silently contains
        whatever SRP happened to show (net energy, by default) under a name
        implying something else.

        Returns the path, named ``srp_hourly_<series>_<start>_<end>.csv``.
        """
        end = end or start
        if end < start:
            raise ValueError(f"end date {end} is before start date {start}")

        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        await self._ensure_usage_page()

        # The tab is a MUI tab, and selecting it re-renders the panel, so every
        # interaction below uses auto-retrying locators rather than element
        # handles -- handles detach mid-render and raise.
        await self.page.get_by_role("tab", name="Hourly/Interval").click()
        await asyncio.sleep(4)

        await self._select_chart(series)

        pickers = self.page.locator('input[placeholder="MM/DD/YYYY"]:visible')
        if await pickers.count() < 2:
            raise ScrapeError(
                "the Hourly/Interval view did not show two date pickers; "
                "SRP may have changed the usage page controls"
            )

        for index, value in ((0, start), (1, end)):
            box = pickers.nth(index)
            await box.click()
            await box.press("Control+a")
            # Typed rather than filled: the MUI masked input ignores a
            # programmatic value set and keeps its previous date.
            await box.type(value.strftime("%m/%d/%Y"), delay=60)

        await self.page.get_by_role("button", name="Submit").filter(
            visible=True
        ).first.click()
        await asyncio.sleep(6)  # the chart must reload before the export matches

        async with self.page.expect_download(timeout=self.download_timeout_ms) as info:
            await self.page.get_by_role("button", name="Export to Excel").filter(
                visible=True
            ).first.click()
            download = await info.value

        path = dest / f"srp_hourly_{series.value}_{start:%Y%m%d}_{end:%Y%m%d}.csv"
        await download.save_as(path)
        logger.info("downloaded hourly %s %s..%s -> %s", series.label, start, end, path)
        return path

    async def download_all(
        self,
        dest_dir: str | Path,
        series: Sequence[Series] | None = None,
    ) -> dict[Series, Path]:
        """Export every series, keeping going if an individual one fails."""
        wanted = list(series) if series else list(Series)
        results: dict[Series, Path] = {}
        for item in wanted:
            try:
                results[item] = await self.download_series(item, dest_dir)
            except Exception as exc:
                logger.error("failed to download %s: %s", item.label, exc)
            await asyncio.sleep(2)  # be polite to SRP between exports
        if not results:
            raise ScrapeError("every series download failed; see the log for details")
        return results


@asynccontextmanager
async def open_client(username: str, password: str, **kwargs) -> AsyncIterator[SRPClient]:
    """Convenience wrapper for ``async with``."""
    client = SRPClient(username, password, **kwargs)
    await client.start()
    try:
        yield client
    finally:
        await client.close()
