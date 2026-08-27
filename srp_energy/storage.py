"""SQLite persistence for SRP readings.

Two tables, both designed so repeated fetches are idempotent:

``daily_reading``
    One row per (series, usage_date). SRP restates recent days as meter data
    settles, so imports upsert rather than insert -- re-running a fetch
    corrects history instead of duplicating it.

``demand_observation``
    An append-only log of the live bill-cycle demand figure, with the caption
    SRP printed beside it. The caption matters: on an average-demand plan the
    number is a cycle mean, on a peak-demand plan it is a maximum, and storing
    it without that context makes the two indistinguishable after the fact.

SQLite is sufficient here by a wide margin -- this is at most a few thousand
rows a year -- and it keeps the project a single-file deploy with no external
services.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import date, datetime, time
from pathlib import Path

from .models import BillProjection, DailyReading, DemandObservation, IntervalReading, Series

SCHEMA_VERSION = 4

_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_reading (
    series          TEXT NOT NULL,
    usage_date      TEXT NOT NULL,
    meter_read_date TEXT,
    off_peak        REAL,
    on_peak         REAL,
    high_temp_f     REAL,
    low_temp_f      REAL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (series, usage_date)
);

CREATE TABLE IF NOT EXISTS demand_observation (
    observed_at TEXT PRIMARY KEY,
    demand_kw   REAL NOT NULL,
    label       TEXT,
    source      TEXT
);

CREATE TABLE IF NOT EXISTS interval_reading (
    series         TEXT NOT NULL,
    interval_start TEXT NOT NULL,
    off_peak       REAL,
    on_peak        REAL,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (series, interval_start)
);

CREATE TABLE IF NOT EXISTS bill_projection (
    observed_at TEXT PRIMARY KEY,
    amount_usd  REAL NOT NULL,
    basis       TEXT,
    source      TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_daily_date ON daily_reading(usage_date);
CREATE INDEX IF NOT EXISTS idx_demand_time ON demand_observation(observed_at);
CREATE INDEX IF NOT EXISTS idx_interval_start ON interval_reading(interval_start);
"""


