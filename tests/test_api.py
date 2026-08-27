"""API tests against a temporary database, with no browser or network involved."""

from __future__ import annotations

from datetime import date, datetime

import pytest

fastapi = pytest.importorskip("fastapi", reason="API extra not installed")
from fastapi.testclient import TestClient  # noqa: E402

from srp_energy.api import create_app  # noqa: E402
from srp_energy.config import Settings  # noqa: E402
from srp_energy.models import DailyReading, DemandObservation, Series  # noqa: E402
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
    store.record_demand(
        DemandObservation(observed_at=datetime(2025, 7, 3, 6, 0), demand_kw=4.5, source=".srp-red-text strong")
    )
    settings = Settings(database_path=tmp_path / "api.db", data_dir=tmp_path)
    return TestClient(create_app(settings, store))


def test_health_reports_coverage(client):
    body = client.get("/health").json()

    assert body["status"] == "ok"
    assert body["series"]["usage"]["days"] == 3
    assert "srp_password" not in str(body).lower(), "credentials must never be exposed"


def test_peak_demand_endpoint(client):
    body = client.get("/api/v1/demand").json()

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

    assert "# TYPE srp_current_demand_kw gauge" in text
    assert "srp_current_demand_kw{" in text
    assert 'srp_daily_energy_kwh{series="usage",period="on_peak"' in text
    assert 'srp_readings_total{series="usage"} 3.0' in text


def test_metrics_carry_the_srp_caption_so_average_is_not_read_as_peak(tmp_path):
    """The same metric is a cycle mean on one plan and a maximum on another."""
    store = Store(tmp_path / "labelled.db")
    store.record_demand(
        DemandObservation(
            observed_at=datetime(2026, 8, 26, 16, 0),
            demand_kw=5.4,
            label="Current Average On-Peak Demand",
        )
    )
    c = TestClient(create_app(Settings(database_path=tmp_path / "labelled.db"), store))

    text = c.get("/metrics").text
    assert 'label="Current Average On-Peak Demand"' in text
    assert 'is_average="true"' in text
    assert "srp_current_demand_kw" in text and "5.4" in text

    body = c.get("/api/v1/demand").json()
    assert body["label"] == "Current Average On-Peak Demand"
    assert body["is_average"] is True


def test_a_peak_plan_caption_is_not_flagged_as_average(tmp_path):
    store = Store(tmp_path / "peak.db")
    store.record_demand(
        DemandObservation(
            observed_at=datetime(2026, 8, 26, 16, 0),
            demand_kw=12.7,
            label="Current Peak Demand",
        )
    )
    c = TestClient(create_app(Settings(database_path=tmp_path / "peak.db"), store))

    assert c.get("/api/v1/demand").json()["is_average"] is False
    assert 'is_average="false"' in c.get("/metrics").text


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

    assert empty.get("/api/v1/demand").status_code == 404
    assert empty.get("/metrics").text.startswith("# no SRP data")


def test_bill_projection_endpoint_and_metric(tmp_path):
    from srp_energy.models import BillProjection

    store = Store(tmp_path / "bill.db")
    store.record_bill_projection(
        BillProjection(
            observed_at=datetime(2026, 8, 26, 17, 28),
            amount_usd=430.0,
            basis="Your September bill projection is based on your usage through 8/25/26.",
            source=".h4.text-white",
        )
    )
    c = TestClient(create_app(Settings(database_path=tmp_path / "bill.db"), store))

    body = c.get("/api/v1/bill-projection").json()
    assert body["amount_usd"] == 430.0
    assert body["currency"] == "USD"
    assert "September" in body["basis"]

    assert "srp_projected_bill_dollars 430.0" in c.get("/metrics").text
    assert c.get("/api/v1/bill-projection/history").json()["count"] == 1


def test_bill_projection_404_before_any_fetch(tmp_path):
    store = Store(tmp_path / "nobill.db")
    c = TestClient(create_app(Settings(database_path=tmp_path / "nobill.db"), store))

    assert c.get("/api/v1/bill-projection").status_code == 404


def test_package_root_exports_the_whole_browser_free_surface():
    """A library user must be able to reach hourly data and the projection.

    These were added after the first release and were missing from __all__,
    so `from srp_energy import IntervalReading` failed while the feature
    existed.
    """
    import srp_energy

    for name in (
        "BillProjection", "DailyReading", "DemandObservation", "IntervalReading",
        "Series", "ParseError", "parse_csv", "parse_file", "parse_hourly_csv",
        "parse_hourly_file", "Store",
    ):
        assert hasattr(srp_energy, name), f"{name} is not importable from the package root"
        assert name in srp_energy.__all__, f"{name} is missing from __all__"


def test_importing_the_package_does_not_require_playwright():
    """Parsing and storage must stay usable without the scraper extra."""
    import subprocess
    import sys

    code = (
        "import sys; sys.modules['playwright'] = None; "
        "import srp_energy; print(srp_energy.__version__)"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
