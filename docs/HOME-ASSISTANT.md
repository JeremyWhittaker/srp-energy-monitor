# SRP Energy Monitor in Home Assistant

A guide to the custom integration that ships in this repository at
`custom_components/srp_energy_monitor/`. It is written for someone who runs Home Assistant and
pays an SRP bill, not for a programmer. Jargon is defined the first time it appears.

- [What this gives you](#what-this-gives-you)
- [Before you start](#before-you-start)
- [Installing it](#installing-it)
- [Setting it up](#setting-it-up)
- [What you get](#what-you-get)
- [The dashboard](#the-dashboard)
- [How it updates itself](#how-it-updates-itself)
- [When it stops updating](#when-it-stops-updating)
- [Common questions](#common-questions)

---

## What this gives you

Six sensors and four history series, taken straight from your own SRP account and refreshed on
their own every four hours:

- what your **demand charge** is running at this billing cycle, and the single worst half hour
  behind it;
- **SRP's own bill projection** in dollars;
- yesterday's **consumption, on-peak generation and metered read** in kWh;
- and **years of hourly history** — usually back to whatever SRP still retains — that Home
  Assistant will chart for you the moment it is installed.

The demand number is the reason this exists. On SRP's demand price plans (E-15, E-16, E-27) part
of your bill is not charged on how much electricity you used at all. It is charged on **how hard
you pulled at your worst moment** — the highest half hour or hour of on-peak draw in the cycle, or
the average of each day's worst on-peak stretch, depending on the plan. Everything running at once
for twenty minutes on a July evening can add more to the bill than an entire ordinary day of use.
It is a number SRP prints on one web page and nowhere else, and this integration puts it in front
of you where you can watch it, chart it, and build automations against it.

> [!NOTE]
> This is **not** Home Assistant's built-in `srp_energy` integration. That one reports a single
> kWh total and cannot see demand. The two use different names internally and can be installed
> side by side.

---

## Before you start

You need three things.

**1. An SRP My Account login without multi-factor authentication.** The integration signs in the
same way you do, by posting your username and password to SRP's login endpoint. Nothing in it can
answer a texted code or an authenticator prompt. **If your SRP account has MFA turned on, this
integration cannot log in and there is no workaround.**

**2. Your nine-digit SRP account number.** It is printed on your bill and shown in My Account.
Dashes are fine; they are stripped.

**3. Home Assistant with the recorder enabled.** The recorder is the component that stores
history in a database — it is on by default, and you only lack it if you deliberately removed it.
The integration declares the recorder as a hard dependency, because that is where its years of
history are written.

There is nothing else to install. The integration ships with an empty requirements list and talks
to SRP using libraries Home Assistant already has, so setup cannot fail on a missing package —
which matters on Home Assistant OS, where building one is often impossible.

---

## Installing it

HACS is **not** supported yet. There is no `hacs.json` in this repository, so HACS will not see it.
Installation is a manual copy.

Copy the whole `custom_components/srp_energy_monitor/` directory from this repository into your
Home Assistant configuration directory, so that it ends up like this:

```text
<config>/
└── custom_components/
    └── srp_energy_monitor/
        ├── __init__.py
        ├── config_flow.py
        ├── const.py
        ├── coordinator.py
        ├── manifest.json
        ├── sensor.py
        ├── srp_api.py
        ├── strings.json
        └── translations/
            └── en.json
```

`<config>` is wherever your `configuration.yaml` lives — `/config` on Home Assistant OS. Create the
`custom_components` folder if it is not there, and do not copy any `__pycache__` folder. Then
**restart Home Assistant**: custom integrations are only discovered at startup.

This repository is the source of truth for those files. Home Assistant will not update them for
you, and neither will anything else — so **every time you pull a newer version of this repository,
copy the directory over again and restart**.

To confirm which version you are running, open `manifest.json` in your config folder and read the
`version` field. The current release is:

```json
"version": "1.2.3"
```

If it says **`1.2.0`**, you have the build that could stop polling permanently after a single
expired session — copy the directory over again and restart. See
[When it stops updating](#when-it-stops-updating).

---

## Setting it up

Home Assistant calls the short question-and-answer form for adding an integration a **config
flow**, and it calls the saved result a **config entry** — one account's worth of settings, which
you can later reload or delete from the UI.

1. Go to **Settings → Devices & Services**.
2. Click **+ Add Integration** (bottom right).
3. Search for **SRP Energy Monitor** and select it.
4. Fill in the three fields:

| Field | What to enter |
|---|---|
| **Account number** | Your nine-digit SRP account number, with or without dashes. |
| **Username** | The username you use at myaccount.srpnet.com. |
| **Password** | That account's password. Stored by Home Assistant, sent only to SRP. |

The form signs in to SRP before it accepts anything, so a typo is caught immediately. You may see
one of these messages:

| Message | What it means |
|---|---|
| The account number should be nine digits | The account field is not nine digits after dashes are removed. |
| SRP rejected that username and password | The credentials really were refused. Check them at myaccount.srpnet.com. |
| SRP's bot protection refused the request | Cloudflare, which sits in front of SRP, declined the call. Your password is probably fine — this varies by network and moment. Wait a few minutes and try again. |
| Could not reach SRP | A network or timeout problem. |

Each account can only be added once; a second attempt with the same number is rejected as already
configured.

### The first run takes a while — that is normal

As soon as the entry is created, the integration backfills history. It asks SRP for **760 days**
of interval data, and because SRP silently truncates any request wider than about a month, it has
to ask in roughly twenty-five separate month-long windows, pausing half a second between them to
be polite to someone else's website. Asking for more days than your account has is harmless — the
extra windows simply come back empty.

So expect setup to sit there for a few minutes, and the sensors to appear only when it finishes.
After that, each refresh revisits seven completed days in **America/Phoenix**, plus any gap
since collection stopped. The current partial day is excluded. This catches utility revisions
without downloading the entire retained history on every refresh.

How far back you actually get depends on what SRP retains for your account. On the account this
was developed against, hourly energy reached back to **2024-07-29**, and both
generation and half-hourly demand to **2025-05-04**, when the solar array came online and the
account moved to a demand plan. Your dates will differ.

### Utility restatements and existing history

SRP can revise an hour after it first appears. The importer replaces that hour's energy
`state` and recomputes every affected cumulative `sum` through the last recorded settled hour.
It starts from the preceding recorded cumulative total, using Home Assistant's supported
external-statistics import API and preserving statistic IDs. Signed net energy remains signed;
export hours can reduce its cumulative total.

An unchanged re-import is idempotent. A missing source hour retains an existing recorded
reading; it is never silently replaced with zero. Conflicting duplicates, sub-hourly energy,
non-finite readings, an unfilled gap, an unknown baseline or a future/unsettled stored tail
cause that series' replay to be skipped with a warning. Current account sensors can still
refresh. A new series starts at zero only when there is no earlier history, or its first
recorded point establishes that zero origin.

**Upgrading from 1.2.1 or earlier:** the old importer could leave revised earlier hours stale
and add its unchanged last hour to `sum` again. The seven-day replay repairs affected recent
states and later sums relative to the preceding baseline. It does not remove corruption
already embedded in an older baseline or recover missing older history automatically.

Before repairing older data, privately export exact affected statistic metadata and rows,
record the intended date range and known preceding cumulative total, and retain a rollback.
Use a targeted replay through the same supported API, including every later cumulative row
whose value changes. Verify raw utility intervals, hourly `state`, adjacent `sum` differences
and a repeated unchanged import before accepting the result. Preserve IDs; do not edit the
recorder database or purge/reseed all history as a shortcut. This release does not itself run
an unbounded historical repair.

---

## What you get

### One device, six sensors

Everything lands under a single device named **SRP Account &lt;your number&gt;**. The entity IDs
follow from that name, so on account 885789006 they look like this:

| Entity ID | What it means | Unit | Extra attributes |
|---|---|---|---|
| `sensor.srp_account_885789006_current_demand` | The demand figure your current cycle is being charged on. On E-15 and E-16 this is the **average** of each day's worst on-peak stretch; on E-27 it is the cycle **maximum**. | kW | `is_average` (true/false), `quantity` ("cycle mean of daily maxima" or "cycle maximum") |
| `sensor.srp_account_885789006_billed_cycle_peak` | The single highest on-peak half hour of the current cycle — the worst moment itself. | kW | `occurred_at` (when it happened), `cycle_start`, `cycle_end` |
| `sensor.srp_account_885789006_bill_projection` | SRP's own projection of what this cycle's bill will come to. Blank if SRP has not produced one yet. | USD | — |
| `sensor.srp_account_885789006_daily_consumption` | Energy consumed on the most recent settled day. | kWh | — |
| `sensor.srp_account_885789006_daily_generation` | SRP's daily **on-peak generation field** for the most recent settled day — see the warning below. Displayed as "Daily on-peak generation". | kWh | — |
| `sensor.srp_account_885789006_daily_net_energy` | What the meter actually registered on the most recent settled day, after exports. | kWh | — |

Every one of the six also carries `attribution` ("Data from Salt River Project") and
`data_through` — the date the figures are good through, which is normally **yesterday**. When a
sensor looks stale, `data_through` is the attribute to check.

One live reading from that account, so the shape is concrete: `current_demand` was **5.26 kW**
with `is_average: true`, while `billed_cycle_peak` was **12.7 kW**, peaked at
`2026-08-18T20:00:00`. Same cycle, different quantities — which is why both exist.

> [!IMPORTANT]
> **Entity IDs are fixed the first time an entity is created.** If you installed an earlier version
> before the generation sensor was renamed, its ID stays `..._daily_generation` even though it now
> displays as "Daily on-peak generation"; a brand-new install may instead get
> `..._daily_on_peak_generation`. The bundled dashboard uses the former. If a dashboard card shows
> "Entity not found", look the real ID up under **Settings → Devices & Services → Entities** and use
> that.

### Four long-term statistics — these behave differently

Alongside the sensors, the integration writes four **long-term statistics**. A statistic is not a
sensor. It is a history series stored directly in Home Assistant's database, one row per hour,
which the recorder keeps forever rather than purging. That is how years of past readings can exist
for an integration you installed this morning — the sensors only know today, but the statistics
were backfilled with everything SRP would hand over.

Because they are not sensors, they are **not** named `sensor.something`. They are named with a
colon:

| Statistic ID | What it holds | Unit |
|---|---|---|
| `srp_energy_monitor:885789006_energy_consumption` | Hourly SRP consumption series, as a running total | kWh |
| `srp_energy_monitor:885789006_energy_generation` | Hourly SRP generation series, as a running total; whole-roof coverage unverified | kWh |
| `srp_energy_monitor:885789006_energy_net` | Hourly metered read, as a running total | kWh |
| `srp_energy_monitor:885789006_demand_kw` | Half-hourly **on-peak** demand, summarised per hour as an average, a minimum and a **maximum** | kW |

Replace `885789006` with your own account number. You will find them in the entity picker of a
**Statistics card** or **Statistics graph card**, and under **Developer tools → Statistics**.

For the demand series, the **max** column is the one worth charting. It preserves the highest half
hour inside each hour, which is the granularity the demand charge is actually billed on; an hourly
average would quietly understate your real peak. Only the **on-peak** leg is stored. SRP publishes
an off-peak demand figure too, but no bill has ever contained it, and on a solar account it is far
larger — 25.3 kW against a 12.7 kW billed peak over one measured cycle — so charting both would
produce something that looks like a demand graph while showing a number you were never charged for.

The three kWh series carry a running total and can be offered on Home Assistant's **Energy
dashboard** or statistics cards. Check the physical measurement boundary before selecting them:
SRP's consumption and generation series have not been reconciled to all inverter and panel
totals. Adding overlapping sources can count the same energy twice. The demand series is a rate
in kW with no total, so it belongs on statistics cards only.

> [!IMPORTANT]
> **Two pairs that are easy to confuse.**
>
> **`sensor.…_daily_generation` keeps only SRP's daily on-peak field.** Its scope, name and ID
> remain unchanged. The daily list can also carry off-peak generation: November 2026 responses
> contain nonzero values. The `energy_generation` statistic sums the hourly portal generation
> series. On 2026-08-27 those two figures were 21 kWh and 84.5 kWh; neither was independently
> established as whole-roof production. The portal's generation semantics and its relationship
> to hardware solar totals remain unresolved.
>
> **`sensor.…_daily_net_energy` is the metered read, not your consumption.** On a solar account
> those are different quantities: consumption is what you used, the metered read is what the meter
> registered after exports were netted off. Consumption is `daily_consumption`.

---

## The dashboard

A ready-made three-view dashboard is in [`ha-dashboard.yaml`](ha-dashboard.yaml), next to this file.

To use it:

1. Open (or create) a dashboard, click the **pencil** to edit it.
2. Open the **three-dot menu → Raw configuration editor**.
3. Paste the file's contents in, replacing what is there.
4. **Replace `123456789` with your own nine-digit account number everywhere it appears** — it is
   part of both the entity IDs and the statistic IDs, so a missed one shows as an empty card.
5. Save.

Two things in it are not yours and should be deleted: the `input_boolean.juicebox_srp_*` cards on
the *Daily Shape* view come from a separate project, and the markdown card on that same view
contains figures measured on one specific account over one specific cycle. Rewrite it with your own
numbers or remove it.

The three views:

| View | What it shows |
|---|---|
| **Bill Cycle** | The cycle in progress. Badges and tiles for the projected bill, the billed demand figure and the worst half hour; a gauge for demand scaled 0–15 kW with a yellow band at 6 and red at 9; a 45-day chart of the daily on-peak maximum; month-to-date and rolling-30-day totals for SRP consumption and generation; tiles for the last settled day; and a 60-day bar chart of the metered read. |
| **Daily Shape** | An hour-by-hour chart of on-peak demand over the last 7 days, with SRP consumption and generation over the same week. The consumption series is not labelled as a separate grid-import meter. |
| **History** | Daily bars for SRP consumption, generation and metered read across 760 days, and a 460-day line of the daily on-peak maximum. |

Adjust the gauge's `min`, `max` and `severity` thresholds to your own household — 15 kW full scale
suits the account it was built on and may be far too high or too low for yours.

---

## How it updates itself

The integration polls SRP **every four hours**, from the moment Home Assistant starts. There is no
button to press and no automation to write.

Polling more often would achieve nothing. SRP settles the previous day's meter data overnight and
publishes nothing in between, so between settles there is simply no new number to fetch. Four hours
is frequent enough to catch a late settle without hammering someone else's website.

The consequence is worth stating plainly: **this is billing data, not a live power meter.** The
daily figures describe *yesterday*, and the demand and projection figures describe the cycle so far.
The `data_through` attribute on every sensor tells you which day the data actually runs to. If you
want to know what your house is drawing right now, you need a meter; what this gives you is what
SRP is going to charge you for, while you can still do something about it.

### Which part of the project does what

This repository holds three separable pieces. The Home Assistant side needs neither of the
others:

| Piece | What it does | Needed for Home Assistant? |
|---|---|---|
| **The integration** (`custom_components/srp_energy_monitor/`) | Runs inside Home Assistant, signs in to SRP itself, stores everything in Home Assistant's own database. | This is the one. |
| **The collector** (the `srp-energy-monitor` command, its SQLite database and timer) | Logs in on a server and stores readings in `data/srp.db` for the CLI, exports and Prometheus. | **No.** |
| **The website / REST API** (`srp-energy-monitor serve`) | Serves a standalone dashboard and JSON API from that database. | **No.** |

You do not need a server, a database, or a browser anywhere for the integration to work. It is
self-contained.

---

## When it stops updating

### The symptom

All six sensors show **unavailable** and stay that way — not for an hour, but indefinitely — and
Home Assistant may show a **"reauthentication needed"** notice on the integration asking for your
password again.

On a dashboard, the two kinds of card behave differently and the contrast is the giveaway: tile
and entity cards show the word **Unavailable** in place of the number, while statistics-graph
cards keep drawing the history they already recorded and simply **stop at a fixed time**. Nothing
shows you a stale number as though it were current — you get blanks beside charts with a cliff
edge in them. The time of that cliff is when the failing refresh happened.

### Why it happens

When an integration reports that its credentials were rejected, Home Assistant deliberately
**stops polling altogether** and waits for a human. That is correct behaviour in general: retrying
a wrong password every four hours forever would eventually get the account locked.

The trap is what counts as "rejected". SRP's session cookie can expire, and an expired session
produces exactly the same HTTP 401 as a wrong password does. In releases before this one, that
meant a brief session expiry or network hiccup at the wrong moment could be misread as a bad
password — and once Home Assistant stopped polling, it never tried again on its own. Observed on
the development account: Home Assistant restarted at 17:40, the first refresh hit a 401, and all six
sensors sat unavailable for about eighteen hours without a single retry.

### Getting it back

**Reload the integration.** This costs nothing and takes seconds:

**Settings → Devices & Services → SRP Energy Monitor → three-dot menu → Reload**

The equivalent as a service call, if you would rather script it:

```yaml
action: homeassistant.reload_config_entry
target:
  entity_id: sensor.srp_account_885789006_current_demand
```

A reload signs in from scratch and repopulates every sensor. Restarting Home Assistant works too,
but is slower and unnecessary.

**As of version 1.2.1 this should no longer stick.** Only a genuine rejection at SRP's login
endpoint is now treated as a credentials problem. A failure on the data endpoints makes the
integration throw its cached session away, sign in again and retry, and if it still cannot get
through it reports an ordinary temporary failure — which Home Assistant retries by itself. A
reauthentication prompt should now mean what it says: your password really has changed — and the
prompt now takes a new username and password and puts the integration straight back to work,
rather than sending you round to delete and re-add it.

So before anything else, **check which version you are running**: open
`/config/custom_components/srp_energy_monitor/manifest.json` and look at `version`. `1.2.1` and later have
the session-recovery fix; `1.2.0` is the build that had the outage. A hand-copied install never updates itself, so
this is the single most likely reason to still be seeing it.

### Checking the logs

To see what the integration is actually doing, add this to `configuration.yaml` and restart:

```yaml
logger:
  logs:
    custom_components.srp_energy_monitor: debug
```

Then read **Settings → System → Logs**, or `home-assistant.log` in your config folder.

A healthy cycle looks like this:

```text
DEBUG (MainThread) [custom_components.srp_energy_monitor] fetching intervals 2026-09-02 .. 2026-09-03 for statistics
DEBUG (MainThread) [custom_components.srp_energy_monitor] wrote 48 srp_energy_monitor:885789006_energy_consumption statistics
DEBUG (MainThread) [custom_components.srp_energy_monitor] wrote 24 demand statistics
DEBUG (MainThread) [custom_components.srp_energy_monitor] Finished fetching srp_energy_monitor data in 4.102 seconds (success: True)
```

On a first run you will also see, at ordinary info level with no extra configuration needed:

```text
INFO (MainThread) [custom_components.srp_energy_monitor] no SRP statistics yet; backfilling 760 days
```

Lines worth recognising when something is wrong:

| Log line | What it means |
|---|---|
| `SRP says the username or password is incorrect (invalid_grant) on login/authorize` | A real credentials failure — SRP's own words for it. Check your login at myaccount.srpnet.com. |
| `… answered 401: the SRP session has expired or was evicted` | A stale session. The integration signs in again and replays the request by itself, so normally you never see this at all — it only reaches the log if the retry fails too. |
| `Cloudflare refused … (403). Your credentials are not the problem` | SRP's bot protection. Transient; it retries on its own and usually clears. |
| `Error fetching srp_energy_monitor data: …` | A temporary failure of any kind. Home Assistant will retry at the next interval. |
| `statistics update skipped: …` | The sensors updated but the history did not. Harmless once; if it repeats, the history will have gaps. |

Turn the debug logging back off when you are done — it is chatty. Remember that logs can contain
your account number, so scrub it before pasting one into a GitHub issue.

> [!TIP]
> Do not try to diagnose this with `curl` from the Home Assistant host. `myaccount.srpnet.com`
> answers `403` to a plain HTTP client whether or not anything is wrong — including while the
> integration is polling successfully — so the result tells you nothing either way. This log is
> the test. See
> [docs/TROUBLESHOOTING.md → Is SRP reachable?](TROUBLESHOOTING.md#is-srp-reachable-read-before-testing-with-curl).

---

## Common questions

**Why is my demand number different from what the SRP website shows?**

Most often because you are comparing two different quantities. `current_demand` is what your plan
bills on — an **average** of each day's worst on-peak stretch on E-15 and E-16, a cycle **maximum**
on E-27 — while `billed_cycle_peak` is always the single worst half hour of the cycle. On one real
cycle those read 5.26 kW and 12.7 kW at the same time. Check the `is_average` attribute on
`current_demand` to see which quantity your plan produced. If you are on an average plan, SRP does
not publish the average directly; it is reconstructed from daily on-peak maxima, excluding weekends
and SRP's six holidays. Ordinary zero-demand weekdays still count. See the
[calendar and tariff evidence](../README.md#which-plans-charge-for-demand).
This rule follows the tariff wording; it has not yet been reconciled against a holiday-containing
billing cycle. The 5.62 kW calculation versus 5.60 kW displayed was checked on the non-holiday
2026-08-10 cycle, with daily values rounded to one decimal.

**Why is generation lower than my inverter says?**

The daily sensor retains SRP's **on-peak field only**. The hourly `energy_generation` statistic
is a different portal series, and neither has been verified as a measurement of every solar
panel. Daily off-peak values are sometimes nonzero; the older claim that they were always zero
was based on limited observations. Compare matching dates and measurement boundaries before
drawing conclusions about inverter or panel performance.

**Why is there no data for today?**

SRP settles meter data overnight, so the most recent complete day is yesterday. Every sensor
carries a `data_through` attribute stating the date its figures run to. Nothing is broken; the data
does not exist yet.

**Does this work with multi-factor authentication?**

No. The integration signs in with a username and password and has no way to answer a code prompt.
An SRP account with MFA enabled cannot be used with it.

**Do I need the website or the daily collector running too?**

No. The Home Assistant integration fetches its own data straight from SRP and stores it in Home
Assistant's database. The collector (which fills a SQLite database for the command-line tools and
Prometheus) and the web dashboard are independent pieces of the same project, useful if you want
data outside Home Assistant. Nothing in Home Assistant depends on either.
