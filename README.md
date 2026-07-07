# Tavern Law Tip Pool

Replaces the Excel tip-pool workbook. Milestones 1 (calculation engine),
2 (manual-entry app), and 3 (Square sync) are built; polish (M4) is next.
Full spec: [CLAUDE.md](Claude.md).

## Run it

```bash
cp .env.example .env    # edit ADMIN_EMAIL / ADMIN_PASSWORD before first boot
make run
```

That's it — one command creates the venv, installs dependencies, and starts
the server. It prints two URLs: one for this machine and one for tablets and
phones on the venue Wi-Fi (it binds `0.0.0.0` by default). Sign in with the
admin credentials from `.env`, add staff on the **Staff** screen, then enter
days on **Daily**.

- `make backup` — timestamped copy of the SQLite DB into `data/backups/`
  (safe while the app is running).
- `make test` — engine + API test suite.

## Configuration

Everything deploy-specific is in `.env` (see `.env.example`): port, host,
data directory, DB path, timezone, venue name. No hardcoded paths or hosts —
hosting this on Fly/Railway later is a config change plus a `CMD python -m
app.serve`. `ADMIN_EMAIL`/`ADMIN_PASSWORD` are read only when the user table
is empty (first boot).

## Current handoff notes

- Current schema version: `7`.
- RBAC is enforced: Super Admin can access all venues and manage users; normal
  users only see assigned venues from `user_venue_access`.
- First existing admin is promoted to Super Admin by migration v7.
- Use **Users** (`#/users`) to create users, reset passwords, enable/disable
  accounts, assign venues, and grant Super Admin. Existing users render as
  collapsible cards so the page stays compact as the roster grows.
- Use **Audit** (`#/audit`) to review changes. Super Admin can toggle all venues;
  long details wrap instead of stretching the page, with a stacked mobile layout.
- Run `make backup` before schema/auth/data-handling changes.
- GitHub publishing safety: `.gitignore` excludes `.env*` except
  `.env.example`, live SQLite data/backups, local AI/tool state, caches, logs,
  and archive bundles. Re-run a secret scan before every push.
- Latest validation after GitHub publish safety prep: `301 passed, 1 warning`.

## Layout

| Path | What |
|---|---|
| `engine/` | Pure M1 calculation engine — integer cents, exact fractions, largest-remainder rounding. No I/O. |
| `app/` | FastAPI backend: auth, days, snapshots, periods, CSV export. SQLite via stdlib `sqlite3`. |
| `static/` | Mobile-first vanilla-JS SPA (no build step — deliberate: single-command run, trivial to containerize; revisit React if the UI outgrows it). |
| `Tests/` | 300 tests: 46 golden days from the 2025 workbook, engine unit tests, API tests, Square sync, RBAC, audit log, and multi-venue regressions. |
| `data/` | SQLite DB + backups (created at runtime; not in git). |

## Rules the app enforces (see CLAUDE.md §2/§6)

- Managers/owners (`EXCLUDED` staff) are hard-blocked from every pool — days
  referencing them refuse to compute.
- Finalizing a day writes an immutable snapshot (inputs + outputs + engine
  version). Editing requires an admin reopen; re-finalizing writes the next
  version. Nothing is ever deleted.
- Auto-gratuity is a separate pool and a separate CSV column (wages, not tips).
- Negative FOH pool days pay $0 FOH and are flagged for the owner, never
  negative tips.
- CSV exports cover finalized days only.

## Square sync (M3)

Set `SQUARE_ACCESS_TOKEN`, `SQUARE_LOCATION_ID`, and `SQUARE_ENV` in `.env`
(start with `sandbox`). `SQUARE_LOCATION_ID` takes one or more comma-separated
location IDs — multiple Square locations are treated as one venue: each daily
pull covers all of them (sales, tips, gratuity, and timecards merge before
the pool is computed, so someone splitting a shift across locations gets one
combined set of hours). Then, as admin, open **Setup**:

1. **Sync categories from Square** and map each category (Food / Alcohol /
   N&A Bev / Retail / Other). Unmapped categories block a day's food sales —
   the app never guesses.
2. **Sync team from Square** and link each team member to an employee (or
   create one with the right pool role). Unknown clocked-in team members
   block hours/cash-tips for the day until linked.
3. Confirm the gratuity service-charge name match and the tippable windows.
4. Set **Business day ends at** (owner setting: 02:00) — Square pulls then
   cover the service day up to that time, so checks settled after midnight
   stay on the night they belong to. This is independent of the tippable
   window, which still hard-stops at midnight for hours purposes.

On **Daily**, "Pull from Square" fills food sales, credit tips, auto-gratuity,
declared cash tips, FOH tippable hours (clipped to the window per §2a), and
the kitchen roster. Every pulled field shows a provenance badge —
**Square** (matches the pull), **override** (manager edited; tap to revert;
change is audit-logged with the original), **blocked** (mapping issue).
Re-pulls are idempotent and never clobber overrides. A nightly job pulls the
prior day at `NIGHTLY_SYNC_HOUR` (skipping finalized days). Raw pull extracts
are stored on the day row for reconciliation.

The Square client pins API version 2025-05-21 and uses Labor
`SearchTimecards` (the Shift API is deprecated). Real-account verification
should start in the Square sandbox: create a category, an item, a team
member, a timecard with a declared cash tip, then pull a day and compare.

## Multi-venue (M5)

The app now hosts **two venues**: Tavern Law (+ Needle & Thread, one venue,
hourly tip pool) and **La Fontana Siciliana** (separate Square merchant,
percent tip-out model — server keeps 65%, 20% bussers / 10% host / 5% kitchen
of each server's own tips, pools split evenly among who worked). A venue
picker gates the app; the selected venue scopes every screen, query, and
export, and its name stays pinned in the header. Per-venue Square credentials
live in `.env` (`SQUARE_ACCESS_TOKEN__LA_FONTANA=...`); tokens are never
mixed. Owner rulings (no-host share moves to bussers, empty-pool returns,
unattributed-tips blocking) are in [docs/M5-la-fontana.md](docs/M5-la-fontana.md)
and the Claude.md decisions log. La Fontana runs a parallel-run pay period
against the manual method before becoming system of record.

## Roles

- **Manager** — daily entry, finalize days, view periods, export for assigned venues.
- **Admin** — all of that plus staff management, setup, audit log, and reopening
  finalized days for assigned venues.
- **Super Admin** — user/password setup and unrestricted access to every venue.
