# systemd units

Three units, two jobs. They are **user** units — they run as you, out of your
own checkout and its `.env`, and nothing in them runs as root.

| Unit | What it does |
|---|---|
| `srp-fetch.service` | One fetch: today's figures, plus the last two days of hourly data. `Type=oneshot`, no `[Install]` — the timer starts it. |
| `srp-fetch.timer` | Runs the above daily at 07:30 with up to 30 minutes of jitter, and catches up a run missed while the machine was off. |
| `srp-web.service` | The dashboard and REST API (`srp-energy-monitor serve`), running continuously and restarting if it dies. |

The collector and the web server are independent. The timer writes to
`data/srp.db`; the web server reads it, and can kick off a fetch of its own from
the dashboard. You can run either alone.

Neither has anything to do with the Home Assistant integration in
`custom_components/` — that talks to SRP directly from inside Home Assistant and
never touches this database. See `docs/HOME-ASSISTANT.md`.

## Before you install

### 1. A virtualenv, with the right extra

Both `.service` files run a binary out of `.venv/bin/` in the checkout. If that
virtualenv does not exist, or exists without the extra the command needs, the
unit cannot work — so create it first:

```bash
cd ~/projects/srp-energy-monitor
python3 -m venv .venv

# For srp-web.service (the dashboard and API):
.venv/bin/pip install -e '.[api]'

# For srp-fetch.service (the collector drives a real browser):
.venv/bin/pip install -e '.[scrape]'
.venv/bin/playwright install chromium

# Or both at once:
.venv/bin/pip install -e '.[all]'
.venv/bin/playwright install chromium
```

`.[api]` is FastAPI and uvicorn; `.[scrape]` is Playwright and python-dotenv;
`.[all]` is both. The package itself declares no dependencies, so a bare
`pip install -e .` gives you a `srp-energy-monitor` that cannot serve and
cannot fetch.

Getting this wrong is worth spelling out because it fails quietly. Without
uvicorn, `serve` prints an install hint and exits 1; `Restart=on-failure` then
restarts it, forever. `srp-web.service` carries `StartLimitIntervalSec=300` /
`StartLimitBurst=5` precisely so that five failures inside five minutes stop the
loop and leave the unit in `failed`, where `systemctl --user status` will tell
you. (Without those, the defaults — five starts per ten seconds — can never be
reached at `RestartSec=10`, and a permanently broken install restarts every ten
seconds indefinitely without ever latching.)

### 2. The paths

The two `.service` files hardcode `%h/projects/srp-energy-monitor` — `%h` is
your home directory — in `WorkingDirectory` and `ExecStart`, and each carries a
comment at the top saying so. If you cloned somewhere else, edit both lines in
each `.service` file you install. `srp-fetch.timer` contains no paths and needs
no edit.

`WorkingDirectory` is load-bearing beyond tidiness: the CLI reads `.env` from the
current directory, so if the unit starts somewhere else it will find no
credentials and no `SRP_HOST`/`SRP_PORT`.

For `srp-fetch.service` that is fatal in the obvious way — `fetch` demands
credentials and exits with a message when it has none. For `srp-web.service` it
is not: `serve` never asks for credentials, so a missing `.env` starts the
dashboard normally on `127.0.0.1:8000`, serving an empty database and reporting
`"credentials_configured": false` from `/health`. If the dashboard is up but
every panel is empty, check `/health` before you check the unit.

### 3. A note on `network-online.target`

`srp-fetch.service` carries `After=`/`Wants=network-online.target`. That target
**does not exist in a user manager** — `systemctl --user status
network-online.target` answers "could not be found" — so the lines are inert
there. They are kept because they cost nothing, they document the intent, and
they are correct if you ever move the unit to the system manager, where a
scraper really does want the network up before it starts.

`srp-web.service` leaves them out: it binds `127.0.0.1` and reads a local SQLite
file, so it has nothing to wait for even under the system manager.

## Install

```bash
mkdir -p ~/.config/systemd/user
cp deploy/srp-fetch.service deploy/srp-fetch.timer deploy/srp-web.service ~/.config/systemd/user/
$EDITOR ~/.config/systemd/user/srp-fetch.service   # only if your checkout is elsewhere
$EDITOR ~/.config/systemd/user/srp-web.service     # likewise

systemctl --user daemon-reload
systemctl --user enable --now srp-fetch.timer      # daily collection
systemctl --user enable --now srp-web.service      # dashboard on 127.0.0.1:8000
```

Then make them survive logout:

```bash
loginctl enable-linger "$USER"
```

Without lingering, your user manager is torn down when your last session ends,
taking your user units with it, and is only started again at your next login.
`loginctl enable-linger` spawns a user manager for you at boot and keeps it after
logout, which is exactly what a daily timer and a long-running server need. (See
`loginctl(1)`, `enable-linger`.) On a machine you SSH into and out of, skipping
this is the single most common reason "the timer never ran".

This is the one step that reaches outside your own account: logind, which runs
as root, writes a flag file under `/var/lib/systemd/linger/`. Enabling linger
for *yourself* is a separate, more permissive polkit action
(`org.freedesktop.login1.set-self-linger`) than enabling it for someone else,
and on current systemd it is allowed without a password. On stricter or older
policies you get an admin prompt — and over SSH, with no polkit agent to show
it, that surfaces as `Interactive authentication required`. If it does, run it
once with `sudo`:

```bash
sudo loginctl enable-linger "$USER"
```

Everything after that is yours again: the units run as you, not as root.

## Check it

```bash
systemctl --user status srp-web.service
systemctl --user list-timers srp-fetch.timer
journalctl --user -u srp-web.service -f
journalctl --user -u srp-fetch.service --since today
curl -s http://127.0.0.1:8000/health
```

To force a collection right now, without waiting for the timer:

```bash
systemctl --user start srp-fetch.service
```

## About the bind address

**The API has no authentication.** It defaults to `127.0.0.1:8000`, and that
default is the security model: anyone who can reach the port can read your entire
usage history and `POST /api/v1/fetch` to make your machine scrape SRP on demand.

`SRP_HOST` and `SRP_PORT` in `.env` change the bind. Setting `SRP_HOST=0.0.0.0`
publishes all of the above to your network — and, if the port is forwarded, to
the internet. If you want it reachable from another machine, terminate TLS and
authentication in a reverse proxy and have that proxy talk to `127.0.0.1:8000`.

A non-numeric `SRP_PORT` is one of the few things that genuinely stops `serve`
from starting: it exits 1 with `SRP_PORT must be an integer`.

## Uninstall

```bash
systemctl --user disable --now srp-web.service srp-fetch.timer
rm ~/.config/systemd/user/srp-{fetch.service,fetch.timer,web.service}
systemctl --user daemon-reload
```
