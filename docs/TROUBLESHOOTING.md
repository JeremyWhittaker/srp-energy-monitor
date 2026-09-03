# Troubleshooting

This repository is one product with **three parts**, and they break in
completely different ways. Work out which part you are actually looking at
before you start debugging — most wasted time here is spent fixing the wrong
one.

| What is misbehaving | That is | Go to |
|---|---|---|
| `srp-energy-monitor fetch` / `hourly` / `backfill` — the thing that signs in and stores data | **the collector** | [Collector problems](#collector-problems) |
| `srp-energy-monitor serve` — the dashboard, the JSON API, `/metrics` | **the website and API** | [Server problems](#server-problems) |
| Sensors in Home Assistant reading `unavailable`, or history not filling in | **the Home Assistant integration** | [Home Assistant problems](#home-assistant-problems) |

The three share nothing but an SRP login. In particular, **the Home Assistant
integration does not read `data/srp.db` and does not call this project's REST
API** — it talks to SRP by itself, from inside Home Assistant. Restarting the
collector or the web server cannot fix it, and vice versa.

## Contents

- [Is SRP reachable? (read before testing with `curl`)](#is-srp-reachable-read-before-testing-with-curl)
- [Collector problems](#collector-problems) — the scraper and the JSON backfill
- [Data problems](#data-problems) — the numbers parsed but look wrong
- [Server problems](#server-problems) — `serve`, the dashboard, the API
- [Home Assistant problems](#home-assistant-problems) — the custom integration

---

## Is SRP reachable? (read before testing with `curl`)

This applies to all three parts, and it is first because getting it wrong sent
the 2026-09-03 investigation down a blind alley for an hour.

**A `403` from `curl` proves nothing.** SRP sits behind Cloudflare, which
decides what to serve based on far more than the URL — and a plain HTTP client
is exactly what it declines.

Measured on this project's own machine, at a moment when all six Home Assistant
sensors were updating normally and the collector was working:

```console
$ curl -s -o /dev/null -w '%{http_code}\n' https://myaccount.srpnet.com/power
403
$ curl -s -o /dev/null -w '%{http_code}\n' \
    -H 'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 …' \
    https://myaccount.srpnet.com/power
403
```

Adding a browser `User-Agent` does not help; Cloudflare fingerprints the TLS
handshake and header ordering too, not just the header you can set. The verdict
also **varies by client and by moment**: on the same host and network, the same
request has come back `403` from `curl` and `200` from Python's standard
library minutes apart, and later `403` from both while the integration kept
polling successfully.

So a failing `curl`:

- does **not** mean SRP is down;
- does **not** mean your network or IP is blocked;
- does **not** mean your credentials are wrong;
- does **not** mean the collector or the integration will fail.

**What to test instead.** Use the client the code actually uses, or read the
code's own log:

| Part | How to actually test it |
|---|---|
| Collector | `srp-energy-monitor --log-level DEBUG fetch --show-browser` |
| Website / API | `curl -s http://127.0.0.1:8000/health` (that is *your* server, not SRP — a `403` there means something else entirely) |
| Home Assistant | Turn on debug logging and read the integration's own lines — see [Checking the logs](HOME-ASSISTANT.md#checking-the-logs) |

The one thing `curl` is good for is proving that DNS resolves and something on
the far end answered at all. A `403` **is** an answer, so it tells you the host
is up. That is the whole of its diagnostic value.

---

## Collector problems

Automation against someone else's website is the most fragile part of this
project, and most collector failures are SRP changing their markup.

### First move: watch it happen

```bash
srp-energy-monitor --log-level DEBUG fetch --show-browser
```

This runs headed, so you can see exactly which page the scraper lands on and
where it stalls. On a headless server, forward X11 or run it on a desktop with
the same credentials — the failure is almost always visible in one glance.

### "Access Denied" with a Ray ID

```
Title: Access Denied
You are not authorized to access this webpage.
Ray ID: a3166fa2ad6ae1fa
```

Cloudflare blocked the browser before SRP ever saw the request — note the Ray
ID, which is Cloudflare's, not SRP's. Nothing in your account or credentials is
involved.

The client already counters this with a realistic desktop fingerprint set in
`start()` and the `USER_AGENT` / `_STEALTH_INIT_SCRIPT` constants in
`srp_energy/client.py`: a current Chrome user agent, a 1440×900 viewport,
`America/Phoenix`, `--disable-blink-features=AutomationControlled`, and an init
script that removes the `navigator.webdriver` giveaway. If Cloudflare tightens
its rules, that is the code to update — start by bumping `USER_AGENT` to a
current Chrome version, since a stale one is the usual first trigger.

Do **not** reach for `curl` to check whether this is what you are hitting; it
answers `403` whether or not anything is wrong. See
[Is SRP reachable?](#is-srp-reachable-read-before-testing-with-curl) above.

### `SRP login failed`

```
LoginError: SRP login failed. Check SRP_USERNAME/SRP_PASSWORD, and note that
an account with MFA enabled cannot be automated -- this scraper has no MFA
support. Landed on: https://...
```

In rough order of likelihood:

1. **Credentials are wrong or not loaded.** `srp-energy-monitor show` prints
   nothing about credentials (and exits 1 on an empty database), so confirm
   what the process actually sees by asking the API instead:
   ```bash
   srp-energy-monitor serve &
   curl -s http://127.0.0.1:8000/health | grep credentials_configured
   ```
   A `.env` in a different directory than the one you run from will not be
   picked up — pass `--env-file /path/to/.env` explicitly. If the password
   contains a `#` or spaces, quote it (`SRP_PASSWORD="pa#ss word"`) --
   otherwise it can be truncated or split before it ever reaches SRP.
2. **MFA is enabled on the account.** This is unsupported and unfixable from
   here. Disable MFA, or don't use this tool.
3. **SRP renamed the login fields.** Run headed, inspect the username input,
   and add its selector to `USERNAME_SELECTORS` / `PASSWORD_SELECTORS` in
   `srp_energy/client.py`. They are ordered tuples — put the working one first.

   Be aware the login page ships **two copies of the form**: a visible
   lowercase `username` / `password` pair, and a hidden capitalised
   `UserName` / `Password` pair (`#txtUserName` / `#txtPassword`). `_fill_first`
   skips anything not visible, because filling a hidden field succeeds silently
   and then submits an empty form. If you add a selector, keep that check.
4. **A consent banner or interstitial is covering the form.** SRP has added
   these before. Dismiss it in `login()` before filling fields.

### `could not find a demand value`

```
ScrapeError: could not find a demand value on the SRP usage or dashboard page.
```

The scraper already tries eight selectors and then falls back to scanning for
any leaf element containing a `kW` reading. If all of that fails, SRP changed
the usage page materially.

Fix: run headed, find the element holding the number, and prepend its selector
to the `selectors` array in `_DEMAND_JS` in `srp_energy/client.py`.

Note this error does **not** stop the CSV downloads — a fetch will still store
daily readings and report the failure in `errors`.

### `could not find the {series} chart button`

The scraper refuses to export when it cannot confirm which chart is selected,
because exporting the wrong chart under the right filename silently corrupts
your data. This is deliberate.

Fix: update the `candidates` tuple in `_select_chart()`. The buttons have
historically been `button.chart-type-btn` with the visible label as text
("Usage", "Generation", "Net energy", "Demand").

### `no export button found`

SRP's "Export to Excel" button moved or was renamed. Update `EXPORT_SELECTORS`
in `srp_energy/client.py`.

### Downloads time out

```
playwright._impl._errors.TimeoutError: Timeout 60000ms exceeded
```

SRP's export generation is genuinely slow, especially in summer. Raise it:

```bash
SRP_DOWNLOAD_TIMEOUT_MS=120000 srp-energy-monitor fetch
```

### `Playwright is not installed`

```bash
pip install -e '.[scrape]'
playwright install chromium
```

On a bare server you may also need the system libraries Chromium wants:

```bash
playwright install-deps chromium     # needs sudo
```

### Browser fails to launch in Docker or as root

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

### The demand figure has `label: null`

The value was found but SRP's caption beside it was not. Because that caption is
the only thing distinguishing a cycle *average* from a cycle *peak*, the value is
stored unlabelled and a warning is logged rather than guessing. Fix by updating
the `captionFor` regex in `_DEMAND_JS` in `srp_energy/client.py` — it currently
looks for text matching `Current ... Demand` within six ancestors of the value.

### Daily demand rows have `off_peak: null`

Correct and expected for the **daily** demand export -- it has one value
column (`On-peak kW`), which is stored in `on_peak`. `total` is `null` for
demand because a peak is a maximum, not a sum.

The **hourly/interval** demand export is different: it has both `Off-peak kW`
and `On-peak kW` columns, so `interval_reading` rows for demand can carry
both `off_peak` and `on_peak`.

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

---

## Home Assistant problems

These are about the custom integration in `custom_components/srp_energy_monitor/`,
which talks to SRP's JSON API from inside Home Assistant. It does not read
`data/srp.db` and does not call this project's REST API, so nothing here is
fixed by restarting the scraper or the web server. Full setup notes are in
`docs/HOME-ASSISTANT.md`.

### All six sensors say `unavailable` and never come back

Symptoms, usually all at once:

- every `sensor.srp_account_*` entity reads `unavailable`, and has for hours
- templates and automations referencing them log "entity not found"
- **Settings → Devices & Services** may show the integration asking you to
  *reconfigure* or *reauthenticate*

On a dashboard it looks distinctive, and the two halves look nothing alike:

- **Tile and entity cards go blank** — an `unavailable` entity renders the word
  **Unavailable** where the number should be. They do not keep showing the last
  reading, so there is no risk of quietly acting on a stale figure.
- **Statistics-graph cards keep drawing** — they read recorded history rather
  than current state, so the bars and lines are all still there and simply
  **stop dead at a fixed time**, with a flat gap from that moment to now.

That combination is the signature: live tiles saying *Unavailable* next to
charts that look fine until a cliff edge at the hour it broke. Note the time on
that cliff — it is when the failing refresh happened, and it is the timestamp to
search the log for.

The giveaway is that it never retries. A network blip recovers on the next
poll; this does not recover at all, at any hour, until a human touches it.

**Why it happens.** When an integration reports an *authentication* failure,
Home Assistant stops polling it permanently and starts a reauth flow. That is
deliberate and correct — repeatedly sending a wrong password to a utility all
night is how accounts get locked.

Before this release, the integration reported an authentication failure too
readily. SRP answers `401` both when your password is wrong *and* when your
session cookie has merely expired, and the client mapped any `401` on any
endpoint to "bad credentials". So a session that quietly aged out — which the
integration cannot avoid, only re-establish — looked identical to a typo'd
password, and one bad refresh froze the integration until someone reloaded it.
Observed on 2026-09-02: a restart, one `401` on the first refresh, and then
about 18 hours of `unavailable` with no retries.

This release fixes that: an expired session is now recognised as an expired
session, and the client logs back in and carries on. Only a failure at the
login endpoint that SRP itself rejects as `invalid_grant` is treated as a
credential problem.

### Is it really my password, or just a dead session?

They fail at different endpoints with different bodies. This is the fastest way
to tell them apart, and worth checking before you go resetting anything at SRP.

A genuinely wrong password fails at **`login/authorize`** with `400`:

```json
{"error":"invalid_grant","error_description":"The user name or password is incorrect."}
```

An expired or missing session fails later, at a **data** endpoint such as
`usage/dailyusagedetail`, with `401`:

```json
{"message":"Authorization has been denied for this request."}
```

Only the first one means your credentials are wrong. If you see the second,
your password is fine and the session needs re-establishing — which the current
release does by itself.

A third case is neither: a `403` whose body is an HTML "Access Denied" page is
Cloudflare refusing the request before SRP sees it. The integration reports that
separately and does not touch your credentials over it.

### Reading the logs

Turn on debug logging for the integration, either from **Settings → Devices &
Services → SRP Energy Monitor → Enable debug logging**, or in
`configuration.yaml`:

```yaml
logger:
  default: info
  logs:
    custom_components.srp_energy_monitor: debug
```

Then read **Settings → System → Logs**. The default view groups repeated
messages, so click *Load full logs* to see them in order with timestamps — the
first failure is the one that explains the outage, and it is the one grouping
buries. The lines worth finding are the HTTP status and path of the failing
call, and whether the integration decided this was an auth problem or a
transport problem.

**What each line means** is tabulated in
[docs/HOME-ASSISTANT.md → Checking the logs](HOME-ASSISTANT.md#checking-the-logs):
what a healthy refresh looks like, and the five messages worth recognising when
it is not healthy.

### Getting it polling again

Reload the config entry. Either:

- **Settings → Devices & Services → SRP Energy Monitor → ⋮ → Reload**, or
- Developer Tools → Actions:

```yaml
action: homeassistant.reload_config_entry
target:
  entity_id: sensor.srp_account_<your account>_current_demand
```

A reload logs in from scratch, so all six sensors repopulate within one refresh.
Restarting Home Assistant does the same thing more slowly. Neither loses your
long-term statistics.

If a reload fixes it and it breaks again days later, that is the pattern this
release's session handling addresses — and it means the copy you are running is
older than the fix.

### Check you actually have the fixed copy

The session-recovery fix landed in **1.2.1**. A hand-copied install never
updates itself, so the copy in Home Assistant can be months behind this
repository without any sign that it is.

Open `/config/custom_components/srp_energy_monitor/manifest.json` and read the
`version` field:

```json
"version": "1.2.1"
```

`1.2.0` is the build that had the outage. If you see it — or anything older —
copy `custom_components/srp_energy_monitor/` over from this repository again and
restart Home Assistant. Installing it is described in
[docs/HOME-ASSISTANT.md](HOME-ASSISTANT.md#installing-it).

### Testing SRP from the Home Assistant host

Don't use `curl` for this. It answers `403` whether or not anything is wrong,
including from machines where the integration is polling successfully at that
very moment — see
[Is SRP reachable?](#is-srp-reachable-read-before-testing-with-curl) at the top
of this page. Read the integration's own debug log instead.
