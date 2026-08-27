"""Ties the scraper, parser and store together.

This is the only module that needs all three, which keeps the dependency
direction clean: the API and CLI call in here, and nothing here calls back out
to them.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Sequence

from .config import Settings
from .models import BillProjection, DemandObservation, Series
from .parser import ParseError, iter_downloads, parse_file, parse_hourly_file
from .storage import Store

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class FetchResult:
    """What a fetch actually accomplished, including partial failures.

    A fetch that gets three of four series is a useful outcome, not an error,
    so failures are reported alongside successes rather than raised.
    """

    readings_written: int = 0
    intervals_written: int = 0
    series_fetched: list[str] = field(default_factory=list)
    demand: DemandObservation | None = None
    bill_projection: BillProjection | None = None
    billing_cycle: str | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.series_fetched or self.demand or self.intervals_written)

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "readings_written": self.readings_written,
            "intervals_written": self.intervals_written,
            "series_fetched": self.series_fetched,
            "demand": self.demand.to_dict() if self.demand else None,
            "bill_projection": (
                self.bill_projection.to_dict() if self.bill_projection else None
            ),
            "billing_cycle": self.billing_cycle,
            "errors": self.errors,
        }


async def fetch_and_store(
    settings: Settings,
    store: Store,
    series: Sequence[Series] | None = None,
    *,
    include_demand: bool = True,
) -> FetchResult:
    """Log in to SRP, pull the requested series, and persist everything.

    Each stage is guarded independently so one failure does not discard the
    data already collected.
    """
    from .client import SRPClient  # imported here so Playwright stays optional

    username, password = settings.require_credentials()
    settings.ensure_dirs()
    result = FetchResult()

    client = SRPClient(
        username,
        password,
        headless=settings.headless,
        timeout_ms=settings.timeout_ms,
        download_timeout_ms=settings.download_timeout_ms,
    )

    try:
        try:
            await client.start()
            await client.login()
        except Exception as exc:
            # Login is the most common real-world failure (bad credentials, MFA,
            # or an SRP markup change). Report it like any other stage rather
            # than letting a traceback escape to the CLI.
            logger.error("could not establish an SRP session: %s", exc)
            result.errors.append(f"login: {exc}")
            return result

        if include_demand:
            try:
                observation = await client.fetch_current_demand()
                store.record_demand(observation)
                result.demand = observation
            except Exception as exc:
                logger.error("demand fetch failed: %s", exc)
                result.errors.append(f"demand: {exc}")

            try:
                projection = await client.fetch_bill_projection()
                store.record_bill_projection(projection)
                result.bill_projection = projection
            except Exception as exc:
                logger.error("bill projection fetch failed: %s", exc)
                result.errors.append(f"bill_projection: {exc}")

            try:
                result.billing_cycle = await client.fetch_billing_cycle()
            except Exception as exc:  # pragma: no cover - cosmetic
                logger.debug("billing cycle fetch failed: %s", exc)

        wanted = list(series) if series else list(Series)
        for item in wanted:
            try:
                path = await client.download_series(item, settings.downloads_dir)
            except Exception as exc:
                logger.error("download failed for %s: %s", item.value, exc)
                result.errors.append(f"{item.value}: {exc}")
                continue

            try:
                readings = parse_file(path, item)
            except ParseError as exc:
                logger.error("parse failed for %s: %s", path.name, exc)
                result.errors.append(f"{item.value}: {exc}")
                continue

            result.readings_written += store.upsert_readings(readings)
            result.series_fetched.append(item.value)

            if not settings.keep_downloads:
                path.unlink(missing_ok=True)

            await asyncio.sleep(1)  # be polite between exports
    finally:
        await client.close()

    logger.info(
        "fetch complete: %d readings across %s%s",
        result.readings_written,
        result.series_fetched or "no series",
        f", {len(result.errors)} error(s)" if result.errors else "",
    )
    return result


def import_directory(store: Store, directory: str | Path) -> tuple[int, list[str]]:
    """Backfill the database from a folder of previously downloaded CSVs.

    Returns ``(rows_written, errors)``. Existing rows are updated in place, so
    importing the same directory twice is a no-op rather than a duplication.
    """
    directory = Path(directory)
    if not directory.is_dir():
        # Path.glob on a missing directory yields nothing rather than raising,
        # so without this a typo'd path reports a successful no-op import.
        return 0, [f"{directory}: not a directory"]

    written = 0
    errors: list[str] = []
    for path, series in iter_downloads(directory):
        try:
            written += store.upsert_readings(parse_file(path, series))
        except (ParseError, OSError) as exc:
            errors.append(f"{path.name}: {exc}")
    return written, errors


async def fetch_hourly(
    settings: Settings,
    store: Store,
    start: "date",
    end: "date | None" = None,
    series: "Sequence[Series] | None" = None,
    *,
    max_days_per_request: int = 31,
) -> FetchResult:
    """Pull the Hourly/Interval export for a date range and store it.

    Long ranges are requested in chunks: SRP renders every interval in the
    window into one chart before it will export, and a multi-month request
    times out rather than failing cleanly.
    """
    from datetime import timedelta

    from .client import SRPClient

    username, password = settings.require_credentials()
    settings.ensure_dirs()
    end = end or start
    wanted = list(series) if series else [Series.USAGE]
    result = FetchResult()

    client = SRPClient(
        username,
        password,
        headless=settings.headless,
        timeout_ms=settings.timeout_ms,
        download_timeout_ms=settings.download_timeout_ms,
    )
    try:
        try:
            await client.start()
            await client.login()
        except Exception as exc:
            logger.error("could not establish an SRP session: %s", exc)
            result.errors.append(f"login: {exc}")
            return result

        window_start = start
        while window_start <= end:
            window_end = min(window_start + timedelta(days=max_days_per_request - 1), end)
            for item in wanted:
                try:
                    path = await client.download_hourly(
                        settings.downloads_dir, window_start, window_end, item
                    )
                    result.intervals_written += store.upsert_intervals(
                        parse_hourly_file(path, item)
                    )
                    if item.value not in result.series_fetched:
                        result.series_fetched.append(item.value)
                    if not settings.keep_downloads:
                        path.unlink(missing_ok=True)
                except Exception as exc:
                    logger.error(
                        "hourly %s %s..%s failed: %s",
                        item.value, window_start, window_end, exc,
                    )
                    result.errors.append(
                        f"hourly {item.value} {window_start}..{window_end}: {exc}"
                    )
                await asyncio.sleep(1)
            window_start = window_end + timedelta(days=1)
            await asyncio.sleep(1)
    finally:
        await client.close()

    logger.info("hourly fetch complete: %d intervals", result.intervals_written)
    return result
