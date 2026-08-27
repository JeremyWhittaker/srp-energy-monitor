"""Command line interface.

    srp-energy-monitor fetch         # scrape SRP and store the results
    srp-energy-monitor serve         # run the REST API + dashboard
    srp-energy-monitor show          # print what's in the database
    srp-energy-monitor export        # dump readings as CSV or JSON
    srp-energy-monitor import        # backfill from previously downloaded CSVs
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
    show.add_argument("--days", type=int, default=30, help="How far back to show (default: 30).")

    export = sub.add_parser("export", help="Write stored readings to stdout or a file.")
    export.add_argument("--format", choices=["csv", "json"], default="csv")
    export.add_argument("--series", choices=[s.value for s in Series])
    export.add_argument("--days", type=int, help="Limit to the last N days.")
    export.add_argument("-o", "--output", help="Write here instead of stdout.")

    hourly = sub.add_parser("hourly", help="Pull hourly interval data for a date range.")
    hourly.add_argument(
        "--start", help="First date, YYYY-MM-DD. Default: --days back from --end."
    )
    hourly.add_argument("--end", help="Last date, YYYY-MM-DD. Default: yesterday.")
    hourly.add_argument(
        "--days", type=int, default=3,
        help="How many days back from --end when --start is omitted (default: 3).",
    )
    hourly.add_argument(
        "--series", action="append", choices=[s.value for s in Series],
        help="Which chart to export; repeatable. Default: usage.",
    )
    hourly.add_argument(
        "--show", action="store_true",
        help="Print the stored intervals afterwards instead of just a count.",
    )

    imp = sub.add_parser("import", help="Backfill from a folder of SRP CSV exports.")
    imp.add_argument("directory", help="Folder containing srp_<series>_<timestamp>.csv files.")

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
        fetch_and_store(
            settings, store, series, include_demand=not args.no_demand
        )
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
    print(f"Dashboard  http://{host}:{port}/")
    print(f"API docs   http://{host}:{port}/docs")
    print(f"Metrics    http://{host}:{port}/metrics")
    uvicorn.run(
        create_app(settings, store),
        host=host,
        port=port,
        reload=args.reload,
        log_level=settings.log_level.lower(),
    )
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
    rows = store.readings(
        series=Series.parse(args.series) if args.series else None, start=start
    )
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
    rows = store.readings(
        series=Series.parse(args.series) if args.series else None, start=start
    )
    if not rows:
        print("No readings to export.", file=sys.stderr)
        return 1

    handle = open(args.output, "w", newline="", encoding="utf-8") if args.output else sys.stdout
    try:
        if args.format == "json":
            json.dump([r.to_dict() for r in rows], handle, indent=2)
            handle.write("\n")
        else:
            fields = [
                "series", "usage_date", "meter_read_date", "off_peak", "on_peak",
                "total", "unit", "high_temp_f", "low_temp_f",
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
    end = date.fromisoformat(args.end) if args.end else date.today() - timedelta(days=1)
    start = (
        date.fromisoformat(args.start)
        if args.start
        else end - timedelta(days=max(0, args.days - 1))
    )
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
            print(
                f"{'hour':<17} {'series':<11} {'off-peak':>9} {'on-peak':>9} {'total':>8}"
            )
            for r in rows:
                print(
                    f"{r.interval_start.strftime('%Y-%m-%d %H:%M'):<17} "
                    f"{r.series.value:<11} "
                    f"{_fmt(r.off_peak):>9} {_fmt(r.on_peak):>9} {_fmt(r.total):>8}"
                )
    return 0 if result.ok else 1


_COMMANDS = {
    "fetch": _cmd_fetch,
    "hourly": _cmd_hourly,
    "serve": _cmd_serve,
    "show": _cmd_show,
    "export": _cmd_export,
    "import": _cmd_import,
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
