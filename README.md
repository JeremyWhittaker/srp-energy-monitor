# SRP Energy Monitor

Collect your **Salt River Project** electricity data — bill-cycle demand, usage, generation and net energy — and serve it over a REST API, a Prometheus endpoint, a CSV/JSON export, or a small built-in dashboard.

SRP publishes residential usage only through their web portal. There is no API. This project signs in on your behalf, pulls the numbers, stores them in SQLite, and makes them available to whatever you want to build next.

> Not affiliated with, endorsed by, or supported by Salt River Project. Use it with your own account.

---

## Why

If you are on one of SRP's demand-based price plans, your bill is driven by a single number that SRP shows on a web page and nowhere else. **Which** number depends on your plan:

- On a **peak**-demand plan (E-27, E-28) it is the highest on-peak demand in the cycle — one careless hour in July sets the charge for the whole month.
- On an **average**-demand plan ("Customer Generation Avg Demand Plan") it is the mean on-peak demand across the cycle so far.

Those are very different quantities, so this tool captures SRP's own caption alongside the value rather than assuming either:

```console
$ curl -s localhost:8000/api/v1/demand
{
  "observed_at": "2026-08-26T16:19:53",
  "demand_kw": 5.4,
  "unit": "kW",
  "label": "Current Average On-Peak Demand",
  "is_average": true,
  "source": ".srp-red-text strong"
}
```

Check `label` / `is_average` before you alert on it. Treating a cycle average as a peak (or the reverse) silently misreports the number that drives your bill.

Which means you can alert on it, chart it against temperature, feed it to Home Assistant, or scrape it into Grafana — none of which the portal lets you do.

## What it collects

| Series | What SRP reports | Unit |
|---|---|---|
| `demand` | Daily on-peak demand, as published in SRP's Demand export | kW |
| `usage` | Energy consumed, split off-peak / on-peak | kWh |
| `generation` | Energy your solar produced, split off-peak / on-peak | kWh |
| `net` | Net energy — negative when you export more than you draw | kWh |

Every series also carries the day's high and low temperature, which SRP includes in its exports and which is most of what you need to explain a usage spike.

Two more live scrapes from the same page: SRP's **projected bill amount** for
the cycle in progress (with the sentence saying what month and through what
date it projects), and the **current bill cycle's headline demand figure** — with the caption SRP published it under — recorded as a time series so you can watch it move through the month.

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
srp-energy-monitor fetch      # log in, pull all four series + the demand figure, store
srp-energy-monitor hourly     # pull hourly interval data for a date range
srp-energy-monitor show       # print what's in the database
srp-energy-monitor serve      # REST API + dashboard on http://127.0.0.1:8000
srp-energy-monitor export --format json --series demand
srp-energy-monitor import ./old-downloads   # backfill from CSVs you already have
```

A first fetch looks like this:

```console
$ srp-energy-monitor fetch
2026-08-26 16:19:37 INFO    srp_energy.client: logging in to SRP as example
2026-08-26 16:19:46 INFO    srp_energy.client: SRP login successful
2026-08-26 16:19:53 INFO    srp_energy.client: Current Average On-Peak Demand: 5.40 kW (via .srp-red-text strong)
2026-08-26 16:20:11 INFO    srp_energy.service: fetch complete: 124 readings across ['usage', 'generation', 'net', 'demand']
Current Average On-Peak Demand: 5.40 kW
Stored 124 readings across 4 series: usage, generation, net, demand
```

### Hourly data

SRP's Hourly/Interval view exposes per-hour kWh and accepts an explicit date
range, so history can be pulled a window at a time:

```bash
srp-energy-monitor hourly --days 3                      # last 3 days, usage
srp-energy-monitor hourly --start 2026-08-01 --end 2026-08-25
srp-energy-monitor hourly --days 2 --series usage --series net --show
srp-energy-monitor hourly --days 1 \
    --series usage --series generation --series net --series demand
```

Each series is exported separately, because the hourly export follows whichever
chart is on screen — without selecting one you silently get net energy under a
name implying something else. Default is `usage`.

**Demand intervals are half-hourly, not hourly**, and are measured in kW rather
than kWh — that is the granularity demand charges are actually assessed on, so a
day of demand is 48 rows while the three energy series are 24.

Timestamps are Arizona local time. That needs no offset precisely because
Arizona does not observe daylight saving, so there are no repeated or missing
hours to disambiguate.

```console
$ srp-energy-monitor hourly --days 1 --show
Stored 24 hourly intervals for 2026-08-25 .. 2026-08-25

hour              series       off-peak   on-peak    total
2026-08-25 13:00  usage            4.80      0.00     4.80
2026-08-25 14:00  usage            0.00      3.50     3.50
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
| `GET /api/v1/demand` | Latest bill-cycle demand figure, with SRP's caption |
| `GET /api/v1/demand/history?days=30` | Demand observations over time |
| `GET /api/v1/bill-projection` | SRP's projected bill amount for this cycle |
| `GET /api/v1/bill-projection/history?days=30` | Projection over time |
| `GET /api/v1/readings?series=&start=&end=` | Daily readings, filterable |
| `GET /api/v1/intervals?series=&start=&end=` | Hourly interval readings |
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
  "on_peak": 0.7,
  "high_temp_f": 114.0,
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

Exposed metrics: `srp_projected_bill_dollars`, `srp_current_demand_kw{label,is_average}`, `srp_current_demand_observed_timestamp_seconds`, `srp_daily_on_peak_demand_kw{date}`, `srp_daily_energy_kwh{series,period,date}`, `srp_readings_total{series}`.

The `label` and `is_average` labels on `srp_current_demand_kw` carry SRP's own caption, so an alert rule can assert which quantity it is reading rather than assuming.

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
| `srp_energy/client.py` | Playwright login, demand-figure scrape, CSV export |
| `srp_energy/parser.py` | SRP CSV → `DailyReading` objects (pure, no I/O) |
| `srp_energy/storage.py` | SQLite schema and queries |
| `srp_energy/service.py` | Wires fetch → parse → store |
| `srp_energy/api.py` | FastAPI app, Prometheus rendering |
| `srp_energy/cli.py` | Command line entry points |
| `srp_energy/web/dashboard.html` | Dependency-free dashboard (no CDN) |

---

## Known limitations

- **Cloudflare fronts the portal.** A default Playwright browser is refused outright with an "Access Denied / Ray ID" page before SRP serves any markup. The client therefore presents a realistic desktop fingerprint (real user agent, 1440×900 viewport, `America/Phoenix` timezone, no `navigator.webdriver` flag). This is presentation, not evasion of an access control — the scraper still signs in with your own credentials to read your own data. If Cloudflare tightens further, this is the first thing that will break.
- **The headline demand figure means different things on different plans.** It is an average on some price plans and a maximum on others, so it is stored and served with SRP's own caption (`label`, `is_average`) rather than a hard-coded meaning. If SRP restyles that card so the caption cannot be read, the value is still recorded but `label` is `null` and a warning is logged — do not assume a peak in that case.
- **Daily exports have no date-range control.** The four daily series give you whatever window the portal is showing, typically the last ~31 days. The *hourly* view does accept an explicit range (`srp-energy-monitor hourly --start --end`), so it is the way to reach further back. If you have old daily exports, `srp-energy-monitor import` will backfill them.
- **Hourly requests are chunked.** SRP renders every interval in the window before it will export, so a multi-month request times out. `fetch_hourly` splits long ranges into 31-day windows automatically.
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
