# SRP owner notes

Read [README.md](README.md) and the [Home Assistant guide](docs/HOME-ASSISTANT.md)
before changing collection or billing behavior. Follow the repository's AGENTS.md
and Jeremy's Foreman, privacy, privilege and shipping rules. These notes describe
source behavior, not a fresh health check of an installed household system.

## Scope and separation

This repository owns the standalone SRP collector, SQLite store, read API and
`custom_components/srp_energy_monitor`. The HA integration talks to SRP directly;
it does not require the standalone database or web service.

There are two collection paths:

- `client.py` uses Playwright for SRP's portal and CSV exports.
- `jsonapi.py` uses the portal's **undocumented private JSON API**. This is not
  an official public API contract, and field names or access rules can change.
  The HA component has its own async client in `srp_api.py`.

Only `client.py` imports Playwright. Keep parsing, persistence and the HA component
browser-free. The duplicated package/component parsing rules are intentional:
change both when applicable and preserve `tests/test_component_parity.py`.

For browser maintenance, retain observed selector fallbacks and require visible
login fields. Authentication checks must inspect the URL path, not match the
portal hostname. Confirm the selected chart before exporting; refuse an
unconfirmed chart instead of storing a mislabeled download.

## Quantities that must remain separate

- **E-15:** mean of eligible daily maximum 30-minute on-peak demand values.
- **E-16:** mean of daily maximum 60-minute on-peak demand values.
- **E-27:** the cycle's maximum 30-minute on-peak demand.
- `CyclePeak` / `highDemandInfoList.onPeakHighDemand` is a reported **kW maximum**.
  On an average-demand plan it is not the billed mean or a dollar charge.
- The current JSON average is reconstructed from daily maxima. A portal caption,
  derived mean, cycle maximum and final billed demand are not interchangeable.
- `payments/billprojection` provides a projected total bill. Actual invoice
  line items and historical demand-charge dollars are not currently collected.

`isAverageDemandRate` distinguishes average versus maximum plan types; it does
not identify E-15 rather than E-16 or prove historical enrollment for each cycle.
The E-15 audit therefore requires the explicit `demand_audit_plan: E15` option.
Configure it through the supported options flow; Disabled or an unsupported plan
keeps the audit unsupported. Saving preserves other options and credentials and
reloads the existing entry. Never edit live `.storage` or substitute a generic
config-entry update for the options flow.

Average-demand calculations exclude weekends and the six SRP holidays: observed
New Year's Day, Memorial Day, observed Independence Day, Labor Day, Thanksgiving
and observed Christmas. Include December 31 observance of next year's New Year.
Ordinary zero-demand weekdays remain in the denominator; MLK Day and Black Friday
are not SRP holidays. The rule follows tariff wording; a holiday-containing bill
remains a separate financial reconciliation gate. See the
[tariff evidence and limits](README.md#which-plans-charge-for-demand).

## Source mappings and interval evidence

| Series | JSON daily list | JSON interval list |
| --- | --- | --- |
| `usage` | `dailyConsumptionList` | `hourlyConsumptionList` |
| `generation` | `dailyGenerationList` | `hourlyGenerationList` |
| `net` | `dailyReadList` | `hourlyUsageList` |
| `demand` | `dailyDemandList` | `demandList` |

Do not call `hourlyUsageList` consumption. SRP generation is a portal series with
unverified whole-roof coverage; do not add it to inverter totals as another array.
The daily generation sensor deliberately exposes **on-peak only**. Its off-peak
field can be nonzero; do not add it to that sensor or rewrite hourly history to
make unlike quantities agree.

Demand is kW; energy is kWh. The observed E-15 `demandList` contract has 30-minute,
**start-labeled** slots. The cycle-maximum occurrence is a different field; its
timestamp convention must not shift raw demand slots. Preserve period width,
timezone and evidence of the timestamp role. Do not apply an unverified universal
end-label rule, or silently treat E-16 as half-hourly.

The demand audit reuses the existing fetch and checks at most seven completed
Arizona days. Require every eligible peak slot, finite nonnegative demand,
compatible units, valid timestamps and unambiguous duplicates. Missing is not
zero. Preserve genuine zeros, the earliest exact winning half-hour and tie count.
States are `matches`, `mismatch`, `incomplete` or `unsupported`; related-source
agreement and a stable content hash do not verify an invoice. Derived historical
cycle means retain explicit coverage and plan-history limits.

## Retention, revisions and database

Chunk JSON hourly requests into at most 31-day windows: a larger HTTP 200 response
can silently contain only the latest month. Daily/interval availability varies
by account and date; observed multi-year history is not an all-time guarantee.
HA initially requests 760 days, then replays seven completed Phoenix days and any
collection gap. Older missing/corrupt history needs a backed-up targeted repair.

Preserve exact timestamps/statistic IDs and a known preceding cumulative total
when revising energy. Replace revised hourly states and rebuild the affected
tail; repeating an unchanged import must not add the last hour again. Refuse
ambiguous duplicates, missing baselines, invalid values and unsettled/future
rows. Never assume utility revisions only rise or that closed cycles cannot
change. A checksum detects changed evidence; it does not establish its meaning.

Standalone SQLite uses **schema 5**, with `daily_reading`, `interval_reading`,
`demand_observation`, `bill_projection`, `cycle_peak` and `meta`. Reading/interval
imports upsert; cycle maxima are keyed by cycle; demand/projection observations
are keyed by observation time. Preserve migrations in `Store._migrate`. There is
no invoice/demand-dollar ledger. The HA integration stores its own sensors and
Recorder statistics, not this SQLite database.

## Authentication and privacy

Standalone credentials are configured through the existing secret settings;
keep `.env` gitignored and mode 0600. HA credentials remain in its config entry.
Never commit or print passwords, tokens, cookies, account identifiers, databases,
raw account payloads, statements or backup archives. Existing INFO logs may
include usernames: inspect and redact them before sharing.

The HA client uses a private session/cookie jar. Preserve its bounded session
repair and anti-forgery token handling. Only an explicit `invalid_grant` response
on `login/authorize` establishes rejected credentials; a data-endpoint 401 is a
session problem, and an unexplained error or Cloudflare 403 is not proof of a bad
password. Do not disable CSRF, TLS/SSH verification, protection mode or access
checks. Do not recommend stealth changes, rotating clients/hosts or repeated
logins to get around a challenge. Stop and report CAPTCHA/MFA/Cloudflare access
blocks; use only an authorized supported authentication path.

Documentation or offline review does not authorize live logins, account changes,
timer/service changes, imports, reloads or device control. The shipped systemd
user timer template runs daily at 07:30 with jitter; its `SuccessExitStatus=0 1`
allows a failed scrape without flapping. Check actual installation separately.
Do not enable linger or change host privileges as a documentation cleanup.

## Validation

Use the current tests rather than old household snapshots:

- `test_component_parity.py`: equivalent package/component parsing.
- `test_jsonapi.py`, `test_storage.py`, `test_api.py`: mapping, persistence and API quantities.
- `test_ha_session_recovery.py`: auth/session and reauthentication guards.
- `test_ha_statistics_restatements.py`: exact-hour revisions and cumulative tails.
- `test_demand_audit.py`, `test_options_flow.py`: coverage, hashes, plan binding and options/reload.

Run relevant tests and Ruff; full suites go through `jeremy-heavy`. Inspect
`git diff --check` and task-owned changes before shipping. A green offline suite
does not prove live collection, a deployed options form, actual enrollment,
physical measurements or invoice reconciliation. When live verification is
authorized, bound it, preserve private evidence and compare the same quantities.
