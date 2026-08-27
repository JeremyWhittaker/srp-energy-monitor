# Security

## Reporting a vulnerability

Please report security issues privately via
[GitHub Security Advisories](https://github.com/JeremyWhittaker/srp-energy-monitor/security/advisories/new),
or by email to me@jeremywhittaker.com.

Please don't open a public issue for anything that would expose someone's
credentials or usage data. Expect a first response within a week. This is a
personal project maintained in spare time, with no bounty programme.

## What this project handles

Running it means handing your **Salt River Project account password** to a
program that drives a browser, and accumulating a database of when your house
uses electricity. Both deserve care.

### Credentials

- Read from the environment or `.env`; keep that file mode `600`.
- Your **password** is never written to the database, never logged, and never returned by any
  API endpoint.
- Your **username** is logged at INFO on each login (`logging in to SRP as <username>`), so
  scrub it before pasting a log into an issue.
- `/health` reports whether credentials are *configured*, as a boolean — but it also returns
  your database path and data coverage, so it is not a safe endpoint to expose either.
- Not sent anywhere except SRP's own login form.

### The API has no authentication

This is deliberate — it is a read-only service designed to sit behind something
that authenticates — but it means:

- It binds to `127.0.0.1` by default. **Keep it there** unless you have put
  something in front of it.
- `POST /api/v1/fetch` triggers a login to SRP. Anyone who can reach the port
  can trigger it.
- Everything else will hand your full usage history to any caller.

If you expose it, use a reverse proxy with auth, a VPN, or a private overlay
network. Interval data is a good occupancy sensor: it shows when a home is
empty.

### Your data

- `data/` is gitignored, along with `*.db` and `srp_*.csv`.
- Raw CSV downloads are kept by default (`SRP_KEEP_DOWNLOADS=true`) and contain
  the same information as the database.
- Please scrub usernames, account numbers, addresses and real interval data out
  of anything you attach to an issue.

## Supported versions

The latest release on `main`. This project tracks a website that changes without
notice; older versions are not patched.
