"""Command line interface.

srp-energy-monitor fetch         # scrape SRP and store the results
srp-energy-monitor serve         # run the REST API + dashboard
srp-energy-monitor show          # print what's in the database
srp-energy-monitor export        # dump readings as CSV or JSON
srp-energy-monitor import        # backfill from previously downloaded CSVs
srp-energy-monitor backfill      # pull history from the JSON API, no browser
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from .config import Settings
from .models import Series
from .storage import Store

# uvicorn resolves its log level through a plain dict lookup, so anything
# outside this set is a KeyError rather than a message.
_UVICORN_LOG_LEVELS = frozenset({"critical", "error", "warning", "info", "debug", "trace"})

# Roughly 270 years. Beyond this `date.today() - timedelta(days=...)` raises
# OverflowError, and no SRP account has data that far back regardless.
_MAX_DAYS = 100_000


def _iso_date(value: str) -> date:
    """argparse type: a YYYY-MM-DD date, reported as a usage error not a traceback."""
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a date in YYYY-MM-DD form") from None


def _positive_days(value: str) -> int:
    """argparse type: a day count that cannot overflow `date.today() - timedelta`."""
    try:
        days = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a whole number of days") from None
    if not 1 <= days <= _MAX_DAYS:
        raise argparse.ArgumentTypeError(f"days must be between 1 and {_MAX_DAYS}, got {days}")
    return days


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="srp-energy-monitor",
        description="Collect and serve Salt River Project energy data.",
    )
    parser.add_argument("--env-file", default=".env", help="Path to a .env file (default: .env)")
    parser.add_argument("--database", help="Override the SQLite database path.")
    parser.add_argument("--log-level", help="DEBUG, INFO, WARNING, ERROR.")
    sub = parser.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser("fetch", help="Log in to SRP and store the latest data.")
    fetch.add_argument(
        "--series",
        action="append",
        choices=[s.value for s in Series],
        help="Limit to one series; repeatable. Default: all four.",
    )
    fetch.add_argument(
        "--no-demand",
        action="store_true",
        help="Skip the live demand-figure scrape and only export CSVs.",
    )
    fetch.add_argument(
        "--show-browser",
        action="store_true",
        help="Run headed. Useful when SRP changes their markup and selectors break.",
    )

    serve = sub.add_parser("serve", help="Run the REST API and dashboard.")
    serve.add_argument("--host", help="Bind address (default: 127.0.0.1).")
    serve.add_argument("--port", type=int, help="Port (default: 8000).")
    serve.add_argument("--reload", action="store_true", help="Auto-reload on code changes.")

    show = sub.add_parser("show", help="Print stored data.")
    show.add_argument("--series", choices=[s.value for s in Series])
    show.add_argument(
        "--days", type=_positive_days, default=30, help="How far back to show (default: 30)."
    )

    export = sub.add_parser("export", help="Write stored readings to stdout or a file.")
    export.add_argument("--format", choices=["csv", "json"], default="csv")
    export.add_argument("--series", choices=[s.value for s in Series])
    export.add_argument("--days", type=_positive_days, help="Limit to the last N days.")
    export.add_argument("-o", "--output", help="Write here instead of stdout.")

    hourly = sub.add_parser("hourly", help="Pull hourly interval data for a date range.")
    hourly.add_argument(
        "--start", type=_iso_date, help="First date, YYYY-MM-DD. Default: --days back from --end."
    )
    hourly.add_argument("--end", type=_iso_date, help="Last date, YYYY-MM-DD. Default: yesterday.")
    hourly.add_argument(
        "--days",
        type=_positive_days,
        default=3,
        help="How many days back from --end when --start is omitted (default: 3).",
    )
    hourly.add_argument(
        "--series",
        action="append",
        choices=[s.value for s in Series],
        help="Which chart to export; repeatable. Default: usage.",
    )
    hourly.add_argument(
        "--show",
        action="store_true",
        help="Print the stored intervals afterwards instead of just a count.",
    )

    imp = sub.add_parser("import", help="Backfill from a folder of SRP CSV exports.")
    imp.add_argument("directory", help="Folder containing srp_<series>_<timestamp>.csv files.")

    backfill = sub.add_parser(
        "backfill",
        help="Pull history from SRP's JSON API with no browser.",
        description=(
            "Fetches daily history, billed cycle peaks and the bill projection, "
            "plus interval data across an arbitrary range. Needs SRP_ACCOUNT_ID. "
            "Interval requests are chunked automatically: SRP silently truncates "
            "anything wider than 31 days to its most recent month."
        ),
    )
    backfill.add_argument(
        "--start",
        type=_iso_date,
        help="First interval date, YYYY-MM-DD. Omit to skip interval data entirely.",
    )
    backfill.add_argument(
        "--end", type=_iso_date, help="Last interval date, YYYY-MM-DD. Default: yesterday."
    )
    backfill.add_argument(
        "--series",
        action="append",
        choices=[s.value for s in Series],
        help="Which series to store; repeatable. Default: all four.",
    )
    backfill.add_argument(
        "--no-daily",
        action="store_true",
        help="Skip the daily history and fetch only intervals.",
    )

    return parser


def _settings_from_args(args: argparse.Namespace) -> Settings:
    settings = Settings.from_env(args.env_file)
    if args.database:
        settings.database_path = Path(args.database).expanduser()
    if args.log_level:
        settings.log_level = args.log_level.upper()
    return settings


def _cmd_fetch(args: argparse.Namespace, settings: Settings, store: Store) -> int:
    from .service import fetch_and_store

    if args.show_browser:
        settings.headless = False
    series = [Series.parse(s) for s in args.series] if args.series else None

    result = asyncio.run(
        fetch_and_store(settings, store, series, include_demand=not args.no_demand)
    )

    if result.demand:
        d = result.demand
        print(f"{d.label or 'Current demand'}: {d.demand_kw:.2f} kW")
    if result.bill_projection:
        b = result.bill_projection
        print(f"Projected bill: ${b.amount_usd:,.2f}" + (f"  ({b.basis})" if b.basis else ""))
    if result.billing_cycle:
        print(result.billing_cycle)
    print(
        f"Stored {result.readings_written} readings "
        f"across {len(result.series_fetched)} series: {', '.join(result.series_fetched) or 'none'}"
    )
    for error in result.errors:
        print(f"  ! {error}", file=sys.stderr)
    return 0 if result.ok else 1


def _cmd_serve(args: argparse.Namespace, settings: Settings, store: Store) -> int:
    try:
        import uvicorn
    except ImportError:
        print(
            "uvicorn is not installed. Install the API extra from a checkout:\n"
            "    pip install -e '.[api]'",
            file=sys.stderr,
        )
        return 1

    from .api import create_app

    host = args.host or settings.host
    port = args.port or settings.port
    # uvicorn looks its level up in a bare dict, so an unrecognised one is a
    # KeyError traceback after we have already printed the URLs.
    log_level = settings.log_level.lower()
    if log_level not in _UVICORN_LOG_LEVELS:
        log_level = "info"

    print(f"Dashboard  http://{host}:{port}/")
    print(f"API docs   http://{host}:{port}/docs")
    print(f"Metrics    http://{host}:{port}/metrics")

    if args.reload:
        # uvicorn refuses to reload an app *object* -- it needs an import
        # string it can re-import in the child process. That child builds its
        # own Settings from the environment, so the CLI's --database and
        # --env-file do not reach it; say so rather than ignoring them quietly.
        if args.database or args.env_file != ".env":
            print(
                "note: --reload runs the app in a child process that reads its "
                "settings from the environment, so --database and --env-file "
                "are not passed through.",
                file=sys.stderr,
            )
        uvicorn.run(
            "srp_energy.api:create_app",
            factory=True,
            host=host,
            port=port,
            reload=True,
            log_level=log_level,
        )
    else:
        uvicorn.run(create_app(settings, store), host=host, port=port, log_level=log_level)
    return 0


def _cmd_show(args: argparse.Namespace, settings: Settings, store: Store) -> int:
    summary = store.summary()
    latest = summary["latest_demand"]
    if latest:
        caption = latest.get("label") or "Current demand (unlabelled)"
        print(f"{caption}: {latest['demand_kw']:.2f} kW  ({latest['observed_at']})")
    if not summary["series"]:
        print("No data stored yet. Run `srp-energy-monitor fetch` first.")
        return 1

    print()
    for name, info in summary["series"].items():  # type: ignore[union-attr]
        print(f"  {name:<11} {info['days']:>4} days  {info['first_date']} .. {info['last_date']}")

    projection = summary.get("latest_bill_projection")
    if projection:
        print(f"Projected bill: ${projection['amount_usd']:,.2f}")
        if projection.get("basis"):
            print(f"  {projection['basis']}")

    intervals = summary.get("intervals") or {}
    if intervals.get("count"):
        print(
            f"  {'hourly':<11} {intervals['count']:>4} intervals  "
            f"{intervals['first']} .. {intervals['last']}"
        )

    start = date.today() - timedelta(days=args.days)
    rows = store.readings(series=Series.parse(args.series) if args.series else None, start=start)
    if not rows:
        return 0

    print()
    print(f"{'date':<12} {'series':<11} {'off-peak':>9} {'on-peak':>9} {'high':>5} {'low':>5}")
    for row in rows:
        print(
            f"{row.usage_date.isoformat():<12} {row.series.value:<11} "
            f"{_fmt(row.off_peak):>9} {_fmt(row.on_peak):>9} "
            f"{_fmt(row.high_temp_f, 0):>5} {_fmt(row.low_temp_f, 0):>5}"
        )
    return 0


def _fmt(value: float | None, places: int = 2) -> str:
    return "-" if value is None else f"{value:.{places}f}"


def _cmd_export(args: argparse.Namespace, settings: Settings, store: Store) -> int:
    start = date.today() - timedelta(days=args.days) if args.days is not None else None
    rows = store.readings(series=Series.parse(args.series) if args.series else None, start=start)
    if not rows:
        print("No readings to export.", file=sys.stderr)
        return 1

    # Not a context manager: the handle is either a file we own and must close
    # or stdout, which we must not. The `finally` below closes only the former.
    handle = (
        open(args.output, "w", newline="", encoding="utf-8")  # noqa: SIM115
        if args.output
        else sys.stdout
    )
    try:
        if args.format == "json":
            json.dump([r.to_dict() for r in rows], handle, indent=2)
            handle.write("\n")
        else:
            fields = [
                "series",
                "usage_date",
                "meter_read_date",
                "off_peak",
                "on_peak",
                "total",
                "unit",
                "high_temp_f",
                "low_temp_f",
            ]
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                writer.writerow({k: row.to_dict()[k] for k in fields})
    finally:
        if args.output:
            handle.close()

    if args.output:
        print(f"Wrote {len(rows)} readings to {args.output}", file=sys.stderr)
    return 0


def _cmd_import(args: argparse.Namespace, settings: Settings, store: Store) -> int:
    from .service import import_directory

    written, errors = import_directory(store, args.directory)
    if errors:
        for error in errors:
            print(f"  ! {error}", file=sys.stderr)
    print(f"Imported {written} readings from {args.directory}")
    return 0 if not errors else 1


def _cmd_hourly(args: argparse.Namespace, settings: Settings, store: Store) -> int:
    from .service import fetch_hourly

    # SRP publishes a day once it is complete, so "yesterday" is the newest
    # date that reliably has all 24 intervals.
    end = args.end or date.today() - timedelta(days=1)
    start = args.start or end - timedelta(days=max(0, args.days - 1))
    if start > end:
        print(f"--start {start} is after --end {end}", file=sys.stderr)
        return 2

    wanted = [Series.parse(s) for s in args.series] if args.series else None
    result = asyncio.run(fetch_hourly(settings, store, start, end, wanted))
    print(f"Stored {result.intervals_written} hourly intervals for {start} .. {end}")
    for error in result.errors:
        print(f"  ! {error}", file=sys.stderr)

    if args.show:
        rows = store.intervals(start=start, end=end)
        if rows:
            print()
            print(f"{'hour':<17} {'series':<11} {'off-peak':>9} {'on-peak':>9} {'total':>8}")
            for r in rows:
                print(
                    f"{r.interval_start.strftime('%Y-%m-%d %H:%M'):<17} "
                    f"{r.series.value:<11} "
                    f"{_fmt(r.off_peak):>9} {_fmt(r.on_peak):>9} {_fmt(r.total):>8}"
                )
    return 0 if result.ok else 1


def _cmd_backfill(args: argparse.Namespace, settings: Settings, store: Store) -> int:
    """Pull history through the JSON backend instead of the browser.

    Kept separate from ``fetch`` rather than folded in behind a flag: the two
    backends have different failure modes and different credentials
    requirements, and a run that silently fell back from one to the other
    would make "why is this number different" much harder to answer.
    """
    from .jsonapi import (
        AccessBlocked,
        SRPApiError,
        SRPJsonClient,
        parse_bill_projection,
        parse_cycle_peaks,
        parse_daily_usage_detail,
        parse_demand_observation,
    )

    username, password = settings.require_credentials()
    if not settings.srp_account_id:
        print(
            "SRP_ACCOUNT_ID is not set. The JSON backend needs your 9-digit "
            "account number; the browser backend (`fetch`) does not.",
            file=sys.stderr,
        )
        return 2

    series = [Series.parse(s) for s in args.series] if args.series else None

    try:
        with SRPJsonClient(username, password, settings.srp_account_id) as srp:
            if not args.no_daily:
                daily_payload = srp.daily_usage_detail()
                try:
                    weather = srp.daily_weather()
                except SRPApiError:
                    # Temperature is decoration on every row; losing it is not
                    # a reason to lose the readings.
                    logging.getLogger(__name__).warning("weather unavailable; continuing")
                    weather = {}

                readings = parse_daily_usage_detail(daily_payload, weather, series)
                print(f"Stored {store.upsert_readings(readings)} daily readings")

                peaks = parse_cycle_peaks(daily_payload)
                print(f"Stored {store.upsert_cycle_peaks(peaks)} billed cycle peaks")
                if peaks:
                    latest = peaks[-1]
                    print(
                        f"  latest cycle {latest.cycle_start} .. {latest.cycle_end}: "
                        f"{latest.on_peak_high_demand_kw} kW at {latest.occurred_at}"
                    )

                observation = parse_demand_observation(daily_payload)
                if observation:
                    store.record_demand(observation)
                    print(f"{observation.label}: {observation.demand_kw:.2f} kW")

                projection = parse_bill_projection(srp.bill_projection())
                if projection:
                    store.record_bill_projection(projection)
                    print(f"Projected bill: ${projection.amount_usd:.2f}")

            if args.start:
                end = args.end or date.today() - timedelta(days=1)
                intervals = srp.hourly_detail(args.start, end, series)
                written = store.upsert_intervals(intervals)
                print(f"Stored {written} intervals for {args.start} .. {end}")
    except AccessBlocked as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    except SRPApiError as exc:
        print(f"SRP JSON API error: {exc}", file=sys.stderr)
        return 1

    return 0


_COMMANDS = {
    "fetch": _cmd_fetch,
    "hourly": _cmd_hourly,
    "serve": _cmd_serve,
    "show": _cmd_show,
    "export": _cmd_export,
    "import": _cmd_import,
    "backfill": _cmd_backfill,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = _settings_from_args(args)
    _configure_logging(settings.log_level)
    settings.ensure_dirs()
    store = Store(settings.database_path)
    try:
        return _COMMANDS[args.command](args, settings, store)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
