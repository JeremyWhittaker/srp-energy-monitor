# Notes for whoever works on this next

Operational context that isn't obvious from the code or the README. Read this
before changing the scraper.

## What this is, in one line

Browser automation against Salt River Project's customer portal, because SRP
offers no API for residential accounts. Everything else in the repo exists to
make that fragile thing survivable.

## The load-bearing design decision

**Only `client.py` imports Playwright.** Parsing, storage, the API and the CLI
work entirely on saved CSVs. When SRP changes their markup — and they have,
twice, during the few days this was written — the scraper breaks while the API
keeps serving the history already collected.

Keep it that way. If you find yourself importing Playwright anywhere else,
that's the bug.

The corollary: every selector is a *cascade* of candidates, not one string, and
the ordering is "currently observed to work" first. Selectors recovered from
older versions of the portal are kept as fallbacks even when they look dead —
one of them (`input[name="UserName"]`, from a script that had been written off
as superseded) turned out to be current markup.

## Three bugs that were expensive to find

Each was invisible until the code ran against the live site. If you are
reviewing a change here, these are the shapes of mistake to look for.

1. **Login was being skipped entirely.** `_looks_authenticated` matched
   `"myaccount"` against the whole URL — but the portal's *hostname* is
   `myaccount.srpnet.com`, so every page including the login page looked
   authenticated. It now examines only the path. Lesson: never substring-match
   a URL when the host contains the token you're looking for.

2. **The headline demand figure was mislabelled a peak.** It is captioned
   "Current Average On-Peak Demand" on this account — a cycle *mean*, because
   the plan is an average-demand plan. Per SRP's own November 2025 tariffs,
   E-27 (Customer Generation) bills the single highest 30-minute on-peak kW --
   a true maximum -- while E-15 (Average Demand) and E-16 (Manage Demand
   5-10 p.m.) bill an *average* of daily maxima. E-28, E-13, E-14, E-21, E-23,
   E-24 and E-26 have no demand charge at all.

   So the caption is scraped and stored alongside the value (`label`,
   `is_average`); nothing in the codebase may assume which one it is. Reporting
   an average as a peak understated the real maximum by more than 2x (5.4 vs
   12.7 kW). Note SRP restructured the plan lineup in November 2025 and will
   again -- do not hard-code plan codes anywhere in the scraper.

3. **Demand intervals were silently discarded.** That export is headed
   `Off-peak kW` / `On-peak kW`; the parser matched only the kWh spellings, so
   all 48 half-hours parsed to `None` and stored blank with no error. The
   parser now selects columns by series *and* raises when a row has a valid
   timestamp but no value in either expected column. Prefer failing loudly over
   storing blanks — that is the whole reason this one hid.

## SRP facts worth knowing before you touch the scraper

- **There is a private JSON API** at `myaccount.srpnet.com/myaccountapi/api/...`
  that the `srpenergy` PyPI package calls with no browser at all. It is
  undocumented and unversioned. `jsonapi.py` is now a second backend over it;
  the portal scraper stays because the CSV export is a contract SRP publishes
  to its customers, and because the two paths fail differently -- markup
  changes break one, a field rename breaks the other.

  The old note here said the `demandList` question was "unverified and worth an
  afternoon". It is answered, and the answer was better than hoped:

  - `demandList` **is** populated on a demand-plan account -- 48 half-hourly kW
    rows a day, in the very response `srpenergy` already receives and discards
    on the line `hourly_usage_list = data["hourlyUsageList"]`.
  - `max(onPeakDemand)` over a billing cycle equals the billed demand charge
    exactly. Checked against SRP's own `highDemandInfoList`: 12.7 kW in the
    half hour ending 2026-08-18T20:00, both figures agreeing to the interval.
    SRP stamps a half hour with the time it ends, so that row is the
    19:30-20:00 interval and 20:00 is what `occurred_at` reports.
  - The bill projection is a JSON field too (`payments/billprojection`), and
    `isAverageDemandRate` is a boolean, so **neither of the two "rendered UI
    only" values actually requires a browser.** That was the load-bearing claim
    in the README's "why a browser" section and it did not survive contact with
    the API.
  - Daily history reaches back **more than 800 days** in one call, against ~31 from the
    portal export, and hourly reaches back at least to 2024-08.

- **Three JSON traps, all of which fail quietly rather than loudly:**

  1. **Ranges wider than ~31 days truncate silently.** `usage/hourlydetail`
     answers a two-year request with HTTP 200 and the most recent month. No
     error, no warning, no indication anything was dropped. `chunk_date_range`
     exists solely for this; never call `hourly_detail_window` directly across
     a wide range.
  2. **SRP's list names do not match its own CSV series names.** Established by
     fetching both paths for the same days and diffing every value:

     | CSV series | JSON daily list | JSON hourly list |
     |---|---|---|
     | `usage` | `dailyConsumptionList` | `hourlyConsumptionList` |
     | `generation` | `dailyGenerationList` | `hourlyGenerationList` |
     | `net` | `dailyReadList` | **`hourlyUsageList`** |
     | `demand` | `dailyDemandList` | `demandList` |

     The list called "usage" is *net* metered energy. Anything summing
     `hourlyUsageList` and calling it consumption -- Home Assistant's built-in
     `srp_energy` integration does exactly this -- reports a different quantity
     than the bill's kWh line. On this account the two differ by ~8 kWh/day.
  3. **Without the `x-xsrf-token` header every usage endpoint returns 403**,
     which looks identical to the Cloudflare block. Fetch
     `login/antiforgerytoken` after `login/authorize` and echo the cookie back.

