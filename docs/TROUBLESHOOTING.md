# Troubleshooting

Almost everything that goes wrong with this project is the scraper, because
the scraper is automation against someone else's website. Start here.

## First move: watch it happen

```bash
srp-energy-monitor --log-level DEBUG fetch --show-browser
```

This runs headed, so you can see exactly which page the scraper lands on and
where it stalls. On a headless server, forward X11 or run it on a desktop with
the same credentials — the failure is almost always visible in one glance.

---

## `SRP login failed`

```
LoginError: SRP login failed. Check SRP_USERNAME/SRP_PASSWORD, and note that
an account with MFA enabled cannot be automated. Landed on: https://...
```

In rough order of likelihood:

1. **Credentials are wrong or not loaded.** Confirm what the process actually
   sees:
   ```bash
   srp-energy-monitor show    # then check /health for credentials_configured
   ```
   A `.env` in a different directory than the one you run from will not be
   picked up — pass `--env-file /path/to/.env` explicitly.
2. **MFA is enabled on the account.** This is unsupported and unfixable from
   here. Disable MFA, or don't use this tool.
3. **SRP renamed the login fields.** Run headed, inspect the username input,
   and add its selector to `USERNAME_SELECTORS` / `PASSWORD_SELECTORS` in
   `srp_energy/client.py`. They are ordered tuples — put the working one first.
4. **A consent banner or interstitial is covering the form.** SRP has added
   these before. Dismiss it in `login()` before filling fields.

## `could not find a peak demand value`

```
ScrapeError: could not find a peak demand value on the SRP usage or dashboard page.
```

The scraper already tries eight selectors and then falls back to scanning for
any leaf element containing a `kW` reading. If all of that fails, SRP changed
the usage page materially.

Fix: run headed, find the element holding the number, and prepend its selector
to the `selectors` array in `_PEAK_DEMAND_JS` in `srp_energy/client.py`.

Note this error does **not** stop the CSV downloads — a fetch will still store
daily readings and report the failure in `errors`.

## `could not find the {series} chart button`

The scraper refuses to export when it cannot confirm which chart is selected,
because exporting the wrong chart under the right filename silently corrupts
your data. This is deliberate.

Fix: update the `candidates` tuple in `_select_chart()`. The buttons have
historically been `button.chart-type-btn` with the visible label as text
("Usage", "Generation", "Net energy", "Demand").

## `no export button found`

SRP's "Export to Excel" button moved or was renamed. Update `EXPORT_SELECTORS`
in `srp_energy/client.py`.

## Downloads time out

```
playwright._impl._errors.TimeoutError: Timeout 60000ms exceeded
```

SRP's export generation is genuinely slow, especially in summer. Raise it:

```bash
SRP_DOWNLOAD_TIMEOUT_MS=120000 srp-energy-monitor fetch
```

## `Playwright is not installed`

```bash
pip install -e '.[scrape]'
playwright install chromium
```

On a bare server you may also need the system libraries Chromium wants:

```bash
playwright install-deps chromium     # needs sudo
```

## Browser fails to launch in Docker or as root

The client already passes `--no-sandbox --disable-setuid-sandbox`. If it still
fails, the container is missing Chromium's shared libraries — use
`mcr.microsoft.com/playwright/python` as the base image, which ships them.

---

## Data problems

### A parse succeeded but the numbers look wrong

Check the raw CSV. They are kept in `data/downloads/` by default:

```bash
ls -t data/downloads/ | head
head -5 data/downloads/srp_demand_*.csv | tail -4
```

If the header does not match what `parser.py` expects, SRP changed the export
format. The parser normalises case and whitespace and accepts several aliases
per column, so add the new spelling to the relevant `_*_KEYS` tuple.

### Demand rows have `off_peak: null`

Correct and expected. The demand export has one value column (`On-peak kW`),
which is stored in `on_peak`. `total` is `null` for demand because a peak is a
maximum, not a sum.

### Net energy values are negative

Also correct. Net energy is negative when your solar exported more than you
drew during that period.

### Re-running a fetch didn't add rows

Also correct. Readings upsert on `(series, usage_date)`. Same day, same series,
one row — the values update in place. If SRP restated a day, you'll see it
change; if nothing changed, nothing changes.

### `no usable date column found`

The file isn't an SRP export, or the download returned an error page instead of
a CSV. Open it and look.

---

## Server problems

### `uvicorn is not installed`

```bash
pip install -e '.[api]'
```

### The dashboard loads but every panel says "Could not load data"

The API is up but the database is empty. Run a fetch, or import old CSVs with
`srp-energy-monitor import <dir>`.

### `409 a fetch is already running`

Working as intended — concurrent scrapes would fight over the same SRP session.
Wait for the running one to finish.

### `database is locked`

Two processes writing at once. The store uses WAL, which allows concurrent
reads during a write, but not concurrent writers. Don't run `fetch` from cron
at the same moment you trigger one from the dashboard.
