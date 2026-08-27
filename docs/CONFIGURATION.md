# Configuration

All configuration is environment variables, optionally loaded from a `.env`
file in the working directory. Copy `.env.example` to start.

Precedence: real environment variables win over `.env`, and CLI flags win over
both. Point at a different file with `--env-file`, or skip file loading
entirely with `--env-file /dev/null`.

## Settings

| Variable | Default | Purpose |
|---|---|---|
| `SRP_USERNAME` | — | SRP My Account username. Required to fetch. |
| `SRP_PASSWORD` | — | SRP My Account password. Required to fetch. |
| `SRP_DATA_DIR` | `./data` | Base directory for the database and downloads. |
| `SRP_DATABASE_PATH` | `$SRP_DATA_DIR/srp.db` | SQLite file. |
| `SRP_DOWNLOADS_DIR` | `$SRP_DATA_DIR/downloads` | Where raw CSV exports land. |
| `SRP_KEEP_DOWNLOADS` | `true` | Keep raw CSVs after import. Set `false` to delete once parsed. |
| `SRP_HOST` | `127.0.0.1` | API bind address. |
| `SRP_PORT` | `8000` | API port. |
| `SRP_HEADLESS` | `true` | Set `false` to watch the browser work. |
| `SRP_TIMEOUT_MS` | `120000` | Playwright default timeout. |
| `SRP_DOWNLOAD_TIMEOUT_MS` | `60000` | How long to wait for an export to generate. |
| `SRP_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |

Booleans accept `1/true/yes/on` (case-insensitive); anything else is false.

## Credentials

Credentials are read from the environment only. They are never written to the
database, never logged, and never returned by any API endpoint — `/health`
reports `credentials_configured: true|false` and nothing more.

`.env` is gitignored. If you would rather not have a plaintext file at all,
export the variables from your shell profile or a systemd `EnvironmentFile`
with `0600` permissions.

## Running as a service

The scraper is a batch job, not a daemon: run it from cron or a systemd timer
and let it exit. Only `serve` is long-running.

`/etc/systemd/system/srp-energy-monitor.service` — the API:

```ini
[Unit]
Description=SRP Energy Monitor API
After=network-online.target

[Service]
Type=simple
User=srp
WorkingDirectory=/opt/srp-energy-monitor
EnvironmentFile=/opt/srp-energy-monitor/.env
ExecStart=/opt/srp-energy-monitor/.venv/bin/srp-energy-monitor serve
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/srp-fetch.service` + `.timer` — the daily scrape:

```ini
# srp-fetch.service
[Unit]
Description=Fetch SRP energy data

[Service]
Type=oneshot
User=srp
WorkingDirectory=/opt/srp-energy-monitor
EnvironmentFile=/opt/srp-energy-monitor/.env
ExecStart=/opt/srp-energy-monitor/.venv/bin/srp-energy-monitor fetch
```

```ini
# srp-fetch.timer
[Unit]
Description=Fetch SRP energy data daily

[Timer]
OnCalendar=*-*-* 06:15:00
RandomizedDelaySec=900
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl enable --now srp-energy-monitor.service srp-fetch.timer
```

### Or as a user timer, with no root at all

`deploy/srp-fetch.service` and `deploy/srp-fetch.timer` in this repo are
ready-made user units. They use `%h` so they work from any home directory:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/srp-fetch.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now srp-fetch.timer

# Required, or user timers only run while you are logged in:
sudo loginctl enable-linger "$USER"
```

Check it with `systemctl --user list-timers srp-fetch.timer` and read a run
with `journalctl --user -u srp-fetch.service`.

The unit runs `fetch` and then `hourly --days 2`, and sets
`SuccessExitStatus=0 1` deliberately: a scrape against someone else's website
will fail occasionally, and a failed unit that retries tomorrow is better than
one that flaps.

`RandomizedDelaySec` spreads the load so you are not hitting SRP at the same
second every morning. `Persistent=true` catches up a run missed while the
machine was off.

Use absolute paths in both units — a relative `WorkingDirectory` or a
`.venv` path that doesn't exist is the most common reason a unit silently
fails to start.

## The database

SQLite at `$SRP_DATA_DIR/srp.db`, currently **schema v4**. `Store` creates and
migrates it on construction — there is no separate migration command.

| Table | Holds |
|---|---|
| `daily_reading` | One row per (series, usage date) from the four daily exports |
| `interval_reading` | One row per (series, interval start); demand is half-hourly |
| `demand_observation` | Append-only log of the scraped bill-cycle demand figure |
| `bill_projection` | Append-only log of the scraped projected bill amount |
| `meta` | `schema_version` |

The two reading tables **upsert**, so re-running a fetch corrects history rather
than duplicating it. SRP restates recent days as meter data settles, so that
behaviour is load-bearing, not an optimisation.

The two scraped scalars are deliberately append-only: both move through the
billing cycle, and the series is the useful part.

Raising `SCHEMA_VERSION` in `srp_energy/storage.py` requires extending
`Store._migrate`. The v1→v2 step is worth reading as a model — it carried old
rows forward with a `NULL` label rather than asserting a meaning they never
had.

To start over, delete the database; everything except the accumulated history
of those two scalars can be re-fetched, and `srp-energy-monitor import` will
rebuild the daily readings from CSVs in `data/downloads/`.

## Exposing the API

There is **no authentication**. The default bind is `127.0.0.1` deliberately.

If you need it reachable from elsewhere, put it behind something that
authenticates: a reverse proxy with basic auth, a Tailscale or WireGuard
interface, or an SSH tunnel. Setting `SRP_HOST=0.0.0.0` on a machine with a
public IP publishes your electricity usage history to the internet.
