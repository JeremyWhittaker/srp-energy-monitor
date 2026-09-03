# Changelog

Notable changes to this project. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.1] — 2026-09-03

This is the release that fixes the Home Assistant outage described under *Fixed*. If you are
checking whether you have it, the integration's `manifest.json` reads `"version": "1.2.1"`; the
broken build is `1.2.0`.

### Added
- **A second, browser-free backend** (`srp_energy.jsonapi`, `srp-energy-monitor backfill`) over
  SRP's private JSON API. Stdlib only — the base install still declares no dependencies, and
  Chromium is now needed for `fetch` alone. It reaches **more than 800 days** of daily history in
  one call against ~31 from the portal export, and hourly data back to at least 2024-08. (819
  days on 2026-09-03 for the account this was built against. The reach is whatever SRP still
  retains, so it grows by a day each day — hence the round number everywhere else.)
- **Billed cycle peaks** (`cycle_peak` table, schema v5, `CyclePeak` model). SRP's
  `highDemandInfoList` gives the single highest on-peak half hour of each billing cycle with the
  timestamp it happened — the figure the demand charge is actually computed from, which nothing
  in this project previously stored. Verified against SRP's own number: 12.7 kW in the half
  hour ending 2026-08-18 20:00. SRP labels a half hour by the time it ends, so that is the
  19:30–20:00 interval, and 20:00 is what the sensor's `occurred_at` attribute reports.
- `SRP_ACCOUNT_ID` setting, required by the JSON backend only. The browser backend reads the
  account from the signed-in session and ignores it.
- **A Home Assistant custom integration** (`custom_components/srp_energy_monitor/`). Six entities
  including the two demand figures that must never be confused -- the cycle figure the portal
  prints, and the billed peak the charge is actually computed from -- plus back-filled long-term
  statistics for hourly energy and half-hourly demand. Declares no requirements: it uses the
  standard library and the aiohttp Home Assistant already ships, so nothing can fail to install
  on HAOS.
- `GET /api/v1/cycle-peaks`, and `latest_cycle_peak` on `/api/v1/summary`.
- `docs/ha-dashboard.yaml` — the three-view Home Assistant dashboard (Bill Cycle, Daily Shape,
  History) built against the integration, ready to paste into a raw configuration editor.
- The dashboard is redesigned around the demand figure rather than treating it as one of five
  equal cards: billing-cycle progress, the billed peak shown beside the cycle average with the
  distinction spelled out, a threshold line and highlighted bar marking the peak on the demand
  chart, and a light/dark toggle. Still one file, still no external resources.
- **`deploy/srp-web.service`** — a systemd user unit for the dashboard and REST API. The repo
  shipped units for the daily fetch but nothing for the website, so `srp-energy-monitor serve`
  only ever ran by hand, in whatever terminal you last typed it in. The web server now starts at
  boot alongside the timer and restarts if it dies. Installation, including the
  `loginctl enable-linger` step that keeps user units running after you log out, is written up
  in `deploy/README.md`, which also explains what each unit does and why the API must stay on
  `127.0.0.1` — it has no authentication.
- **`docs/HOME-ASSISTANT.md`** — the Home Assistant integration's own document: what each of the
  six entities means, why the two demand figures differ and which one the bill uses, installing
  it without HACS, the long-term statistics it back-fills, and what to do when it stops
  updating.

### Fixed
- **The Home Assistant integration now recovers from an expired session instead of going dark.**
  SRP answers `401` both to a wrong password and to a session cookie that has merely aged out,
  and the client read every `401`, on every endpoint, as a credential failure. Home Assistant,
  once told its credentials are bad, stops polling permanently and asks for reauthentication —
  the right behaviour for a wrong password, and the wrong one here. A single `401` was
  therefore enough to freeze the integration: after a restart on 2026-09-02 at 17:40, one `401`
  from `usage/dailyusagedetail` on the first refresh left all six sensors at `unavailable` for
  roughly 18 hours with no retries, until the config entry was reloaded by hand. Only a failure
  at `login/authorize` — where a genuinely wrong password produces `400 invalid_grant` — now
  counts as a credential problem. Everything else is retryable: a `401` anywhere else is read
  as a stale session and the client signs back in and replays the request, and any other
  failure simply waits for the next poll. Either way the sensors recover without anyone
  noticing. Both failure
  bodies, side by side, are in `docs/TROUBLESHOOTING.md`; the table of log lines to look for is
  in `docs/HOME-ASSISTANT.md` under *Checking the logs*.
