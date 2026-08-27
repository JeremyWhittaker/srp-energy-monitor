## What this changes

<!-- One or two sentences. Link the issue if there is one. -->

## How it was verified

<!--
`pytest` is necessary but not sufficient for anything touching the scraper —
every markup bug this project has hit passed a green suite. If you changed
client.py, please say what you ran against the live portal.
-->

- [ ] `pytest` passes
- [ ] `ruff check .` and `ruff format --check .` pass
- [ ] Scraper changes only: verified against the live SRP portal with
      `srp-energy-monitor fetch` (and `--show-browser` if selectors moved)
- [ ] Docs updated (README / docs/ / CLAUDE.md) if behaviour or flags changed
- [ ] No credentials, account numbers or real usage data in the diff
