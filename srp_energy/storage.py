"""SQLite persistence for SRP readings.

Two tables, both designed so repeated fetches are idempotent:

``daily_reading``
    One row per (series, usage_date). SRP restates recent days as meter data
    settles, so imports upsert rather than insert -- re-running a fetch
    corrects history instead of duplicating it.

``peak_demand_observation``
    An append-only log of the live billing-cycle peak demand figure. This one
    *is* a time series: the value climbs through the month, and watching it
    climb is the point.

SQLite is sufficient here by a wide margin -- this is at most a few thousand
rows a year -- and it keeps the project a single-file deploy with no external
services.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from .models import DailyReading, PeakDemandObservation, Series

SCHEMA_VERSION = 1

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

CREATE TABLE IF NOT EXISTS peak_demand_observation (
    observed_at TEXT PRIMARY KEY,
    demand_kw   REAL NOT NULL,
    source      TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_daily_date ON daily_reading(usage_date);
CREATE INDEX IF NOT EXISTS idx_demand_time ON peak_demand_observation(observed_at);
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
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(SCHEMA_VERSION),),
            )

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
                ON CONFLICT(series, usage_date) DO UPDATE SET
                    meter_read_date = excluded.meter_read_date,
                    off_peak        = excluded.off_peak,
                    on_peak         = excluded.on_peak,
                    high_temp_f     = excluded.high_temp_f,
                    low_temp_f      = excluded.low_temp_f,
                    updated_at      = excluded.updated_at
                """,
                rows,
            )
        return len(rows)

    def record_peak_demand(self, observation: PeakDemandObservation) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO peak_demand_observation"
                " (observed_at, demand_kw, source) VALUES (?, ?, ?)",
                (
                    observation.observed_at.isoformat(timespec="seconds"),
                    observation.demand_kw,
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

    def latest_peak_demand(self) -> PeakDemandObservation | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM peak_demand_observation ORDER BY observed_at DESC LIMIT 1"
            ).fetchone()
        return _row_to_observation(row) if row else None

    def peak_demand_history(
        self,
        start: datetime | None = None,
        limit: int = 500,
    ) -> list[PeakDemandObservation]:
        sql = "SELECT * FROM peak_demand_observation"
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
            demand_count = conn.execute(
                "SELECT COUNT(*) FROM peak_demand_observation"
            ).fetchone()[0]

        latest = self.latest_peak_demand()
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
            "peak_demand_observations": demand_count,
            "latest_peak_demand": latest.to_dict() if latest else None,
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


def _row_to_observation(row: sqlite3.Row) -> PeakDemandObservation:
    return PeakDemandObservation(
        observed_at=datetime.fromisoformat(row["observed_at"]),
        demand_kw=row["demand_kw"],
        source=row["source"],
    )
