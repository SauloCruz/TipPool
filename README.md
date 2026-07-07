# TipPool

**Multi-venue tip pool management for restaurants — correct to the cent, auditable to the keystroke.**

TipPool replaces error-prone spreadsheet tip pools with a small, boring, thoroughly-tested
web app. It pulls sales, tips, and timecards from Square, computes each day's tip
distribution deterministically, locks finalized days into immutable snapshots, and produces
the exact reports a restaurant needs to pay people — weekly cash payouts and
payroll-ready exports.

Built for and battle-tested at two working venues in Seattle: a bar running an
hours-proportional tip pool, and an Italian restaurant running a percentage tip-out model.
One app, one login, two completely different sets of rules.

![Python](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-backend-009688?logo=fastapi&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-storage-003B57?logo=sqlite&logoColor=white)
![Frontend](https://img.shields.io/badge/frontend-no--build%20vanilla%20JS-F7DF1E?logo=javascript&logoColor=black)

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

### Two tip models, one platform

| | Hourly pool (`POOL_HOURS`) | Percent tip-out (`PERCENT_TIPOUT`) |
|---|---|---|
| Who earns | Front-of-house pool by tippable hours; kitchen gets a % of food sales | Each server keeps 65% of their own tips; 20% / 10% / 5% tip out to bussers, host, kitchen |
| Hours | Timecards clipped to the open-to-public window (prep and after-midnight work excluded), exact to the minute | Presence-based — single shift, checkbox roster, even splits |
| Kitchen | Daily even split of the food-sales allocation | Monthly pool: the 5% accumulates and is split among a roster chosen at payroll time |
| Edge cases | Negative-pool days flagged, never paid negative | No-host nights re-route the host share to bussers; empty pools return to contributing servers — all flagged, all configurable |
| Gratuity | Separate pool, hours-proportional | Separate pool, even split among front-of-house |

Auto-gratuity (service charges) is tracked separately from tips end-to-end in both models —
different tax treatment, separate payroll line, never merged.

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
- **Role-based access control**: managers handle daily entry for their venues, admins run
  setup and staff, and a super admin manages users and sees everything.
- **Plain-English warnings** — "Every declared cash tip is $0 — possible skipped
  declarations", not error codes. Warnings are individually mutable per venue; blocking
  issues never are.

### Reports that match how restaurants pay

- **Semi-monthly** (1st–15th / 16th–EOM) payroll periods for the hourly-pool model.
- **Weekly Friday–Thursday** cash tip payout *and* **monthly** payroll reports for the
  tip-out model — including per-employee **cash round-up** (payouts pre-filled to the
  next amount ending in zero, editable per period, with the total round-up tracked so
  the drawer reconciles).
- **CSV exports** with component columns (keep vs. pool share vs. returned vs. gratuity)
  so every number on the report can be traced back to its rule.
- A prominent **"Cash to pay out"** total — the exact figure to withdraw from the bank.
- **Print views** (browser print-to-PDF, no dependencies): a signable period summary
  for any venue, and per-employee **IRS Form 4070 facsimiles** for tip-out venues —
  cash tips, card tips, tips paid out, and net tips per month, with SSN/address left
  blank for the employee to complete by hand.

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
| Backend | FastAPI + stdlib `sqlite3` (`app/`) | Two venues, a handful of users — no ORM, no server fleet, WAL mode, per-request connections |
| Frontend | No-build vanilla JS SPA (`static/`) | One command to run, nothing to compile, trivial to containerize; assets served `no-cache` so updates apply on reload |
| Auth | Session cookies, scrypt password hashing (stdlib) | Zero crypto dependencies |
| Config | Everything in `.env` | Migrating to a host like Fly or Railway is config-only |

```
engine/     pure calculation models (POOL_HOURS, PERCENT_TIPOUT, window clipping)
app/        FastAPI API: days, snapshots, periods, exports, Square sync, RBAC, audit
static/     mobile-first SPA (vanilla JS, hash routing, no build step)
Tests/      301 tests: golden days, engine properties, API contracts, sync, RBAC
```

Schema migrations are versioned and applied automatically at boot (currently **v7**).
Secrets stay server-side; the Square tokens never reach the browser.

---

## Getting started

```bash
git clone https://github.com/SauloCruz/TipPool.git
cd TipPool
cp .env.example .env     # set ADMIN_EMAIL / ADMIN_PASSWORD before first boot
make run
```

`make run` creates the virtualenv, installs dependencies, and starts the server — it
prints one URL for the local machine and one for phones/tablets on the same Wi-Fi.
Sign in, pick a venue, add staff, and enter a day.

| Command | What it does |
|---|---|
| `make run` | Start the app (binds `0.0.0.0`, LAN-visible) |
| `make test` | Run the full test suite |
| `make backup` | Timestamped online backup of the SQLite DB |

Square is optional: without credentials the app runs in manual-entry mode. To connect,
set `SQUARE_ACCESS_TOKEN` / `SQUARE_LOCATION_ID` (comma-separated for multi-location
venues) in `.env`, and the venue-suffixed variants (e.g. `SQUARE_ACCESS_TOKEN__<VENUE>`)
for additional venues. Then map categories and link team members in **Setup** — the app
will refuse to guess at anything unmapped.

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

**In production at two venues.** Daily entries, Square pulls, finalized snapshots, weekly
cash payouts, and monthly payroll exports are live. The engine's golden-file suite
reproduces three historical pay periods from the original spreadsheet to the cent, and
the full suite stands at **301 passing tests**.

Historical employee data in the public test fixtures is pseudonymized.

## Roadmap

- [x] **Printable period summary** — print/save-as-PDF report with signature line
- [x] **IRS Form 4070 facsimiles** — per-employee monthly tip reports (tip-out venues)
- [ ] **Historical importer** — load past spreadsheet periods as finalized history (under evaluation)
- [ ] **Retire the legacy Daily Review** once the stepper has run a full pay period
- [ ] **Hosted deployment** — the app is containerization-ready; hosting is config-only
- [ ] Per-shift pooling and role-weighted points (modeled for, not built)

## License

No license is granted. This repository is published for reference; all rights reserved.
