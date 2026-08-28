# Changelog

Notable changes to this project. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **A second, browser-free backend** (`srp_energy.jsonapi`, `srp-energy-monitor backfill`) over
  SRP's private JSON API. Stdlib only — the base install still declares no dependencies, and
  Chromium is now needed for `fetch` alone. It reaches **812 days** of daily history in one call
  against ~31 from the portal export, and hourly data back to at least 2024-08.
- **Billed cycle peaks** (`cycle_peak` table, schema v5, `CyclePeak` model). SRP's
  `highDemandInfoList` gives the single highest on-peak half hour of each billing cycle with the
  timestamp it happened — the figure the demand charge is actually computed from, which nothing
  in this project previously stored. Verified against SRP's own number: 12.7 kW at
  2026-08-18 19:30.
- `SRP_ACCOUNT_ID` setting, required by the JSON backend only. The browser backend reads the
  account from the signed-in session and ignores it.
- **A Home Assistant custom integration** (`custom_components/srp_energy_monitor/`). Six entities
  including the two demand figures that must never be confused -- the cycle figure the portal
  prints, and the billed peak the charge is actually computed from -- plus back-filled long-term
  statistics for hourly energy and half-hourly demand. Declares no requirements: it uses the
  standard library and the aiohttp Home Assistant already ships, so nothing can fail to install
  on HAOS.
- `GET /api/v1/cycle-peaks`, and `latest_cycle_peak` on `/api/v1/summary`.
- The dashboard is redesigned around the demand figure rather than treating it as one of five
  equal cards: billing-cycle progress, the billed peak shown beside the cycle average with the
  distinction spelled out, a threshold line and highlighted bar marking the peak on the demand
  chart, and a light/dark toggle. Still one file, still no external resources.

### Fixed
- **The README's central argument was wrong.** It claimed the browser was necessary because the
  demand card's caption and the bill projection were rendered UI with no JSON equivalent. Both
  are JSON fields (`isAverageDemandRate`, `payments/billprojection`), and `demandList` carries
  48 half-hourly kW readings a day that no public client parses. Rewritten as a comparison of
  failure modes, which is the real reason to keep both paths.

### Notes for anyone building on the JSON path
- Requests wider than **31 days truncate silently** — HTTP 200, most recent month only, no
  warning. `chunk_date_range` guards every range; a naive wide request produces a backfill that
  looks complete and is not.
- **SRP's list names do not match its own CSV series names.** `hourlyUsageList` is *net* metered
  energy, not consumption; consumption is `hourlyConsumptionList`. Anything summing the former
  and calling it usage reports a different quantity than the bill's kWh line. Full mapping in
  `CLAUDE.md`.
- A **403 is not an authentication failure**. Within one hour on one network, `requests` and
  `curl` were refused where stdlib `urllib` was not. `AccessBlocked` reports this as a transport
  problem rather than sending people to check a password that was never wrong.

### Fixed (documentation)
- The README claimed SRP has "no API". It has no *documented* API, but there is a private JSON
  endpoint behind the portal that other clients call without a browser. Corrected, with an
  explicit section on why this project drives the portal anyway.
- The README named E-27 and **E-28** as the peak-demand plans. Per SRP's November 2025 tariffs
  E-28 has no demand charge at all; E-27 bills a true 30-minute maximum, while E-15 and E-16
  bill averages of daily maxima. Replaced with a plan table, and noted that E-16 opened demand
  charges to all residential customers rather than solar accounts only.
- Documentation accuracy pass across `docs/`, `.env.example`, `requirements.txt` and `deploy/`:
  credentials are no longer described as "never logged" (the username is), `/health` is no longer
  described as returning nothing but a boolean, the `import` example no longer omits its required
  argument, and the inline systemd examples now match the units actually shipped.

### Added
- Public-repository scaffolding: CI on Python 3.10–3.13 (`pytest`, `ruff`, a package build
  that fails if the dashboard is missing from the wheel), issue and PR templates including a
  scraper-specific one, `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`.
- The dashboard now shows the **projected bill** and the most recent day's usage, generation
  and net energy, instead of four cards repeating the same row count. Data coverage moved to
  the header.
- Ruff lint and format configuration.

### Fixed
- **Data loss on a header change.** Two defects compounded: `parse_csv` had no equivalent of
  the hourly parser's "fail loudly" guard, so an SRP column rename produced a full export of
  `None`-valued readings and reported success — and `upsert_readings` assigned `excluded.*`
  unconditionally, so those blanks overwrote roughly a month of good history on the next
  scheduled fetch. `parse_csv` now raises when no value column is present at all (keyed on
  headers, so a genuinely blank day is still stored as blank), and both upserts use `COALESCE`
  so a restated value still wins but a `NULL` never erases a stored one.
- `GET /api/v1/intervals` reported `"unit": "kWh"` in its response envelope for every query,
  including `?series=demand`, which is **kW**. The envelope now carries the unit only when the
  query names a single series, and is `null` for a mixed result. Per-row units were already
  correct.