- **The integration no longer shares a cookie jar with every other integration.** Both the
  coordinator and the config flow built their HTTP client on `async_get_clientsession`, Home
  Assistant's *shared* aiohttp session, so SRP's `.AspNet.MyAccount` session cookie lived in a jar
  the rest of Home Assistant also writes to and clears — and the client cached its anti-forgery
  token forever, so once that cookie was gone it never logged in again and every subsequent call
  failed. Both now call `async_create_clientsession`, which hands back a session and cookie jar
  of their own, so the only thing that can evict the login cookie is SRP. The client also drops
  its cached token when a session dies, and re-authenticates on demand rather than assuming a
  session it had at startup is still there hours later.
- **A reauthentication prompt now leads somewhere.** The reauth step previously restarted the
  whole add-integration form, which then aborted as already-configured, so the only way out of an
  auth failure was to delete the entry and set it up again. There is now a proper
  `reauth_confirm` step: it asks for a username and password only, validates them against SRP,
  updates the existing entry in place and reloads it, so the coordinator that stopped polling is
  the one that starts again.
- **The daily generation entity is named for what it actually holds.** SRP's
  `dailyGenerationList` populates only its on-peak column; the off-peak one is always zero, so
  the figure reads 0 on weekends while the array is plainly still producing. Summing the two
  columns and calling the result "daily generation" understated 2026-08-27 as 21 kWh against
  84.5 kWh summed from `hourlyGenerationList`. Renamed to **Daily on-peak generation**; total
  production is the `energy_generation` statistic, which was already correct. Consumption and
  net needed no change -- their daily and hourly lists agree to within a rounding step.
- **Interval demand no longer carries its off-peak leg.** The daily parser already dropped it —
  SRP publishes an off-peak demand figure but bills only the on-peak one — while the interval
  parser kept both, and the Home Assistant coordinator then stored `max(off_peak, on_peak)` as
  the demand statistic. On a solar account off-peak demand runs roughly double the on-peak
  figure, so the chart reported an unbilled **25.3 kW** for a cycle whose billed peak was
  **12.7 kW**. Both parsers now drop the leg, which makes the mistake unrepresentable rather
  than merely unlikely, and a parity test holds the two paths together.
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
- **The README never made clear that this repo is three things.** It read as documentation for
  the collector, with the web dashboard and the Home Assistant integration mentioned in passing,
  so people ran `serve` by hand without realising a service could run it, and assumed the
  integration read `data/srp.db` — it does not; it polls SRP directly from inside Home
  Assistant and shares nothing with the collector but a login. Rewritten around the three parts,
  what each is for, which ones you actually need, and how they do and do not relate.
- The README claimed SRP has "no API". It has no *documented* API, but there is a private JSON
  endpoint behind the portal that other clients call without a browser. Corrected, with an
  explicit section on why this project drives the portal anyway.
- **The same overstatement came back during the rewrite, with a second one attached** — "no API,
  no export, no alert" in the opening paragraph, and "SRP publishes no API" under *Limitations*.
  Both are now "no *documented* API", and "no export" is gone: SRP does offer an export, and this
  project's collector works by clicking its **Export to Excel** button. What is actually true is
  that the export is a manual, per-chart download, after the fact — which is what the opening
  paragraph now says.
- **The README showed API output before it said what the project was.** A `curl` command and a
  JSON blob sat in the second section, ahead of *What is in this project*. Moved to the end of
  that section, where the reader already knows which of the three parts is answering.
- **"Skip all of the above — you do not need this checkout running anywhere"** was misleading for
  the Home Assistant path: the integration is a directory *inside* this repository that you copy
  into Home Assistant, so you do need the files. Reworded to what is actually true — you need the
  files, but nothing here has to be installed, configured or left running.
- **The backfill's reach was quoted three different ways** — "812 days" in this file, "over 800
  days" and "800+ days" in the README. Standardised on **more than 800 days**, since the true
  figure is whatever SRP still retains and therefore grows daily.
- The README named E-27 and **E-28** as the peak-demand plans. Per SRP's November 2025 tariffs
  E-28 has no demand charge at all; E-27 bills a true 30-minute maximum, while E-15 and E-16
  bill averages of daily maxima. Replaced with a plan table, and noted that E-16 opened demand
  charges to all residential customers rather than solar accounts only.
- Documentation accuracy pass across `docs/`, `.env.example`, `requirements.txt` and `deploy/`:
  credentials are no longer described as "never logged" (the username is), `/health` is no longer
  described as returning nothing but a boolean, the `import` example no longer omits its required
  argument, and the inline systemd examples now match the units actually shipped.
- **`docs/TROUBLESHOOTING.md` opened by telling every reader that their problem is the scraper**,
  and offered `fetch --show-browser` as the first move — useless advice for someone whose Home
  Assistant sensors have gone `unavailable`, and the same "this repo reads as three projects"
  problem the README had. It now opens with a table of contents that splits the document by which
  part you are running, and sends you straight to the right one.
