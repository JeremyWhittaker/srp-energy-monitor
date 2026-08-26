"""API tests against a temporary database, with no browser or network involved."""

from __future__ import annotations

from datetime import date, datetime

import pytest

fastapi = pytest.importorskip("fastapi", reason="API extra not installed")
from fastapi.testclient import TestClient  # noqa: E402

from srp_energy.api import create_app  # noqa: E402
from srp_energy.config import Settings  # noqa: E402
from srp_energy.models import DailyReading, PeakDemandObservation, Series  # noqa: E402
from srp_energy.storage import Store  # noqa: E402


@pytest.fixture()
def client(tmp_path):
    store = Store(tmp_path / "api.db")
    store.upsert_readings(
        [
            DailyReading(
                series=Series.USAGE,
                usage_date=date(2025, 7, day),
                off_peak=80.0 + day,
                on_peak=20.0 + day,
                high_temp_f=110.0,
                low_temp_f=85.0,
            )
            for day in (1, 2, 3)
        ]
        + [
            DailyReading(series=Series.DEMAND, usage_date=date(2025, 7, 3), on_peak=2.4),
        ]
    )
    store.record_peak_demand(
        PeakDemandObservation(observed_at=datetime(2025, 7, 3, 6, 0), demand_kw=4.5, source=".srp-red-text strong")
    )
    settings = Settings(database_path=tmp_path / "api.db", data_dir=tmp_path)
    return TestClient(create_app(settings, store))


def test_health_reports_coverage(client):
    body = client.get("/health").json()

    assert body["status"] == "ok"
    assert body["series"]["usage"]["days"] == 3
    assert "srp_password" not in str(body).lower(), "credentials must never be exposed"


def test_peak_demand_endpoint(client):
    body = client.get("/api/v1/peak-demand").json()

    assert body["demand_kw"] == 4.5
    assert body["unit"] == "kW"


def test_readings_endpoint_filters(client):
    assert client.get("/api/v1/readings").json()["count"] == 4
    assert client.get("/api/v1/readings?series=usage").json()["count"] == 3
    assert client.get("/api/v1/readings?series=usage&start=2025-07-02").json()["count"] == 2


def test_readings_rejects_unknown_series(client):
    response = client.get("/api/v1/readings?series=bogus")

    assert response.status_code == 400
    assert "unknown series" in response.json()["detail"]


def test_series_endpoint_is_chart_shaped(client):
    body = client.get("/api/v1/series/usage?days=3650").json()

    assert body["unit"] == "kWh"
    assert body["labels"] == ["2025-07-01", "2025-07-02", "2025-07-03"]
    assert body["on_peak"] == [21.0, 22.0, 23.0]
    assert body["total"] == [102.0, 104.0, 106.0]


def test_metrics_exposes_prometheus_format(client):
    text = client.get("/metrics").text

    assert "# TYPE srp_peak_demand_kw gauge" in text
    assert "srp_peak_demand_kw 4.5" in text
    assert 'srp_daily_energy_kwh{series="usage",period="on_peak"' in text
    assert 'srp_readings_total{series="usage"} 3.0' in text


def test_fetch_without_credentials_is_refused(client):
    response = client.post("/api/v1/fetch")

    assert response.status_code == 503
    assert "credentials" in response.json()["detail"]


def test_dashboard_renders(client):
    response = client.get("/")

    assert response.status_code == 200
    assert "SRP Energy Monitor" in response.text


def test_peak_demand_404_on_empty_database(tmp_path):
    store = Store(tmp_path / "empty.db")
    empty = TestClient(create_app(Settings(database_path=tmp_path / "empty.db"), store))

    assert empty.get("/api/v1/peak-demand").status_code == 404
    assert empty.get("/metrics").text.startswith("# no SRP data")
