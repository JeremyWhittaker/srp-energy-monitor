<div align="center">

# SRP Energy Monitor

**Your SRP demand charge, as a number you can alert on.**

Salt River Project has no documented or supported API. This signs in on your behalf, collects
the **kW demand figure that sets your bill** — plus usage, solar generation, net energy and
SRP's own bill projection — and serves them as **REST JSON**, **Prometheus metrics**,
**CSV/JSON exports** and a **dashboard**.

It is, as far as we can tell, the only SRP tool that exposes the demand number at all.
[Everything else](#related-projects) returns kWh and dollars.

[![CI](https://github.com/JeremyWhittaker/srp-energy-monitor/actions/workflows/ci.yml/badge.svg)](https://github.com/JeremyWhittaker/srp-energy-monitor/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Home Assistant ready](https://img.shields.io/badge/Home%20Assistant-ready-41BDF5?logo=homeassistant&logoColor=white)](#home-assistant)

<img src="docs/assets/dashboard.png" alt="The SRP Energy Monitor dashboard: current demand, projected bill, and a 30-day chart" width="900">

<sub>The built-in dashboard. Dependency-free, no CDN, light and dark. Numbers above are synthetic.</sub>

</div>

---

> [!IMPORTANT]
> Not affiliated with, endorsed by, or supported by Salt River Project. It signs in with
> **your** credentials to read **your own** account, and nothing else.

## Contents

[Why](#why-this-exists) · [Quickstart](#quickstart) · [The demand number](#the-demand-number-read-this-one)
· [Collecting data](#collecting-data) · [API](#the-api) · [Prometheus](#prometheus-and-grafana)
· [Home Assistant](#home-assistant) · [Library](#as-a-python-library) · [How it works](#how-it-works)
· [Limitations](#limitations) · [FAQ](#faq) · [Related projects](#related-projects) · [Contributing](#contributing)

---

## Why this exists

If you are on one of SRP's **demand-based price plans**, a single number drives a large slice
of your bill — and SRP shows it on one web page, in one card, with no way to get at it.

One careless hour in July can set a charge that applies to the whole billing cycle.

SRP will text you that your *usage* was unusually high, after the fact. It will not tell you
that your *demand* is climbing, at a threshold you chose, in time to do something about it —
and it will not hand the number to your thermostat.

This makes that number, and everything around it, a normal HTTP resource:

```console
$ curl -s localhost:8000/api/v1/demand
{
  "observed_at": "2026-08-26T06:00:41",
  "demand_kw": 5.4,
  "unit": "kW",
  "label": "Current Average On-Peak Demand",
  "is_average": true,
  "source": ".srp-red-text strong"
}
```

## What you get

| | |
|---|---|
| **REST API** | JSON for every series, with OpenAPI docs at `/docs` |
| **Prometheus** | `/metrics` endpoint — point Grafana at it, no glue code |
| **Dashboard** | Built in, dependency-free, dark mode, no CDN |
| **CSV / JSON export** | `export --format json --series demand` |
| **Python library** | Parsing and storage work on CSVs with no browser |
| **SQLite** | Your history, upserted, on your disk |
| **Hourly intervals** | Per-hour kWh and half-hourly kW, over any date range |
| **Bill projection** | SRP's own projected bill amount, tracked over the cycle |

### The data

| Series | What SRP reports | Unit |
|---|---|---|
| `demand` | Daily on-peak demand | kW |
| `usage` | Energy consumed, split off-peak / on-peak | kWh |
| `generation` | Energy your solar produced, split off-peak / on-peak | kWh |
| `net` | Net energy — negative when you export more than you draw | kWh |

Every daily row also carries the day's **high and low temperature**, which SRP includes in its
exports and which is most of what you need to explain a usage spike.

Two more values come from live scrapes of the same page: SRP's **projected bill amount** for
the cycle in progress, and the **current bill cycle's headline demand figure** — both recorded
as time series, so you can watch them climb through the month.

---

## Quickstart

Requires **Python 3.10+**. Not on PyPI yet — install from a checkout:

```bash
git clone https://github.com/JeremyWhittaker/srp-energy-monitor.git
cd srp-energy-monitor

python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[all]'
playwright install chromium        # the scraper drives a real browser

cp .env.example .env
$EDITOR .env                       # set SRP_USERNAME and SRP_PASSWORD

srp-energy-monitor fetch           # collect
srp-energy-monitor serve           # http://127.0.0.1:8000
```

That's it. Full settings reference in **[docs/CONFIGURATION.md](docs/CONFIGURATION.md)**.

> [!WARNING]
> **MFA is not supported.** If your SRP account has multi-factor authentication enabled, the
> scraper cannot log in, and there is no workaround short of a manual session hand-off.

`.env` is gitignored. Your password is read from the environment, never written to the
database, never logged, and never returned by the API. (Your *username* is logged at INFO on
each login, so scrub it before pasting a log into an issue.)

---

## The demand number, read this one

Not every SRP plan has a demand charge, and the plans that do **do not all measure the same
thing**. As of the November 2025 price plan changes:

| Plan | Name | Demand charge | What it measures |
|---|---|---|---|
| **E-16** | Manage Demand 5–10 p.m. and Save | Yes | Average of each day's highest **60-minute** on-peak kW |
| **E-15** | Average Demand (legacy, solar) | Yes | Average of each day's highest **30-minute** on-peak kW |
| **E-27** | Customer Generation (legacy, solar) | Yes | The single **highest 30-minute** on-peak kW in the cycle |
| E-28 | Conserve 6–9 p.m. and Save | No | — |
| E-13 / E-14 | Time-of-Use Export / EV Export (legacy, solar) | No | — |
| E-21 (EZ-3) / E-23 (Basic) / E-24 (M-Power) / E-26 (TOU) | | No | — |

E-27 is a **maximum**; E-15 and E-16 are **averages**. Reporting one as the other misstates the
number that drives the charge. **E-16 is open to every residential customer, solar or not** —
so a demand charge is no longer a solar-owner's problem.

Because of that, this tool never assumes. It captures SRP's own caption alongside the value:

```json
{ "demand_kw": 5.4, "label": "Current Average On-Peak Demand", "is_average": true }
```

**Check `label` / `is_average` before you alert on it.** During development, reading an average
as a peak understated the real maximum by more than 2× — 5.4 kW against an actual 12.7 kW.

Plan lineups change; SRP restructured them in November 2025. The caption is scraped from the
page rather than hard-coded precisely so this keeps working when they change again.

If SRP restyles that card so the caption cannot be read, the value is still recorded, `label`
is `null`, and a warning is logged. Don't assume a peak in that case either.

---

## Collecting data

```bash
srp-energy-monitor fetch          # all four series + the demand figure + bill projection
srp-energy-monitor hourly         # hourly interval data for a date range
srp-energy-monitor show           # print what's in the database
srp-energy-monitor serve          # REST API + dashboard
srp-energy-monitor export --format json --series demand
srp-energy-monitor import ./old-downloads    # backfill from CSVs you already have
srp-energy-monitor backfill      # years of history from the JSON API, no browser

srp-energy-monitor fetch --no-demand         # CSVs only, skip the live scalars
srp-energy-monitor fetch --show-browser      # run headed when selectors break
```

A fetch looks like this:

```console
$ srp-energy-monitor fetch
2026-08-26 06:00:32 INFO    srp_energy.client: logging in to SRP as example
2026-08-26 06:00:39 INFO    srp_energy.client: SRP login successful
2026-08-26 06:00:41 INFO    srp_energy.client: Current Average On-Peak Demand: 5.40 kW (via .srp-red-text strong)
2026-08-26 06:01:04 INFO    srp_energy.service: fetch complete: 124 readings across ['usage', 'generation', 'net', 'demand']
Current Average On-Peak Demand: 5.40 kW
Projected bill: $214.86  (Your projected bill amount for August is based on usage through 8/25.)
Stored 124 readings across 4 series: usage, generation, net, demand
```

And `show` prints the state of the database:

```console
$ srp-energy-monitor show --days 2
Current Average On-Peak Demand: 5.40 kW  (2026-08-26T06:00:41)

  demand        31 days  2026-07-26 .. 2026-08-25
  generation    31 days  2026-07-26 .. 2026-08-25
  net           31 days  2026-07-26 .. 2026-08-25
  usage         31 days  2026-07-26 .. 2026-08-25
Projected bill: $214.86
  Your projected bill amount for August is based on usage through 8/25.
  hourly        48 intervals  2026-08-24T00:00 .. 2026-08-25T23:00

date         series       off-peak   on-peak  high   low
2026-08-25   demand              -      5.12   104    79
2026-08-25   generation      33.67     11.24   104    79
2026-08-25   net             21.28      2.93   104    79
2026-08-25   usage           54.95     14.17   104    79
```

### Hourly intervals

SRP's Hourly/Interval view accepts an explicit date range, so history can be pulled a window
at a time:

```bash
srp-energy-monitor hourly --days 3                      # last 3 days, usage
srp-energy-monitor hourly --start 2026-08-01 --end 2026-08-25
srp-energy-monitor hourly --days 2 --series usage --series net --show
srp-energy-monitor hourly --days 1 \
    --series usage --series generation --series net --series demand
```

Each series must be exported separately, because **the hourly export follows whichever chart
is on screen** — without selecting one you silently get net energy under a name implying
something else. The default is `usage` alone, so pass `--series` for anything more.

**Demand intervals are half-hourly and in kW**, not hourly kWh — that is the granularity
demand charges are actually assessed on. A day of demand is 48 rows; the three energy series
are 24.

Timestamps are Arizona local time, and need no offset precisely because Arizona does not
observe daylight saving. There are no repeated or missing hours to disambiguate.

### Keeping it current

SRP settles the previous day overnight, so once a day is plenty — and re-running a fetch is
safe: readings upsert on `(series, date)`, so re-imports **correct** history rather than
duplicating it. SRP restates recent days as meter data settles, which is exactly why that
matters.

The repo ships systemd user units that do this at 07:30 with jitter:

```bash
cp deploy/srp-fetch.service deploy/srp-fetch.timer ~/.config/systemd/user/
$EDITOR ~/.config/systemd/user/srp-fetch.service   # point it at your checkout
systemctl --user daemon-reload
systemctl --user enable --now srp-fetch.timer
loginctl enable-linger "$USER"                     # or it only runs while logged in
```

Or, if you would rather use cron:

```cron
30 7 * * *  cd /opt/srp-energy-monitor && .venv/bin/srp-energy-monitor fetch >> /var/log/srp-fetch.log 2>&1
```

---

## The API

`srp-energy-monitor serve` exposes interactive OpenAPI docs at **`/docs`**.

| Endpoint | Returns |
|---|---|
| `GET /api/v1/demand` | Latest bill-cycle demand figure, with SRP's caption |
| `GET /api/v1/demand/history?days=30` | Demand observations over time |
| `GET /api/v1/bill-projection` | SRP's projected bill amount for this cycle |
| `GET /api/v1/bill-projection/history?days=30` | Projection over time |
| `GET /api/v1/readings?series=&start=&end=` | Daily readings, filterable |
| `GET /api/v1/intervals?series=&start=&end=` | Hourly/half-hourly interval readings |
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
  "on_peak": 5.12,
  "high_temp_f": 104.0,
  "low_temp_f": 79.0,
  "unit": "kW",
  "total": null
}
```

> [!CAUTION]
> **The API has no authentication.** It binds to `127.0.0.1` by default. If you expose it, put
> it behind a reverse proxy, a VPN or a private overlay network — it will happily hand your
> full usage history to anyone who asks, and interval data is an excellent occupancy sensor.
> `/health` also reports your database path. See [SECURITY.md](SECURITY.md).

## Prometheus and Grafana

Point a scrape job at `/metrics` and the data lands in Grafana with no glue code:

```yaml
scrape_configs:
  - job_name: srp
    scrape_interval: 15m
    static_configs:
      - targets: ['localhost:8000']
```

```prometheus
srp_current_demand_kw{label="Current Average On-Peak Demand",is_average="true"}  5.4
srp_current_demand_observed_timestamp_seconds                                   1787749241
srp_projected_bill_dollars                                                      214.86
srp_daily_on_peak_demand_kw                                                     5.12
srp_daily_energy_kwh{series="usage",period="on_peak"}                           14.17
srp_daily_reading_timestamp_seconds{series="usage"}                             1787641200
srp_stored_readings{series="usage"}                                             31
```

The date being reported is deliberately **not** a label — Prometheus identifies a series by its
label set, so `date="…"` would mint a fresh, 24-hour-lived series every day that no range query
could span. It is published as `srp_daily_reading_timestamp_seconds` instead, which is also how
you alert on the data going stale:

```yaml
- alert: SRPDataStale
  expr: time() - srp_daily_reading_timestamp_seconds{series="usage"} > 172800
  annotations:
    summary: "SRP usage data has not advanced in 2 days — the scraper may be broken"
```

The `label` and `is_average` labels carry SRP's own caption, so an alert rule can **assert**
which quantity it is reading rather than assuming:

```yaml
- alert: SRPPeakDemandClimbing
  expr: srp_current_demand_kw{is_average="false"} > 12
  for: 1h
  annotations:
    summary: "On-peak demand is {{ $value }} kW and setting this cycle's charge"
```

## Home Assistant

A **custom integration** ships in this repo at `custom_components/srp_energy_monitor/`. Copy that
directory into your Home Assistant `config/custom_components/`, restart, then add it from
**Settings → Devices & Services**. It asks for your account number, username and password.

It talks to SRP directly over the JSON API — no browser on the Home Assistant box, and
`"requirements": []`, so nothing is installed at setup time and nothing can fail to build a wheel.

Six entities:

| Entity | Unit | What it is |
|---|---|---|
| Current demand | kW | What the portal prints for the cycle in progress — a mean on E-15/E-16, a maximum on E-27. The `is_average` attribute says which. |
| Billed cycle peak | kW | The single highest on-peak half hour of the cycle, with the timestamp it happened |
| Bill projection | USD | SRP's own projection for the cycle |
| Daily consumption / generation / net | kWh | The most recent settled day |

It also writes **long-term statistics**, back-filled on first setup: hourly kWh for consumption,
generation and net, plus half-hourly demand stored as an hourly mean with min and max. The `max`
column is the one worth charting — it preserves the highest half hour of each hour, which is the
granularity demand is billed on.

> [!NOTE]
> This is **not** Home Assistant's built-in `srp_energy` integration, and it uses a different
> domain so the two can coexist. The built-in one exposes a single kWh total and cannot see
> demand at all.

### Or just point REST sensors at the API

If you would rather run the server and keep Home Assistant thin, the REST platform works against
any host running `srp-energy-monitor serve` — use that host's address, not `127.0.0.1`, unless
Home Assistant is on the same machine:

```yaml
sensor:
  - platform: rest
    name: SRP demand
    resource: http://YOUR-HOST:8000/api/v1/demand
    value_template: "{{ value_json.demand_kw }}"
    unit_of_measurement: kW
    device_class: power
    state_class: measurement
    json_attributes: [label, is_average, observed_at]
    scan_interval: 3600

  - platform: rest
    name: SRP billed cycle peak
    resource: http://YOUR-HOST:8000/api/v1/cycle-peaks?limit=1
    value_template: "{{ value_json.peaks[0].on_peak_high_demand_kw }}"
    unit_of_measurement: kW
    device_class: power
    state_class: measurement
    scan_interval: 3600
```

## As a Python library

Parsing and storage have **no browser dependency**, so you can use them on CSVs you already
downloaded by hand:

```python
from srp_energy import Series, Store, parse_file, parse_hourly_file

readings = parse_file("srp_demand_20260826_060041.csv", Series.DEMAND)
worst = max(readings, key=lambda r: r.on_peak or 0)
print(f"Worst day: {worst.usage_date} at {worst.on_peak} kW ({worst.high_temp_f}°F)")

store = Store("./data/srp.db")
store.upsert_readings(readings)

# Hourly exports parse the same way; demand is half-hourly and in kW.
store.upsert_intervals(parse_hourly_file("srp_hourly_usage_20260825_20260826.csv"))

# And read it all back without a browser anywhere in sight.
print(store.latest_demand(), store.latest_bill_projection())
```

---

## How it works

```
  SRP My Account  ──►  client.py   ──►  parser.py  ──►  storage.py  ──►  api.py  ──►  your tools
  (Playwright)         scrape +         CSV → typed     SQLite           REST +        Grafana,
                       CSV export       readings        (upsert)         Prometheus    HA, scripts
```

The load-bearing design decision is that **only `client.py` touches a browser**. Parsing,
storage, the CLI and the API work entirely on saved CSVs — so when SRP changes their markup,
and they will, the scraper breaks while everything else keeps serving the history you already
collected. Every selector is a cascade with fallbacks rather than a single brittle string, for
the same reason.

| Path | Role |
|---|---|
| `srp_energy/client.py` | Playwright login, live scrapes, CSV export |
| `srp_energy/parser.py` | SRP CSV → typed readings (pure, no I/O) |
| `srp_energy/storage.py` | SQLite schema and queries |
| `srp_energy/service.py` | Wires fetch → parse → store |
| `srp_energy/api.py` | FastAPI app, Prometheus rendering |
| `srp_energy/cli.py` | Command line entry points |
| `srp_energy/models.py` | Typed records: readings, intervals, demand, projection |
| `srp_energy/config.py` | Environment / `.env` settings |
| `srp_energy/__init__.py` | Public library surface (models, parsers, `Store`) |
| `srp_energy/web/dashboard.html` | Dependency-free dashboard (no CDN) |
| `deploy/` | systemd user units for the daily fetch |
| `docs/` | [Configuration](docs/CONFIGURATION.md) and [troubleshooting](docs/TROUBLESHOOTING.md) |
| `CLAUDE.md` | Handoff notes: the traps, and why the code is shaped this way |

---

## Limitations

Stated plainly, because finding these out the hard way is worse.

- **Cloudflare fronts the portal.** A default Playwright browser is refused outright with an
  "Access Denied / Ray ID" page before SRP serves any markup. The client therefore presents a
  realistic desktop fingerprint (real user agent, 1440×900 viewport, `America/Phoenix`
  timezone, no `navigator.webdriver` flag). This is presentation, not evasion of an access
  control — it still signs in with your own credentials to read your own data. If Cloudflare
  tightens further, this is the first thing that will break.
- **No MFA support.** If your account has it enabled, this cannot log in.
- **The headline demand figure means different things on different plans.** See
  [above](#the-demand-number-read-this-one) — check `label`.
- **Daily exports have no date-range control.** The four daily series give you whatever window
  the portal shows, typically the last ~31 days. Three ways further back: the *hourly* view
  accepts an explicit range, old exports can be replayed with `srp-energy-monitor import`, and
  `srp-energy-monitor backfill` pulls the account's whole retained daily history — 812 days on
  the account this was built against — in a single JSON call.
- **Hourly requests are chunked.** SRP renders every interval in the window before it will
  export, so a multi-month request times out. Long ranges are split into 31-day windows
  automatically.
- **One account, one meter.** If your login has several, the scraper takes whichever the
  portal defaults to.
- **Scraping is inherently fragile.** SRP can change their markup at any time. When a fetch
  starts failing, run `srp-energy-monitor fetch --show-browser` to watch it happen, then update
  the selector cascades in `client.py`. See
  **[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)**.
- **Not real-time.** SRP publishes daily, one day behind. This is a billing-data tool, not a
  live power monitor.

## FAQ

<details>
<summary><b>Is this allowed? Will SRP shut my account down?</b></summary>

It signs in with your own credentials and reads your own data, once a day — the same thing you
would do in a browser. It is not a bulk scraper and it does not touch anyone else's account.
That said, SRP's terms are between you and SRP, this is unaffiliated software, and you run it
at your own risk. Don't hammer it; once a day is genuinely enough.
</details>

<details>
<summary><b>Why not use SRP's Green Button / data download?</b></summary>

Because it is a manual download of a file, on a page, when you remember to do it. This is the
same data on a schedule, in a database, behind an API, with the demand figure and bill
projection that the download does not include.
</details>

<details>
<summary><b>Which SRP plans have a demand charge?</b></summary>

**E-16** (Manage Demand 5–10 p.m. and Save), **E-15** (Average Demand, legacy) and **E-27**
(Customer Generation, legacy) all bill on demand. **E-28**, **E-13**, **E-14**, **E-21/EZ-3**,
**E-23/Basic**, **E-24/M-Power** and **E-26** do not. See [the table above](#the-demand-number-read-this-one)
for what each one measures — E-27 is a true peak, E-15 and E-16 are averages.

E-16 is open to all residential customers as of November 2025, so demand charges are no longer
limited to solar accounts. Check your own bill; this is a summary of SRP's published tariffs,
not advice, and SRP changes them.
</details>

<details>
<summary><b>Do I need solar for this to be useful?</b></summary>

No — and since E-16 opened demand charges to every residential customer, that is truer than it
used to be. `usage` and `demand` are the point on a demand plan, with or without panels.
`generation` and `net` will simply be empty. Development happened on an account *with* solar,
so if the portal behaves differently without it, please
[open an issue](https://github.com/JeremyWhittaker/srp-energy-monitor/issues) — that is a
genuinely useful report.
</details>

<details>
<summary><b>Does it work for APS?</b></summary>

No. APS is a different utility with a different portal. The parsing and storage layers are
generic enough to reuse, but the scraper is SRP-specific.
</details>

<details>
<summary><b>Where does my data go?</b></summary>

A SQLite file on your disk, and nowhere else. There is no telemetry, no cloud, and no network
traffic other than to SRP. See [SECURITY.md](SECURITY.md).
</details>

<details>
<summary><b>Can I run it in Docker?</b></summary>

There is no image yet — Playwright needs a browser in the container, so it is a real piece of
work rather than a three-line Dockerfile. It is on the
[wanted list](CONTRIBUTING.md#things-that-would-genuinely-help).
</details>

## Related projects

> Not to be confused with the [`srpenergy`](https://pypi.org/project/srpenergy/) PyPI package or
> Home Assistant's built-in `srp_energy` integration. Different projects, similar names.

| Project | Returns | Browser? | Demand kW? |
|---|---|---|---|
| [srpenergy](https://github.com/lamoreauxlab/srpenergy-api-client-python) | Hourly kWh and cost | No | **No** |
| [HA `srp_energy`](https://www.home-assistant.io/integrations/srp_energy/) (built in) | Consumption and cost statistics | No | **No** |
| [SRPNET-api](https://github.com/Spencer-Sawyer/SRPNET-api) | Daily/hourly usage, rate code | No | **No** |
| SRP's own portal / Green Button | Manual CSV download, by hand, per chart | — | **No** |
| **This** | Demand kW, billed cycle peak, bill projection, usage, generation, net, temperature | Optional | **Yes** |

Every other client returns kWh and dollars. None of them exposes the kW demand figure or the
bill projection — which, on E-15/E-16/E-27, is the number that actually moves your bill.

The honest trade: **they need only Python, and so does this unless you want the browser.** The
`backfill` backend is stdlib-only; `pip install srp-energy-monitor` with no extras pulls nothing
at all. Chromium is needed only for `fetch`. If you want a tiny dependency to read hourly kWh in
a script, `srpenergy` is well maintained and a better fit. If you want the demand number, this is
currently the only option.

### Two backends, and why both exist

**`fetch`** drives the portal with Playwright and consumes the **CSV exports SRP itself offers
you** — a contract SRP publishes to its own customers.

**`backfill`** uses the private JSON API at `myaccount.srpnet.com/myaccountapi/api/...` — the same
undocumented, unversioned endpoints `srpenergy` calls — with no browser at all.

Earlier versions of this README argued the browser was necessary because the demand card's
*caption* and the bill projection were rendered UI with no JSON equivalent. **That was wrong.**
`isAverageDemandRate` is a boolean stating which quantity the plan bills, `payments/billprojection`
returns the projection as a field, and `demandList` carries 48 half-hourly kW readings a day that
no public client parses. The billed demand charge is recoverable exactly: `max(onPeakDemand)` over
a cycle matched SRP's own figure to the interval — 12.7 kW at 2026-08-18 19:30.

So the honest reason to keep both is not capability, it is **failure mode**. A markup change
breaks the scraper; a field rename breaks the JSON client; a Cloudflare mood breaks whichever one
you are holding. Neither can break the parser or the history already collected.

What each is better at:

| | `fetch` (browser) | `backfill` (JSON) |
|---|---|---|
| Daily history reach | ~31 days, whatever the portal shows | **812 days in one call** |
| Hourly history reach | date-picker range, chunked | at least back to 2024-08 |
| Billed cycle peak + timestamp | not available | **yes** (`highDemandInfoList`) |
| Demand figure on an *average* plan | scraped exactly | reconstructed, ±0.05 kW |
| Install cost | Chromium download | none — stdlib only |
| Blocked by Cloudflare | rarely | sometimes, unpredictably |

Two things to know before using the JSON path. Requests wider than **31 days silently truncate**
to the most recent month with a 200 and no warning, so ranges are chunked automatically. And SRP's
list names do not match its own CSV series names — the list called `hourlyUsageList` is *net*
metered energy, not consumption. See [CLAUDE.md](CLAUDE.md) for the full mapping.

---

## Development

```bash
pip install -e '.[dev]'
pytest                       # 149 tests, no network, no credentials
ruff check . && ruff format --check .
```

Tests run against CSV fixtures that mirror SRP's real export format — BOM, CRLF header, quoted
numerics, the ragged `12:0 AM` clock, `Combined total` summary row — with invented numbers. No
credentials or network needed, which makes them fast and safe.

It also means **a green suite does not prove the scraper works**. Every markup-related bug this
project has hit passed the tests. Verify a scraper change against the live site:

```bash
srp-energy-monitor fetch && srp-energy-monitor show --days 7
```

## Contributing

Bug reports about a broken scraper are as valuable as code here — SRP changes their portal
without notice, and one screenshot of `--show-browser` usually identifies the fix.

Especially wanted: **confirmation from a different price plan** (E-27/E-28 caption the demand
card differently), **an account without solar**, a **Home Assistant custom component**, and a
shareable **Grafana dashboard**. See [CONTRIBUTING.md](CONTRIBUTING.md).

If this saved you an afternoon, a ⭐ helps other SRP customers find it.

## History

A focused rewrite of the SRP half of an older personal project that combined SRP scraping with
EG4 inverter and Enphase solar polling in a single ~3,000-line Flask app. The SRP logic — login
flow, selector cascades, CSV export sequence — is carried over and hardened here; everything
inverter-specific was left behind.

## License

[MIT](LICENSE).

*Salt River Project and SRP are trademarks of Salt River Project Agricultural Improvement and
Power District. This project is not affiliated with, endorsed by, or supported by SRP.*