- **A 403 is not an auth failure and not reliably a property of the host.**
  Within one hour on one network: `requests` refused, `curl` refused, stdlib
  `urllib` succeeded, and a second host accepted all three. Treat it as "not
  this time, from here" -- retry, change machine, or fall back to the browser.
  `AccessBlocked` exists so this never gets misreported as bad credentials.

- **The average-vs-maximum problem has a clean answer on this path.**
  `isAverageDemandRate` states which quantity the plan bills. On a peak plan
  the billed figure is `highDemandInfoList.onPeakHighDemand` directly. On an
  average plan SRP publishes no field for it, and it is reconstructed as the
  mean of `dailyDemandList` over the cycle's **weekdays** -- including weekend
  zeros halves the answer. Reconstruction gave 5.62 kW against the portal's
  printed 5.60; the residual is daily figures being published to one decimal.
  Prefer the scraped figure when you have both.
- **Cloudflare fronts the portal.** A default Playwright browser gets "Access
  Denied" with a Ray ID before SRP serves any markup. `USER_AGENT` and
  `_STEALTH_INIT_SCRIPT` in `client.py` exist solely to get past that. If
  fetches start failing wholesale, bump the Chrome version in `USER_AGENT`
  first — a stale one is the usual trigger.
- **The login page ships two copies of the form**, one visible and one hidden
  (`#username_desktop` vs `#txtUserName`). `_fill_first` requires visibility,
  because filling a hidden field succeeds silently and submits an empty form.
- **The export follows whichever chart is on screen.** `_select_chart` must run
  before any export, and it *refuses* rather than exporting an unconfirmed
  chart — a mislabelled download is worse than a failed one.
- **Demand is half-hourly and in kW**; usage, generation and net are hourly and
  in kWh. A day of demand is 48 rows, the others 24.
- **Only the hourly view has a date-range picker.** The daily exports give you
  whatever window the portal shows (~31 days). To reach further back, use
  `hourly --start/--end`. Long ranges are chunked into 31-day windows because
  SRP renders every interval before it will export, and a multi-month request
  times out.
- **Arizona has no daylight saving**, which is the only reason interval
  timestamps can be naive local time with no ambiguous hours.

## Credentials

Credentials live in `.env` (gitignored; keep it mode 600) and are read only by
`config.py`. The password is never written to the database, the logs or any API
response. The username *is* logged at INFO on login (`logging in to SRP as
<username>`), which is deliberate -- it is the fastest way to tell "wrong
account" from "broken selector" -- but it means a pasted log is not anonymous.

A login failure that looks like a scraper bug is usually a credentials bug:
check `.env` actually holds real values rather than placeholders before you go
hunting for a broken selector.

MFA is unsupported. If the account enables it, this tool stops working and
there is no fix short of a manual session hand-off.

## Scheduling

A systemd **user** timer (`deploy/srp-fetch.{service,timer}`, installed to
`~/.config/systemd/user/`) runs `fetch` then `hourly --days 2 --series …` daily
at 07:30 + jitter. `loginctl enable-linger` is required, or the timer only runs
while that user has an active login session.

`SuccessExitStatus=0 1` is deliberate: a scrape against someone else's website
will fail occasionally, and a timer that quietly retries tomorrow beats a unit
that flaps.

```bash
systemctl --user list-timers srp-fetch.timer
journalctl --user -u srp-fetch.service
```

## Verifying a change

The test suite runs on synthetic fixtures with no network — necessary but not
sufficient. **A green suite does not mean the scraper works.** Every one of the
three bugs above passed the tests.

To actually verify, run against the live site and compare to
<https://myaccount.srpnet.com/power/myaccount/usage> by eye:

```bash
.venv/bin/srp-energy-monitor fetch
.venv/bin/srp-energy-monitor show --days 7
```

When a selector breaks, `--show-browser` runs headed and the failure is usually
obvious in one glance.

## Database

SQLite at `data/srp.db`, currently **schema v4**. `Store._migrate` carries v1
forward; bump `SCHEMA_VERSION` and extend `_migrate` for anything further.

Everything upserts, so re-running a fetch corrects history rather than
duplicating it. SRP restates recent days as meter data settles, so that
property matters — do not "optimise" it into an insert.

The two scraped scalars (demand figure, bill projection) are append-only time
series, deliberately: watching them climb through the cycle is the point.

## Where this came from

A focused rewrite of the SRP half of an older personal project that mixed SRP
scraping with EG4 inverter and Enphase solar polling in a single ~3,000-line
Flask app. The SRP logic — login flow, selector cascades, CSV export sequence —
was carried over and hardened here; everything inverter-specific was left
behind.

That history is why the scraper is more defensive than its size suggests: most
of the selector cascades are scar tissue from the original.
