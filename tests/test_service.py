"""Service-layer tests.

The property under test is graceful degradation: a fetch that cannot log in,
or an import pointed at a bad path, must report the failure rather than crash
or silently claim success.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from srp_energy.config import Settings
from srp_energy.service import fetch_and_store, import_directory
from srp_energy.storage import Store

FIXTURES = Path(__file__).parent / "fixtures"


def test_import_directory_loads_every_series(tmp_path):
    store = Store(tmp_path / "s.db")

    written, errors = import_directory(store, FIXTURES)

    assert errors == []
    assert written == 14
    assert {r.series.value for r in store.readings()} == {"usage", "generation", "net", "demand"}


def test_import_directory_is_idempotent(tmp_path):
    store = Store(tmp_path / "s.db")
    import_directory(store, FIXTURES)
    import_directory(store, FIXTURES)

    assert len(store.readings()) == 14


def test_import_directory_reports_a_missing_path(tmp_path):
    """A typo'd path used to report a successful no-op import."""
    store = Store(tmp_path / "s.db")

    written, errors = import_directory(store, tmp_path / "nope")

    assert written == 0
    assert errors and "not a directory" in errors[0]


def test_import_directory_ignores_unrelated_files(tmp_path):
    store = Store(tmp_path / "s.db")
    (tmp_path / "notes.txt").write_text("hello")
    (tmp_path / "other.csv").write_text("a,b\n1,2\n")

    written, errors = import_directory(store, tmp_path)

    assert (written, errors) == (0, [])


def test_login_failure_is_reported_not_raised(tmp_path, monkeypatch):
    """A bad login is the most common real failure; it must not traceback."""
    store = Store(tmp_path / "s.db")
    settings = Settings(
        srp_username="alice",
        srp_password="secret",
        data_dir=tmp_path,
        downloads_dir=tmp_path / "dl",
        database_path=tmp_path / "s.db",
    )

    class BoomClient:
        def __init__(self, *args, **kwargs):
            pass

        async def start(self):
            raise RuntimeError("browser exploded")

        async def close(self):
            pass

    monkeypatch.setattr("srp_energy.client.SRPClient", BoomClient)

    result = asyncio.run(fetch_and_store(settings, store))

    assert result.ok is False
    assert result.errors and "browser exploded" in result.errors[0]
    assert result.readings_written == 0


def test_fetch_without_credentials_exits_cleanly(tmp_path):
    import pytest

    store = Store(tmp_path / "s.db")
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / "s.db")

    with pytest.raises(SystemExit):
        asyncio.run(fetch_and_store(settings, store))
