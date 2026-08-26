# SRP Energy Monitor

Collect your **Salt River Project** electricity data — peak demand, usage, generation and net energy — and serve it over a REST API, a Prometheus endpoint, a CSV/JSON export, or a small built-in dashboard.

SRP publishes residential usage only through their web portal. There is no API. This project signs in on your behalf, pulls the numbers, stores them in SQLite, and makes them available to whatever you want to build next.

> Not affiliated with, endorsed by, or supported by Salt River Project. Use it with your own account.

---

## Why

If you are on SRP's demand-based rate plans (E-27, E-28, EZ-3), your bill is driven by a single number: the **highest 30-minute on-peak demand** in the billing cycle. One careless hour in July can set the charge for the entire month. SRP shows you that number on a web page and nowhere else.

This turns it into data:

```console
$ curl -s localhost:8000/api/v1/peak-demand
{"observed_at": "2026-08-26T06:00:04", "demand_kw": 8.4, "unit": "kW", "source": ".srp-red-text strong"}
```

Which means you can alert on it, chart it against temperature, feed it to Home Assistant, or scrape it into Grafana — none of which the portal lets you do.

## What it collects

| Series | What SRP reports | Unit |
|---|---|---|
| `demand` | Daily peak on-peak demand | kW |
| `usage` | Energy consumed, split off-peak / on-peak | kWh |
| `generation` | Energy your solar produced, split off-peak / on-peak | kWh |
| `net` | Net energy — negative when you export more than you draw | kWh |

Every series also carries the day's high and low temperature, which SRP includes in its exports and which is most of what you need to explain a usage spike.

Plus a live scrape of the **current billing cycle's peak demand**, recorded as a time series so you can watch it climb through the month.

---

## Install

Requires Python 3.10+.

```bash
git clone https://github.com/JeremyWhittaker/srp-energy-monitor.git
cd srp-energy-monitor

python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[all]'
playwright install chromium      # the scraper drives a real browser
```

Then configure credentials:

```bash
cp .env.example .env
$EDITOR .env                     # set SRP_USERNAME and SRP_PASSWORD
```

`.env` is gitignored. Credentials are read from the environment, never written to the database, and never returned by the API.

> **MFA is not supported.** If your SRP account has multi-factor authentication enabled, the scraper cannot log in. There is no workaround short of a manual session hand-off.

---

## Use

```bash
srp-energy-monitor fetch      # log in, pull all four series + peak demand, store
srp-energy-monitor show       # print what's in the database
srp-energy-monitor serve      # REST API + dashboard on http://127.0.0.1:8000
srp-energy-monitor export --format json --series demand
srp-energy-monitor import ./old-downloads   # backfill from CSVs you already have
```

A first fetch looks like this:

```console
$ srp-energy-monitor fetch
2026-08-26 06:00:02 INFO    srp_energy.client: logging in to SRP as jeremy@example.com
2026-08-26 06:00:09 INFO    srp_energy.client: SRP login successful
2026-08-26 06:00:14 INFO    srp_energy.client: peak demand 8.40 kW (via .srp-red-text strong)
2026-08-26 06:00:41 INFO    srp_energy.service: fetch complete: 124 readings across ['net', 'generation', 'usage', 'demand']
Peak demand: 8.40 kW
Stored 124 readings across 4 series: net, generation, usage, demand
```

### Keep it current

SRP updates the previous day's data overnight, so once a day is plenty. A cron entry:

```cron
15 6 * * *  cd /opt/srp-energy-monitor && .venv/bin/srp-energy-monitor fetch >> /var/log/srp-fetch.log 2>&1
```

Re-running a fetch is safe: readings upsert on `(series, date)`, so re-imports correct history rather than duplicating it. SRP restates recent days as meter data settles, which is exactly why that matters.

---

## API

`srp-energy-monitor serve` exposes interactive OpenAPI docs at **`/docs`**.

| Endpoint | Returns |
|---|---|
| `GET /api/v1/peak-demand` | Latest billing-cycle peak demand |
| `GET /api/v1/peak-demand/history?days=30` | Peak demand over time |
| `GET /api/v1/readings?series=&start=&end=` | Daily readings, filterable |
| `GET /api/v1/series/{series}?days=30` | One series, shaped for charting |
| `GET /api/v1/summary` | Row counts and date coverage |
| `POST /api/v1/fetch` | Trigger a scrape now |
| `GET /health` | Liveness + coverage |
| `GET /metrics` | Prometheus exposition |
| `GET /` | Dashboard |

