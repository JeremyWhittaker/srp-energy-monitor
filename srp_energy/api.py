"""REST API over the stored SRP data.

The point of this project is that other programs can consume your SRP numbers,
so the API is the primary interface and the dashboard is a client of it. Every
endpoint returns JSON, is read-only apart from ``POST /api/v1/fetch``, and is
documented automatically at ``/docs`` (OpenAPI).

A Prometheus exposition endpoint lives at ``/metrics`` so the same data can be
scraped into an existing monitoring stack without writing any glue.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import HTMLResponse, PlainTextResponse

from .config import Settings
from .models import Series
from .storage import Store

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1"


def _parse_series(value: str | None) -> Series | None:
    if value is None:
        return None
    try:
        return Series.parse(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def create_app(settings: Settings | None = None, store: Store | None = None) -> FastAPI:
    """Build the FastAPI app.

    Accepts injected settings/store so tests can point at a temporary database.
    """
    settings = settings or Settings.from_env()
    store = store or Store(settings.database_path)

    app = FastAPI(
        title="SRP Energy Monitor",
        version="1.0.0",
        summary="Read your Salt River Project peak demand and energy data over HTTP.",
        description=(
            "Salt River Project publishes residential usage only through their web "
            "portal. This service signs in on your behalf, collects peak demand, "
            "usage, generation and net energy, stores them, and serves them as JSON "
            "so other tools can use them.\n\n"
            "Not affiliated with or endorsed by Salt River Project."
        ),
        docs_url="/docs",
        redoc_url="/redoc",
    )
    app.state.settings = settings
    app.state.store = store
    # Serialises manual fetches; two concurrent scrapes would fight over the
    # same SRP session and waste a login.
    app.state.fetch_lock = asyncio.Lock()

    # ------------------------------------------------------------- dashboard

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def dashboard() -> HTMLResponse:
        html = Path(__file__).parent / "web" / "dashboard.html"
        if not html.is_file():  # pragma: no cover - packaging guard
            return HTMLResponse("<h1>SRP Energy Monitor</h1><p>See <a href='/docs'>/docs</a>.</p>")
        return HTMLResponse(html.read_text(encoding="utf-8"))

    # ---------------------------------------------------------------- health

    @app.get("/health", tags=["meta"], summary="Liveness and data coverage")
    async def health() -> dict[str, Any]:
        summary = store.summary()
        return {
            "status": "ok",
            "credentials_configured": bool(settings.srp_username and settings.srp_password),
            **summary,
        }

    @app.get(f"{API_PREFIX}/summary", tags=["meta"], summary="Row counts and date coverage")
    async def summary() -> dict[str, Any]:
        return store.summary()

    # ----------------------------------------------------------------- data

    @app.get(
        f"{API_PREFIX}/peak-demand",
        tags=["peak demand"],
        summary="Latest billing-cycle peak demand",
    )
    async def peak_demand() -> dict[str, Any]:
        latest = store.latest_peak_demand()
        if latest is None:
            raise HTTPException(
                status_code=404,
                detail="no peak demand recorded yet; run a fetch first",
            )
        return latest.to_dict()

    @app.get(
        f"{API_PREFIX}/peak-demand/history",
        tags=["peak demand"],
        summary="Peak demand observations over time",
    )
    async def peak_demand_history(
        days: int = Query(30, ge=1, le=3650, description="How far back to look."),
        limit: int = Query(500, ge=1, le=10_000),
    ) -> dict[str, Any]:
        since = datetime.now() - timedelta(days=days)
        history = store.peak_demand_history(start=since, limit=limit)
        return {
            "count": len(history),
            "unit": "kW",
            "observations": [o.to_dict() for o in history],
        }

    @app.get(
        f"{API_PREFIX}/readings",
        tags=["readings"],
        summary="Daily readings for one or all series",
    )
    async def readings(
        series: str | None = Query(
            None, description="usage, generation, net or demand. Omit for all."
        ),
        start: date | None = Query(None, description="Earliest usage date (inclusive)."),
        end: date | None = Query(None, description="Latest usage date (inclusive)."),
        limit: int | None = Query(None, ge=1, le=10_000),
    ) -> dict[str, Any]:
        rows = store.readings(
            series=_parse_series(series), start=start, end=end, limit=limit
        )
        return {"count": len(rows), "readings": [r.to_dict() for r in rows]}

    @app.get(
        f"{API_PREFIX}/series/{{series}}",
        tags=["readings"],
        summary="One series, shaped for charting",
    )
    async def series_for_chart(
        series: str,
        days: int = Query(30, ge=1, le=3650),
    ) -> dict[str, Any]:
        parsed = _parse_series(series)
        assert parsed is not None  # _parse_series only returns None for None input
        start = date.today() - timedelta(days=days)
        rows = store.readings(series=parsed, start=start)
        return {
            "series": parsed.value,
            "label": parsed.label,
            "unit": parsed.unit,
            "count": len(rows),
            "labels": [r.usage_date.isoformat() for r in rows],
            "off_peak": [r.off_peak for r in rows],
            "on_peak": [r.on_peak for r in rows],
            "total": [r.total for r in rows],
            "high_temp_f": [r.high_temp_f for r in rows],
            "low_temp_f": [r.low_temp_f for r in rows],
        }

    # ---------------------------------------------------------------- action

    @app.post(
        f"{API_PREFIX}/fetch",
        tags=["actions"],
        summary="Scrape SRP now and store the result",
    )
    async def fetch(
        series: str | None = Query(None, description="Limit to one series."),
    ) -> dict[str, Any]:
        from .service import fetch_and_store

        if not (settings.srp_username and settings.srp_password):
            raise HTTPException(
                status_code=503,
                detail="SRP credentials are not configured; set SRP_USERNAME and SRP_PASSWORD",
            )
        if app.state.fetch_lock.locked():
            raise HTTPException(status_code=409, detail="a fetch is already running")

        parsed = _parse_series(series)
        async with app.state.fetch_lock:
            try:
                result = await fetch_and_store(
                    settings, store, [parsed] if parsed else None
                )
            except Exception as exc:
                logger.exception("fetch failed")
                raise HTTPException(status_code=502, detail=f"fetch failed: {exc}") from exc
        return result.to_dict()

    # --------------------------------------------------------------- metrics

    @app.get(
        "/metrics",
        response_class=PlainTextResponse,
        tags=["meta"],
        summary="Prometheus exposition of the latest values",
    )
    async def metrics() -> Response:
        return PlainTextResponse(_render_metrics(store), media_type="text/plain; version=0.0.4")

    return app


def _render_metrics(store: Store) -> str:
    """Render the current data in Prometheus text exposition format.

    Written by hand rather than pulling in prometheus_client: the output is a
    dozen lines and this keeps the dependency list short.
    """
    lines: list[str] = []

    def add(name: str, help_text: str, kind: str, samples: list[tuple[str, float]]) -> None:
        if not samples:
            return
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")
        lines.extend(f"{name}{labels} {value}" for labels, value in samples)

    latest = store.latest_peak_demand()
    if latest is not None:
        add(
            "srp_peak_demand_kw",
            "Peak on-peak demand recorded in the current SRP billing cycle.",
            "gauge",
            [("", latest.demand_kw)],
        )
        add(
            "srp_peak_demand_observed_timestamp_seconds",
            "Unix timestamp of the most recent peak demand observation.",
            "gauge",
            [("", latest.observed_at.timestamp())],
        )

    # Most recent complete day per series, which is what alerting cares about.
    energy_samples: list[tuple[str, float]] = []
    for series in Series:
        rows = store.readings(series=series)
        if not rows:
            continue
        newest = rows[-1]
        if series is Series.DEMAND:
            if newest.on_peak is not None:
                add(
                    "srp_daily_peak_demand_kw",
                    "Peak demand for the most recent day SRP has reported.",
                    "gauge",
                    [(f'{{date="{newest.usage_date.isoformat()}"}}', newest.on_peak)],
                )
            continue
        for period, value in (("off_peak", newest.off_peak), ("on_peak", newest.on_peak)):
            if value is not None:
                energy_samples.append(
                    (
                        f'{{series="{series.value}",period="{period}",'
                        f'date="{newest.usage_date.isoformat()}"}}',
                        value,
                    )
                )

    add(
        "srp_daily_energy_kwh",
        "Most recent daily energy total reported by SRP, by series and period.",
        "gauge",
        energy_samples,
    )

    summary = store.summary()
    add(
        "srp_readings_total",
        "Number of stored daily readings, by series.",
        "gauge",
        [
            (f'{{series="{name}"}}', float(info["days"]))
            for name, info in summary["series"].items()  # type: ignore[union-attr]
        ],
    )

    return "\n".join(lines) + "\n" if lines else "# no SRP data collected yet\n"


# Deliberately no module-level `app`: building one at import time would create
# the database as a side effect of importing this module, which breaks tests
# and tooling. Run it with uvicorn's factory mode instead:
#
#     uvicorn "srp_energy.api:create_app" --factory
#
# or just use `srp-energy-monitor serve`, which does the same thing.
