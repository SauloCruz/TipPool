<p align="center">
  <img src="static/logo.png" alt="TipPool — fair, transparent, together" width="220">
</p>

# TipPool

**Multi-venue tip pool management for restaurants — correct to the cent, auditable to the keystroke.**

TipPool replaces error-prone spreadsheet tip pools with a small, boring, thoroughly-tested
web app. It pulls sales, tips, and timecards from Square, computes each day's tip
distribution deterministically, locks finalized days into immutable snapshots, and produces
the exact reports a restaurant needs to pay people — weekly cash payouts and
payroll-ready exports.

Built for and battle-tested at three working venues in Seattle: a bar running an
hours-proportional tip pool, an Italian restaurant running a percentage tip-out model, and
a Mexican restaurant running a points-and-hours pool. One app, one login, three completely
different sets of rules.

![Python](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-backend-009688?logo=fastapi&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-storage-003B57?logo=sqlite&logoColor=white)
![Frontend](https://img.shields.io/badge/frontend-no--build%20vanilla%20JS-F7DF1E?logo=javascript&logoColor=black)
![License](https://img.shields.io/badge/license-Elastic%202.0-0377CC)

---

## Why it exists

Tip pools are payroll. A spreadsheet that miscounts a divisor or sums the wrong column
range doesn't just make a mess — it pays real people the wrong money. TipPool was built
after auditing exactly those bugs in a production workbook, with three design rules:

1. **Correct by construction.** All money is integer cents. Payout splits use
   deterministic largest-remainder rounding, and every pool is covered by conservation
   invariants — the cents always add up, asserted in the engine itself and in the test
   suite, never "close enough."
2. **Never silently guess.** Unmapped Square categories, unknown team members, and
   unattributed tips *block* the day until a human decides. Every automatic value shows
   its provenance; every manual override records who, when, and what the original was.
3. **Nothing is ever lost.** Finalizing a day writes an immutable snapshot of inputs and
   outputs. Edits require an explicit reopen and produce a new version — history is
   retained forever, and nothing in the app can hard-delete a finalized day.

---

## Features

### Three tip models, one platform

| | Hourly pool (`POOL_HOURS`) | Percent tip-out (`PERCENT_TIPOUT`) | Points × hours (`POINTS_HOURS`) |
|---|---|---|---|
| Who earns | Front-of-house pool by tippable hours; kitchen gets a % of food sales | Each server keeps 65% of their own tips; 20% / 10% / 5% tip out to bussers, host, kitchen | Everything pooled, split 80% front-of-house / 20% kitchen; each side divided by points × hours |
| Hours | Timecards clipped to the open-to-public window (prep and after-midnight work excluded), then rounded up to the next 0.05 h | Presence-based — single shift, checkbox roster, even splits | As Square reports them, to the hundredth of an hour; no window clipping |
| Role weighting | Host/door shifts earn half credit per hour, marked per person per day (staff work dual roles); the rate is configurable and recorded on each snapshot | Fixed roles per person, no hourly weighting | Points per role (1.25 bartender / shift lead, 1.0 server, 0.5 support). **The role comes from the shift, not the person** — Square records the job chosen at clock-in, so someone who bartends Friday and hosts Saturday is credited correctly for each |
| Kitchen | Daily even split of the food-sales allocation | Monthly pool: the 5% accumulates and is split among a roster chosen at payroll time | 20% of every pool, split by kitchen hours worked |
| Edge cases | Negative-pool days flagged, never paid negative | No-host nights re-route the host share to bussers; empty pools return to contributing servers — all flagged, all configurable | Unmapped job title blocks the day; card processing fee withheld before pooling; excluded jobs earn nothing and never dilute anyone else's share |
| Gratuity | Separate pool, hours-proportional | Separate pool, even split among front-of-house | Separate pool, same 80/20 split and the same points |
| Periods | Semi-monthly | Weekly Fri–Thu cash payout + monthly payroll | Semi-monthly |

Auto-gratuity (service charges) is tracked separately from tips end-to-end in all three
models — different tax treatment, separate payroll line, never merged.

The points model also handles **private events**: staff clocked in under an event job are
paid from that event's pool instead of the night's, support roles are tipped out a
configurable percentage of the event's front-of-house portion per role, and the venue's
admin fee never touches the staff pool.

**Card processing fees.** Where the venue's policy withholds the processor's fee before
distributing, the fee applies to everything the processor actually handled — card tips and
auto-gratuity, never cash — so every share is of money the venue really received. The rate
is a setting that defaults to zero (it must be set deliberately, never assumed) and the
rate used is written into each day's snapshot, so changing it never moves a finalized day.

### Square integration that doesn't trust itself

- **One-tap daily pull** of sales, card tips (net of refunds), declared cash tips,
  auto-gratuity, timecards, and per-server tip attribution — per venue, with per-venue
  credentials that are never mixed.
- **Provenance badges** on every field: `Square` (matches the pull), `override`
  (manager-edited, tap to revert, audit-logged with the original), `blocked` (mapping
  issue — the day cannot finalize until resolved).
- **Idempotent re-pulls** that never clobber manual overrides.
- **Multiple Square locations per venue** and **multiple Square accounts per employee**
  (hours and tips aggregate onto the person).
- **Nightly auto-sync** of the prior business day, with a configurable day-end cutoff
  (e.g. 2 AM) so late check settlements land on the night they belong to.
- Raw pull extracts stored alongside each day for reconciliation.

### Built for the people who actually use it

- **Mobile-first, dark-themed UI** — designed for a tired manager closing out on a phone
  at 12:30 AM. The Daily Review is a four-step wizard (*Confirm → Enter → Review → Lock*)
  that surfaces exactly what needs a decision and nothing else.
- **Venue picker** gates the app; the active venue is pinned in the header so a day is
  never finalized against the wrong restaurant.
- **Installable on iPhone** — "Add to Home Screen" launches TipPool full-screen with its
  own icon (web app manifest + Apple touch icon), no app store required.
- **Role-based access control**: managers handle daily entry for their venues, admins run
  setup and staff, and a super admin manages users and sees everything.
- **Plain-English warnings** — "Every declared cash tip is $0 — possible skipped
  declarations", not error codes. Warnings are individually mutable per venue; blocking
  issues never are.

### Reports that match how restaurants pay

- **Semi-monthly** (1st–15th / 16th–EOM) payroll periods for the hourly-pool and
  points models.
- **Weekly Friday–Thursday** cash tip payout *and* **monthly** payroll reports for the
  tip-out model — including per-employee **cash round-up** (payouts pre-filled to the
  next amount ending in zero, editable per period, with the total round-up tracked so
  the drawer reconciles).
- **CSV exports** with component columns (keep vs. pool share vs. returned vs. gratuity)
  so every number on the report can be traced back to its rule.
- A prominent **"Cash to pay out"** total — the exact figure to withdraw from the bank.
- **A money-source breakdown on every total** — card tips gross, declared cash tips, and
  the processing fee withheld — so a period can be checked line by line against the
  point-of-sale's own card, cash, and service-charge figures.
- **Average tip percentage** for the period (discretionary tips over net sales, with
  auto-gratuity shown separately), so service improvements can be tracked over time. If
  any day in the period is missing its sales figure the rate is withheld entirely and the
  dates are named — a short denominator overstates the rate, and a plausible wrong number
  is worse than none.
- **Print views** (browser print-to-PDF, no dependencies): a signable period summary for
  any venue; per-employee **IRS Form 4070 facsimiles** for tip-out venues — cash tips,
  card tips, tips paid out, and net tips per month, with SSN/address left blank for the
  employee to complete by hand; and **take-home stubs**, a pay-envelope slip per person
  printed 3 or 4 to a page with a cut line, for handing out with the paystub.

### Trust & audit

- Immutable, versioned day snapshots (inputs + outputs + engine version).
- A full **audit log** — every override, finalize, reopen, pull, setting change, roster
  edit, and user change, with old/new values — browsable in the app per venue.
- Managers and owners are **hard-blocked from every tip pool** (Washington State
  compliance guardrail): a day referencing an excluded person refuses to compute.
- Records retained indefinitely; there is no delete path for finalized data.

---

## Architecture

Deliberately boring, in the best way:

| Layer | Choice | Why |
|---|---|---|
| Engine | Pure Python module (`engine/`) — no I/O, no framework | Money math is testable in isolation; 46 golden days from the original workbook verify it cent-for-cent |
| Backend | FastAPI + stdlib `sqlite3` (`app/`) | Three venues, a handful of users — no ORM, no server fleet, WAL mode, per-request connections |
| Frontend | No-build vanilla JS SPA (`static/`) | One command to run, nothing to compile, trivial to containerize; assets served `no-cache` so updates apply on reload |
| Auth | Session cookies, scrypt password hashing (stdlib) | Zero crypto dependencies |
| Config | Everything in `.env` | Migrating to a host like Fly or Railway is config-only |

```
engine/     pure calculation models (POOL_HOURS, PERCENT_TIPOUT, POINTS_HOURS, clipping, labor hours)
app/        FastAPI API: days, snapshots, periods, exports, Square sync, RBAC, audit
static/     mobile-first SPA (vanilla JS, hash routing, no build step)
Tests/      516 tests: golden days, engine properties, API contracts, sync, RBAC
```

Schema migrations are versioned and applied automatically at boot (currently **v8**).
Secrets stay server-side; the Square tokens never reach the browser.

---

## Getting started

```bash
git clone https://github.com/SauloCruz/TipPool.git
cd TipPool
cp .env.example .env     # set ADMIN_EMAIL / ADMIN_PASSWORD before first boot
docker compose build
docker compose up -d
```

Docker Compose is the normal local runtime. It starts one app container backed by one
persistent SQLite volume. Open <http://127.0.0.1:8377>, sign in, pick a venue, add staff,
and enter a day.

| Command | What it does |
|---|---|
| `make docker-build` | Build the local Docker image |
| `make docker-up` | Start the app container |
| `make docker-down` | Stop the app container and keep data |
| `make docker-logs` | Follow app logs |
| `make docker-backup` | Timestamped online backup of the SQLite DB inside the Docker volume |
| `make run` | Developer fallback: run directly in a Python virtualenv |
| `make test` | Run the full test suite |
| `make backup` | Developer fallback: backup the direct local SQLite DB |

Square is optional: without credentials the app runs in manual-entry mode. To connect,
set `SQUARE_ACCESS_TOKEN` / `SQUARE_LOCATION_ID` (comma-separated for multi-location
venues) in `.env`, and the venue-suffixed variants (e.g. `SQUARE_ACCESS_TOKEN__<VENUE>`)
for additional venues. Then map categories and link team members in **Setup** — the app
will refuse to guess at anything unmapped.

---

## Docker runtime

Docker support is intentionally simple: one FastAPI/Uvicorn container and one persistent
SQLite volume. Compose runs use a named volume (`tippool-data`) and set `NIGHTLY_SYNC=0`
by default, so local restarts do not pull from Square automatically. Manual Square pulls
still work when credentials are present in `.env`.

```bash
cp .env.example .env     # if you do not already have one
# edit ADMIN_EMAIL and ADMIN_PASSWORD
docker compose build
docker compose up -d
docker compose ps
```

Open <http://127.0.0.1:8377>. The container health check calls `/healthz`, which verifies
the app can open the SQLite database.

Useful commands:

| Command | What it does |
|---|---|
| `docker compose logs -f app` | Follow app logs |
| `docker compose exec app python -m app.backup` | Create an online SQLite backup inside the mounted volume |
| `docker compose restart app` | Restart without deleting data |
| `docker compose down` | Stop the container and keep the named volume |
| `docker compose down -v` | Stop and delete the local test volume |

When importing an existing SQLite backup into the Docker volume, copy the database while
the app is stopped, then repair volume ownership before starting the app:

```bash
docker compose down
# copy the backup to /data/tippool.sqlite3 using docker compose cp or a helper container
docker compose run --rm --user root --entrypoint sh app -c \
  'chown -R app:app /data && chmod 700 /data && chmod 600 /data/tippool.sqlite3'
docker compose up -d
```

This matters because helper containers usually copy files as `root`; the TipPool
container runs as the unprivileged `app` user and SQLite must be writable by that user.

For production, run exactly one app container per SQLite volume. Enable `NIGHTLY_SYNC`
only after persistent storage, backups, and monitoring are in place.

---

## Development workflow

This repository is the source of truth at
[`SauloCruz/TipPool`](https://github.com/SauloCruz/TipPool). Every agent or developer
working on the app should use git from the start of the task:

- Pull the latest `main` before editing.
- Keep changes small, review staged files before committing, and run the relevant safe
  validation command.
- Update `README.md` and `AGENTS.md` whenever behavior, status, setup, or next steps
  change.
- Commit each completed unit of work with a clear message.
- Never commit `.env`, live SQLite data/backups, Square credentials, admin passwords,
  local AI/tool state, caches, logs, or one-off archive bundles.

---

## Status

**In production at three venues.** Daily entries, Square pulls, finalized snapshots,
weekly cash payouts, and payroll exports are live. The engine's golden-file suite
reproduces three historical pay periods — 46 days — from the original spreadsheet to the
cent, and the full suite stands at **452 passing tests**.

Historical employee data in the public test fixtures is pseudonymized.

## Roadmap

- [x] **Printable period summary** — print/save-as-PDF report with signature line
- [x] **IRS Form 4070 facsimiles** — per-employee monthly tip reports (tip-out venues)
- [x] **Payroll entry sheet** — lean per-person hours/gratuity/tips/gross with a totals row
- [x] **Take-home stubs** — a pay-envelope slip per employee, 3 or 4 to a page with a cut line
- [x] **Retire the legacy Daily Review** — removed; the stepper is the only day screen
- [x] **Local container smoke-test path** — Dockerfile, Compose, persistent test volume, health check
- ~~**Historical importer**~~ — dropped: the app went live with real data, so back-loading spreadsheet history is unnecessary
- [ ] **Hosted deployment** — the app is containerization-ready; hosting is config-only
- [x] **Role-weighted points** — points × hours, with the role taken from the clocked-in shift
- [ ] Per-shift pooling within a single day (one shift per day today)
- [ ] **Staff self-service view** — let employees look up their own take-home instead of printing stubs

## License

TipPool is licensed under the [Elastic License 2.0](LICENSE) (ELv2):

- **You may** use, copy, modify, and self-host the software — including running
  it for your own restaurant — free of charge, provided the copyright and
  license notices are preserved.
- **You may not** provide the software to third parties as a hosted or managed
  service, or resell it as a commercial offering. That right is reserved by the
  copyright holder.

Copyright © 2026 Saulo Cruz.