- Prometheus metrics carried the reported day as a `date` label, which mints a new time series
  every 24 hours and makes range queries impossible — the exact use case `/metrics` exists for.
  The date is now `srp_daily_reading_timestamp_seconds{series}`. `srp_readings_total` is renamed
  `srp_stored_readings` (`_total` is reserved for counters), and caption label values are now
  escaped rather than having their quotes and backslashes deleted.
- `srp-energy-monitor serve --reload` never started the server: uvicorn refuses to reload an app
  object and exited after the CLI had already printed the URLs. It now passes the app as an
  import string in factory mode, and says that `--database`/`--env-file` do not reach the
  reloaded child process.
- An unrecognised `SRP_LOG_LEVEL` crashed `serve` with a raw `KeyError` from uvicorn.
- `hourly --start`/`--end` turned a malformed date into a `ValueError` traceback, and `--days`
  was unbounded, so a large value raised `OverflowError` and a negative one silently produced a
  future start date and an empty result. Both are argparse usage errors now.
- The bundled `.env` fallback parser (used when python-dotenv is absent) stripped everything
  from the first `#`, silently truncating an unquoted password containing one — which then
  fails login in a way that looks exactly like a broken scraper. It now treats only ` #` as a
  comment, matching python-dotenv.
- The sdist shipped `tests/*.py` without `tests/fixtures/*.csv`, so a source-based install could
  not run its own test suite. Added `MANIFEST.in`.
- The `dev` extra did not include ruff, so the first-contribution workflow in CONTRIBUTING.md
  failed with `ruff: command not found`.
- Every read endpoint was `async def` while blocking on SQLite, so one slow query stalled the
  whole server — `/metrics` worst of all. They are plain `def` now, which FastAPI runs in a
  threadpool. `POST /api/v1/fetch` stays async.
- A failure part-way through `SRPClient.start()` left the Playwright driver and Chromium
  running for the life of the process; both `open_client` and `async with SRPClient(...)` now
  tear down whatever came up.
- The dashboard skipped any value equal to `0`, so a genuine zero reading was indistinguishable
  from missing data; showed only the single newest date, so a series lagging by a day lost its
  card entirely; kept asserting freshness after a failed refresh; and escaped nothing before
  writing SRP's scraped captions into the DOM.

### Changed
- `srp_energy` now ships a `py.typed` marker, so type checkers see its annotations.
- The test suite grew from 123 to 149 tests, including first coverage of the CLI parser.
- Chart x-axis labels could overprint each other at the right edge.

## [1.1.0] — 2026-08-27

### Added
- **Hourly interval data** (`srp-energy-monitor hourly`) over an explicit date range, chunked
  into 31-day windows because SRP renders every interval before it will export. Demand
  intervals are half-hourly and in kW; the three energy series are hourly kWh.
- **Bill projection**: SRP's projected amount for the cycle in progress, with the sentence
  stating what it projects and through what date, recorded as a time series.
- `GET /api/v1/intervals`, `GET /api/v1/bill-projection`, `GET /api/v1/bill-projection/history`.
- `srp_projected_bill_dollars` Prometheus metric.
- systemd user units for a daily fetch (`deploy/`).

### Fixed
- Hourly kW intervals were silently discarded. The demand export is headed `Off-peak kW` /
  `On-peak kW`, but the parser matched only the kWh spellings, so all 48 half-hours parsed to
  `None` and stored blank with no error. The parser now selects columns by series *and* raises
  when a row has a valid timestamp but no value in either expected column.

## [1.0.0] — 2026-08-26

### Added
- Playwright scraper for the SRP My Account portal: login, chart selection, CSV export for
  usage, generation, net energy and demand.
- The bill-cycle demand figure, scraped **with SRP's own caption** (`label`, `is_average`).
- SQLite storage with upsert on `(series, date)`, so re-fetching corrects history rather than
  duplicating it — SRP restates recent days as meter data settles.
- FastAPI REST API with OpenAPI docs, a Prometheus `/metrics` endpoint, and a dependency-free
  dashboard.
- CLI: `fetch`, `serve`, `show`, `export`, `import`.
- Test suite running on synthetic CSV fixtures with no network or credentials.

### Fixed
- Login was being skipped entirely: `_looks_authenticated` substring-matched `"myaccount"`
  against the whole URL, but the portal's *hostname* is `myaccount.srpnet.com`, so every page —
  including the login page — looked authenticated. It now examines only the path.
- The headline demand figure was reported as a peak. On an average-demand plan it is a cycle
  *mean*, and reading one as the other understated the real maximum by more than 2×. The
  caption is now stored alongside the value and nothing assumes which it is.

[Unreleased]: https://github.com/JeremyWhittaker/srp-energy-monitor/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/JeremyWhittaker/srp-energy-monitor/releases/tag/v1.1.0
[1.0.0]: https://github.com/JeremyWhittaker/srp-energy-monitor/releases/tag/v1.0.0