class Store:
    """Thin, dependency-free wrapper over the SQLite database."""

    def __init__(self, path: str | Path = "./data/srp.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        # WAL keeps the API readable while a fetch is writing.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(_SCHEMA)
            self._migrate(conn)
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(SCHEMA_VERSION),),
            )

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Carry a v1 database forward.

        v1 stored the live figure in ``peak_demand_observation`` and assumed it
        was a peak. It may well have been a cycle *average* -- that depends on
        the price plan -- so rows are copied across with a NULL label rather
        than being relabelled with a guess.
        """
        existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "peak_demand_observation" in existing:
            conn.execute(
                "INSERT OR IGNORE INTO demand_observation (observed_at, demand_kw, label, source)"
                " SELECT observed_at, demand_kw, NULL, source FROM peak_demand_observation"
            )
            conn.execute("DROP TABLE peak_demand_observation")

    # -------------------------------------------------------------- writing

    def upsert_readings(self, readings: Iterable[DailyReading]) -> int:
        """Insert or update daily readings. Returns the number written."""
        rows = [
            (
                r.series.value,
                r.usage_date.isoformat(),
                r.meter_read_date.isoformat() if r.meter_read_date else None,
                r.off_peak,
                r.on_peak,
                r.high_temp_f,
                r.low_temp_f,
                datetime.now().isoformat(timespec="seconds"),
            )
            for r in readings
        ]
        if not rows:
            return 0
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO daily_reading
                    (series, usage_date, meter_read_date, off_peak, on_peak,
                     high_temp_f, low_temp_f, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                -- COALESCE, not a plain assignment: SRP restates recent days
                -- as meter data settles, so a new value must win -- but a NULL
                -- must not. A parse that yields blanks (SRP renaming a column
                -- is the realistic cause) would otherwise erase a month of
                -- good history on the next scheduled fetch.
                ON CONFLICT(series, usage_date) DO UPDATE SET
                    meter_read_date = COALESCE(excluded.meter_read_date, meter_read_date),
                    off_peak        = COALESCE(excluded.off_peak, off_peak),
                    on_peak         = COALESCE(excluded.on_peak, on_peak),
                    high_temp_f     = COALESCE(excluded.high_temp_f, high_temp_f),
                    low_temp_f      = COALESCE(excluded.low_temp_f, low_temp_f),
                    updated_at      = excluded.updated_at
                """,
                rows,
            )
        return len(rows)

    def upsert_intervals(self, readings: Iterable[IntervalReading]) -> int:
        """Insert or update hourly intervals, keyed on the interval start."""
        rows = [
            (
                r.series.value,
                r.interval_start.isoformat(timespec="minutes"),
                r.off_peak,
                r.on_peak,
                datetime.now().isoformat(timespec="seconds"),
            )
            for r in readings
        ]
        if not rows:
            return 0
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO interval_reading
                    (series, interval_start, off_peak, on_peak, updated_at)
                VALUES (?, ?, ?, ?, ?)
                -- Same reasoning as upsert_readings: a restated value wins, a
                -- NULL never overwrites a stored one.
                ON CONFLICT(series, interval_start) DO UPDATE SET
                    off_peak   = COALESCE(excluded.off_peak, off_peak),
                    on_peak    = COALESCE(excluded.on_peak, on_peak),
                    updated_at = excluded.updated_at
                """,
                rows,
            )
        return len(rows)

    def record_bill_projection(self, projection: BillProjection) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO bill_projection"
                " (observed_at, amount_usd, basis, source) VALUES (?, ?, ?, ?)",
                (
                    projection.observed_at.isoformat(timespec="seconds"),
                    projection.amount_usd,
                    projection.basis,
                    projection.source,
                ),
            )

    def latest_bill_projection(self) -> BillProjection | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM bill_projection ORDER BY observed_at DESC LIMIT 1"
            ).fetchone()
        return _row_to_projection(row) if row else None

    def bill_projection_history(
        self, start: datetime | None = None, limit: int = 500
    ) -> list[BillProjection]:
        sql = "SELECT * FROM bill_projection"
        params: list[object] = []
        if start is not None:
            sql += " WHERE observed_at >= ?"
            params.append(start.isoformat(timespec="seconds"))
        sql += " ORDER BY observed_at DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_projection(r) for r in rows]

    def record_demand(self, observation: DemandObservation) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO demand_observation"
                " (observed_at, demand_kw, label, source) VALUES (?, ?, ?, ?)",
                (
                    observation.observed_at.isoformat(timespec="seconds"),
                    observation.demand_kw,
                    observation.label,
                    observation.source,
                ),
            )

    # -------------------------------------------------------------- reading

    def readings(
        self,
        series: Series | Sequence[Series] | None = None,
        start: date | None = None,
        end: date | None = None,
        limit: int | None = None,
    ) -> list[DailyReading]:
        """Query daily readings, oldest first.

        ``limit`` keeps the *most recent* N readings, then returns them in
        ascending order. Anything else would be a trap: asking for 30 days of
        data and silently getting the oldest 30 rows in the database is never
        what a caller means.

        Note it counts **rows, not days**. All four series share this table, so
        an unfiltered ``limit=30`` spans about eight days and the oldest day
        comes back partial. Pass ``series``, or filter with ``start``/``end``,
        when you want whole days.
        """
        clauses: list[str] = []
        params: list[object] = []

        if series is not None:
            wanted = [series] if isinstance(series, Series) else list(series)
            clauses.append(f"series IN ({','.join('?' * len(wanted))})")
            params.extend(s.value for s in wanted)
        if start is not None:
            clauses.append("usage_date >= ?")
            params.append(start.isoformat())
        if end is not None:
            clauses.append("usage_date <= ?")
            params.append(end.isoformat())

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

        if limit is None:
            sql = f"SELECT * FROM daily_reading{where} ORDER BY usage_date ASC, series ASC"
        else:
            # Take the newest N in the inner query, then flip back to ascending
            # so callers always get chronological order regardless of limit.
            sql = (
                f"SELECT * FROM (SELECT * FROM daily_reading{where}"
                " ORDER BY usage_date DESC, series DESC LIMIT ?)"
                " ORDER BY usage_date ASC, series ASC"
            )
            params.append(limit)

        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_reading(row) for row in rows]

    def intervals(
        self,
        series: Series | None = None,
        start: date | None = None,
        end: date | None = None,
        limit: int | None = None,
    ) -> list[IntervalReading]:
        """Query hourly intervals, oldest first.

        ``start``/``end`` are inclusive dates; ``end`` covers the whole day, so
        asking for a single date returns its 24 intervals (48 for demand, which
        SRP reports half-hourly).

        As with :meth:`readings`, ``limit`` counts rows rather than days or
        series; pass ``series`` alongside it if you want a whole series back.
        """
        clauses: list[str] = []
        params: list[object] = []
        if series is not None:
            clauses.append("series = ?")
            params.append(series.value)
        if start is not None:
            clauses.append("interval_start >= ?")
            params.append(datetime.combine(start, time.min).isoformat(timespec="minutes"))
        if end is not None:
            clauses.append("interval_start <= ?")
            params.append(datetime.combine(end, time.max).isoformat(timespec="minutes"))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

        if limit is None:
            sql = f"SELECT * FROM interval_reading{where} ORDER BY interval_start ASC, series ASC"
        else:
            # `series DESC` is a tiebreak, not decoration: several series can
            # share an interval_start, and without it which rows survive the
            # LIMIT is left to SQLite.
            sql = (
                f"SELECT * FROM (SELECT * FROM interval_reading{where}"
                " ORDER BY interval_start DESC, series DESC LIMIT ?)"
                " ORDER BY interval_start ASC, series ASC"
            )
            params.append(limit)

        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            IntervalReading(
                series=Series(r["series"]),
                interval_start=datetime.fromisoformat(r["interval_start"]),
                off_peak=r["off_peak"],
                on_peak=r["on_peak"],
            )
            for r in rows
        ]

    def latest_demand(self) -> DemandObservation | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM demand_observation ORDER BY observed_at DESC LIMIT 1"
            ).fetchone()
        return _row_to_observation(row) if row else None

    def demand_history(
        self,
        start: datetime | None = None,
        limit: int = 500,
    ) -> list[DemandObservation]:
        sql = "SELECT * FROM demand_observation"
        params: list[object] = []
        if start is not None:
            sql += " WHERE observed_at >= ?"
            params.append(start.isoformat(timespec="seconds"))
        sql += " ORDER BY observed_at DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_observation(row) for row in rows]

    def summary(self) -> dict[str, object]:
        """Row counts and date coverage per series, for /health and the CLI."""
        with self.connect() as conn:
            per_series = conn.execute(
                """
                SELECT series,
                       COUNT(*)            AS days,
                       MIN(usage_date)     AS first_date,
                       MAX(usage_date)     AS last_date,
                       MAX(updated_at)     AS updated_at
                FROM daily_reading
                GROUP BY series
                ORDER BY series
                """
            ).fetchall()
            demand_count = conn.execute("SELECT COUNT(*) FROM demand_observation").fetchone()[0]
            interval_row = conn.execute(
                "SELECT COUNT(*) AS n, MIN(interval_start) AS first, MAX(interval_start) AS last"
                " FROM interval_reading"
            ).fetchone()

        latest = self.latest_demand()
        projection = self.latest_bill_projection()
        return {
            "database": str(self.path),
            "schema_version": SCHEMA_VERSION,
            "series": {
                row["series"]: {
                    "days": row["days"],
                    "first_date": row["first_date"],
                    "last_date": row["last_date"],
                    "updated_at": row["updated_at"],
                }
                for row in per_series
            },
            "intervals": {
                "count": interval_row["n"],
                "first": interval_row["first"],
                "last": interval_row["last"],
            },
            "demand_observations": demand_count,
            "latest_demand": latest.to_dict() if latest else None,
            "latest_bill_projection": projection.to_dict() if projection else None,
        }


def _row_to_reading(row: sqlite3.Row) -> DailyReading:
    return DailyReading(
        series=Series(row["series"]),
        usage_date=date.fromisoformat(row["usage_date"]),
        meter_read_date=(
            date.fromisoformat(row["meter_read_date"]) if row["meter_read_date"] else None
        ),
        off_peak=row["off_peak"],
        on_peak=row["on_peak"],
        high_temp_f=row["high_temp_f"],
        low_temp_f=row["low_temp_f"],
    )


def _row_to_projection(row: sqlite3.Row) -> BillProjection:
    return BillProjection(
        observed_at=datetime.fromisoformat(row["observed_at"]),
        amount_usd=row["amount_usd"],
        basis=row["basis"],
        source=row["source"],
    )


def _row_to_observation(row: sqlite3.Row) -> DemandObservation:
    return DemandObservation(
        observed_at=datetime.fromisoformat(row["observed_at"]),
        demand_kw=row["demand_kw"],
        label=row["label"],
        source=row["source"],
    )
