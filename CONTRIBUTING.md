# Contributing

Thanks for looking. This project exists because SRP has no API, so most of the
value here is knowledge about a website that changes without notice — bug
reports and selector fixes are worth as much as features.

## The one thing to know first

**A green test suite does not mean the scraper works.**

The tests run against CSV fixtures with no network, which makes them fast and
safe but blind to the thing most likely to break. Every markup bug this project
has hit passed a green suite. If your change touches `srp_energy/client.py`,
verify it against the live portal and say so in the PR:

```bash
srp-energy-monitor fetch --show-browser   # watch it happen
srp-energy-monitor show --days 7          # eyeball against myaccount.srpnet.com
```

## Setup

```bash
git clone https://github.com/JeremyWhittaker/srp-energy-monitor.git
cd srp-energy-monitor
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
playwright install chromium    # only needed to run the scraper
pytest
```

You do **not** need an SRP account to work on parsing, storage, the API, the
CLI or the dashboard — that is the point of keeping Playwright confined to one
module.

## The architectural rule

**Only `client.py` may import Playwright.** Parsing, storage, the API and the
CLI work entirely on saved CSVs, which is why the API keeps serving history
after SRP breaks the scraper. If you find yourself reaching for Playwright
anywhere else, that is the bug.

Selectors are *cascades* of candidates, not single strings, ordered
"most recently observed to work" first. Add to the front; don't delete the
fallbacks, even the ones that look dead — one of them was resurrected from a
script that had been written off as superseded and turned out to be current
markup.

## Style and checks

```bash
pytest
ruff check .
ruff format --check .
```

pytest runs on Python 3.10 through 3.13; ruff check and ruff format --check run on a single Python 3.12 job. Line length is 100. There is no
type-check gate, but the code is annotated and new code should be too.

## Tests

New parsing behaviour needs a fixture. Put it in `tests/fixtures/` in SRP's real
export shape — UTF-8 BOM, CRLF line endings, quoted numerics, the ragged
`12:0 AM` clock, the `Combined total` summary row — with **invented numbers**.

Never commit a real export. Real interval data reveals when a house is empty.

## Reporting a broken scraper

Use the *Scraper stopped working* issue template. Run
`srp-energy-monitor fetch --show-browser` first and attach a screenshot of where
it stalls — that single image usually identifies the fix. Scrub your username,
account number and address from anything you paste.

## Things that would genuinely help

- **A different price plan.** The headline demand figure is captioned
  differently on E-27/E-28 (a peak) than on the average-demand plans (a mean),
  and the code is built to carry SRP's own caption rather than assume. Confirming
  what your plan's card actually says — and what selector it lives under — makes
  it correct for more people.
- **Solar-free accounts.** Development happened on an account with generation.
  If the portal hides the Generation chart entirely without it, the export
  sequence needs to cope.
- **A Home Assistant custom component** built on the REST API.
- **A shareable Grafana dashboard JSON** for the `/metrics` endpoint.

## Scope

This collects SRP data and serves it. Inverter polling, solar monitoring and
bill optimisation belong in whatever you build *on top of* the API — the last
attempt at putting them in one app is precisely what this was carved out of.

## Licence

Contributions are accepted under the [MIT licence](LICENSE).