```console
$ curl -s 'localhost:8000/api/v1/readings?series=demand&start=2026-08-01' | jq '.readings[-1]'
{
  "series": "demand",
  "usage_date": "2026-08-25",
  "meter_read_date": "2026-08-26",
  "off_peak": null,
  "on_peak": 8.4,
  "high_temp_f": 112.0,
  "low_temp_f": 88.0,
  "unit": "kW",
  "total": null
}
```

> The API has **no authentication**. It binds to `127.0.0.1` by default. If you expose it, put it behind a reverse proxy or a VPN — it will happily hand your usage history to anyone who asks.

### Prometheus

Point a scrape job at `/metrics` and the data lands in Grafana with no glue code:

```yaml
scrape_configs:
  - job_name: srp
    scrape_interval: 15m
    static_configs:
      - targets: ['localhost:8000']
```

Exposed metrics: `srp_peak_demand_kw`, `srp_peak_demand_observed_timestamp_seconds`, `srp_daily_peak_demand_kw`, `srp_daily_energy_kwh{series,period,date}`, `srp_readings_total{series}`.

### As a library

Parsing and storage have no browser dependency, so you can use them on CSVs you already have:

```python
from srp_energy import Series, Store, parse_file

readings = parse_file("srp_demand_20260826_060041.csv", Series.DEMAND)
worst = max(readings, key=lambda r: r.on_peak or 0)
print(f"Worst day: {worst.usage_date} at {worst.on_peak} kW ({worst.high_temp_f}°F)")

Store("./data/srp.db").upsert_readings(readings)
```

---

## How it works

```
  SRP My Account  ──►  client.py   ──►  parser.py  ──►  storage.py  ──►  api.py  ──►  your tools
  (Playwright)         scrape +         CSV → typed     SQLite           REST +        Grafana,
                       CSV export       readings        (upsert)         Prometheus    HA, scripts
```

The design point is that **only `client.py` touches a browser**. Parsing, storage and the API work entirely on saved CSVs, so when SRP changes their markup — and they will — the scraper breaks while everything else keeps serving the history you already collected. Every selector is a cascade with fallbacks rather than a single brittle string, for the same reason.

| File | Role |
|---|---|
| `srp_energy/client.py` | Playwright login, peak-demand scrape, CSV export |
| `srp_energy/parser.py` | SRP CSV → `DailyReading` objects (pure, no I/O) |
| `srp_energy/storage.py` | SQLite schema and queries |
| `srp_energy/service.py` | Wires fetch → parse → store |
| `srp_energy/api.py` | FastAPI app, Prometheus rendering |
| `srp_energy/cli.py` | Command line entry points |
| `srp_energy/web/dashboard.html` | Dependency-free dashboard (no CDN) |

---

## Known limitations

- **No date-range selection.** SRP's export gives you whatever window the portal is showing, typically the last ~31 days. There is no way to request an arbitrary historical range, so deep history has to be accumulated by fetching regularly. If you have old exports, `srp-energy-monitor import` will backfill them.
- **No MFA support.** See above.
- **Single account.** If your login has multiple SRP accounts or meters, the scraper takes whichever the portal defaults to.
- **Scraping is inherently fragile.** SRP can change their markup at any time. When a fetch starts failing, run `srp-energy-monitor fetch --show-browser` to watch it happen, then update the selector cascades in `client.py`.
- **Not real-time.** SRP publishes daily, one day behind. This is a billing-data tool, not a live power monitor.

## Development

```bash
pip install -e '.[dev]'
pytest
```

Tests run against CSV fixtures that mirror SRP's real export format — BOM, CRLF header, quoted numerics, `Combined total` summary row — with invented numbers. No credentials or network needed.

## History

This project is a focused rewrite of the SRP half of an older personal
`eg4-srp-monitor`, which combined SRP scraping with EG4 inverter and Enphase
solar polling in a single 3,000-line Flask app. The SRP logic — login flow,
selector cascades, CSV export sequence — is carried over and hardened here;
everything inverter-specific was left behind.

## License

MIT — see [LICENSE](LICENSE).