- **`curl` was documented as a reachability test in two places that disagreed.** One section said
  a `403` from `curl` is normal and to use it to confirm the site is up; a later one said `curl`
  tells you nothing at all. The second is right, and this genuinely misled the 2026-09-03
  diagnosis. Merged into a single project-wide section, *Is SRP reachable?*, cross-referenced from
  both the Cloudflare and the Home Assistant sections: measured on the same host in the same
  minute, `myaccount.srpnet.com` answered `403` to `curl` (with and without a browser
  `User-Agent`) while all six Home Assistant sensors were updating normally.
- **The Home Assistant symptom was described wrongly.** The troubleshooting page said the
  dashboard's numbers would look "stale but not obviously wrong". They do not: an `unavailable`
  entity renders as the word **Unavailable** on tile and entity cards. Only statistics-graph
  cards keep drawing, because they read recorded history rather than current state — so the
  actual signature is empty tiles beside charts that stop dead at a fixed time. That is how you
  recognise the fault, so it now says so.
- **`deploy/README.md` claimed every unit hardcodes a checkout path and carries a comment saying
  so.** `srp-fetch.timer` has neither. Corrected to name the two `.service` files, and the
  install recipe now states the prerequisite it never mentioned: a `.venv` in the checkout with
  `.[api]` (web) or `.[scrape]` plus `playwright install chromium` (fetch) installed. Without
  uvicorn, `serve` exits 1 and `Restart=on-failure` retries it indefinitely.
- **`deploy/README.md` claimed "no root anywhere" and then said to run `loginctl enable-linger`.**
  Enabling linger asks logind — a root daemon — to write a flag file under
  `/var/lib/systemd/linger/`. Doing it for your own account is a more permissive polkit action
  than doing it for someone else's and usually needs no password, but over SSH with no polkit
  agent it can still fail with `Interactive authentication required` and need `sudo`. Reworded to
  "nothing in them runs as root", which is the true and useful claim.
- **`deploy/srp-web.service` and `deploy/README.md` were unreachable.** Nothing linked to them:
  `README.md` still copied only the fetch unit and its timer, and still described `deploy/` as
  units "for the daily collection". Both are now part of the README's install steps, the API
  section, and the file map.

### Fixed (systemd units)
- **`deploy/srp-web.service`'s restart policy did not do what its comment claimed.** The comment
  said a bad `.env` or a taken port "fails instantly, so space the retries out"; in fact
  `RestartSec=10` sits under the default limit of five starts per ten seconds, so the limit could
  never be reached and a permanently broken install restarted every ten seconds forever without
  ever latching `failed`. Added `StartLimitIntervalSec=300` / `StartLimitBurst=5`, so five
  failures in five minutes stop the loop and leave a unit whose status says so. Verified with
  `systemd-analyze --user verify`.
- **The same comment was wrong about what fails.** `_cmd_serve` never calls
  `require_credentials()` and `_load_dotenv` returns silently when the file is absent, so a
  missing or credential-less `.env` starts the dashboard normally on `127.0.0.1:8000` — verified
  live: `/health` answers `200` with `"credentials_configured": false`. What actually stops a
  start is a missing venv, a non-numeric `SRP_PORT`, or a port in use. The comment now says that.
- **`deploy/srp-web.service` carried `After=`/`Wants=network-online.target`**, inherited from
  `srp-fetch.service`. That target does not exist in a systemd *user* manager — `systemctl --user
  status network-online.target` answers "could not be found" — so it was inert. Dropped from the
  web unit, which binds `127.0.0.1` and reads a local file and has nothing to wait for.
  `srp-fetch.service` keeps its copy, and `deploy/README.md` now explains that it is inert there
  too and is retained only because it stays correct if the unit is moved to the system manager.

### Added (repository and dashboard)
- Public-repository scaffolding: CI on Python 3.10–3.13 (`pytest`, `ruff`, a package build
  that fails if the dashboard is missing from the wheel), issue and PR templates including a
  scraper-specific one, `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`.
- The dashboard now shows the **projected bill** and the most recent day's usage, generation
  and net energy, instead of four cards repeating the same row count. Data coverage moved to
  the header.
- Ruff lint and format configuration.

### Fixed (collector, API and dashboard)
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
- The test suite grew from 123 to 238 tests, including first coverage of the CLI parser.
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

[1.2.1]: https://github.com/JeremyWhittaker/srp-energy-monitor/compare/v1.1.0...v1.2.1
[1.1.0]: https://github.com/JeremyWhittaker/srp-energy-monitor/releases/tag/v1.1.0
[1.0.0]: https://github.com/JeremyWhittaker/srp-energy-monitor/releases/tag/v1.0.0
