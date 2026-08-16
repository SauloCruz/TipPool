# Tavern Law Tip Pool App — Project Brief for Codex

> Paste this file into a new repo as `AGENTS.md` (or feed it to Codex as the project prompt).
> It fully specifies the business logic, Square integration, and acceptance criteria.

## 0. Current implementation status / handoff notes (updated 2026-07-07)

This repo is no longer a greenfield M1/M2 build. It is a release-candidate app
with:

- **M1/M2/M3 built:** core engine, manual-entry app, CSV export, Square sync,
  category/team mapping, override preservation, snapshots, and nightly sync.
- **M5 built:** multi-venue support for Tavern Law (+ Needle & Thread as one
  venue) and La Fontana Siciliana (`PERCENT_TIPOUT` model).
- **RBAC now enforced:** schema version is `7`; `user.super_admin` exists;
  `user_venue_access` is enforced. Super Admin sees all venues and manages
  users/passwords. Non-super users only see/edit assigned venues. The first
  existing admin is promoted to Super Admin during migration v7 so the owner
  is not locked out.
- **Admin UI:** `#/users` lets Super Admin create users, reset passwords,
  enable/disable accounts, assign venue access, and grant Super Admin. Existing
  users render as collapsed cards by default with a dropdown handle; keep this
  compact pattern as users are added.
- **Audit UI:** `#/audit` shows audit log entries; venue admins see their
  venue, Super Admin can toggle all venues. Long detail payloads are
  pretty-printed and word-wrapped, with a stacked layout on small screens.
  API: `GET /api/audit-log`.
- **Frontend:** no-build vanilla JS in `static/app.js`; do not introduce a
  build step casually. The Daily screen is the stepper only — the classic
  `#/day-classic` fallback was retired 2026-07-07 (owner). Do not reintroduce it.
- **Print views (2026-07-07):** `#/print-summary/{anchor}` (signable period
  report, both venues — closes the M4 PDF summary via browser print-to-PDF)
  and `#/print-4070/{anchor}` (IRS Form 4070 facsimile per employee, LF
  monthly only; data from `GET /api/periods/{anchor}/form4070`). SSN/address
  intentionally never stored — blank for hand-fill. Gratuity excluded from
  the 4070 (wages, not tips). Buttons live on the Export screen.
- **Container runtime (2026-07-07):** owner decision: Docker Compose is now
  the normal local runtime. Use the active clone at
  `/Users/saulocruz/Projects/TipPool`, not the stale iCloud working copy.
  Docker support is one app container plus one persistent SQLite volume. Files:
  `Dockerfile`, `.dockerignore`, `docker-compose.yml`; health endpoint:
  `GET /healthz` opens SQLite and returns schema version. Compose uses named
  volume `tippool-data` and forces `NIGHTLY_SYNC=0`; manual Square pulls still
  work, but nightly auto-pull should only be enabled after backup/monitoring is
  confirmed.
  Imported SQLite backups may arrive in the volume as `root`; repair with
  `chown -R app:app /data && chmod 700 /data && chmod 600 /data/tippool.sqlite3`
  before startup or SQLite will fail with "unable to open database file".
  Run exactly one app container per SQLite volume; do not scale replicas while
  the in-process nightly sync loop exists.
- **Host/door half credit (2026-07-29):** POOL_HOURS role weighting is now
  LIVE. Day inputs carry `door_worked: [employee_id]` (per day, not a fixed
  role — staff work dual roles); `compute_outputs` turns it into
  `foh_role_weights` at `tl_door_weight` (default 0.5) and the engine applies
  weighted hours to the tip pool AND auto-gratuity. Outputs gained
  `door_weight`, per-person `door`/`weighted_hours`, and
  `totals.total_weighted_hours`; the CSV gained a "Credited Hours" column.
  NOTE `tips_per_hour` is now the rate per WEIGHTED hour (was raw hours — that
  reported a rate nobody was paid once a weight existed). Engine 1.2.0.
- **Hours round UP to 0.05 (2026-07-29):** `rounding_increment` is now "0.05"
  and `engine.round_hours_up` rounds with ROUND_CEILING, never to nearest
  (0.78 -> 0.80). Applied in `clip_timecard` for Square pulls AND in
  `put_day` for hand-typed hours, so both paths store identical values; the
  day screen mirrors the rule via `computed.hours_increment`. Supersedes the
  2026-07-05 exact-minutes ruling — do not restore 0.01/half-up.
- **Pulls never 500 on one bad punch (2026-07-29):** a timecard whose
  clock-out is not after its clock-in (same-minute double punch, or a
  backwards manual edit in Square) used to reach `clip_timecard` and raise,
  failing the whole day's pull with a bare "Internal Server Error" (real case:
  2026-07-25). Both extractors now report it as the `invalid_timecard` warning
  (0 hours credited, declared cash still collected) instead of raising; the LF
  path would previously have gone further and subtracted NEGATIVE hours. The
  pull endpoint also wraps unexpected exceptions into a 500 that names the
  date and the actual error, and logs the traceback.
- **M6 Poquitos STARTED (2026-08-03):** third venue, third tip model
  `POINTS_HOURS` — 80/20 FOH/BOH split, points x hours per role, role read
  from each timecard's Square job (`wage.title`). Engine + 24 tests are DONE
  (`engine/points_hours.py`); spec and open questions in `docs/M6-poquitos.md`.
  Still to do: venue row + `SQUARE_ACCESS_TOKEN__POQUITOS`, job-title -> role
  mapping screen, compute dispatch, day screen, export. The event sub-model is
  built too (compute_event_points_hours); three minor event rules remain open in
  that doc — do not invent them.
- **Tests:** `make test` / `.venv/bin/python -m pytest -q` currently passes
  **443 tests**.
- **Live-data safety:** before schema/auth/data-handling work, run
  `make backup`. Recent rollback backups were created in `data/backups/`.
  Do not mutate `data/tippool.sqlite3` casually.
- **GitHub safety:** `.gitignore` intentionally excludes `.env*` except
  `.env.example`, live SQLite data/backups, `.claude/`, `.codex/`, caches,
  logs, and archive bundles. Before pushing, inspect the staged files and run a
  secret scan; never commit venue data or real Square/admin credentials.
- **License (2026-07-07):** Elastic License 2.0 (`LICENSE`, owner decision) —
  free use/copy/modify/self-host with notices preserved; providing the software
  as a hosted/managed service or commercial offering is reserved to the owner
  (keeps a future monetization path). Never remove or weaken the license or
  copyright notices.
- **Version control workflow:** this repo lives at
  `https://github.com/SauloCruz/TipPool`. Every AI agent and developer must work
  from git, pull the latest `main` before starting when possible, keep changes
  small, update Markdown handoff notes as behavior/status changes, inspect
  staged files for secrets/live data, run safe validation, and commit each
  completed unit of work with a clear message.

When this status conflicts with older sections below (for example “RBAC
deferred” or “multi-venue non-goal”), this status block and the Owner Decisions
Log updates are the current source of truth.

---

## 1. What we're building

A web app that replaces the Excel "Payroll Tip Pool" workbook used at **Tavern Law**
(Seattle bar/gastropub) to calculate daily tip distribution for FOH and BOH staff,
aggregated per semi-monthly pay period (1st–15th and 16th–EOM).

Today a manager enters ~7 data points per day into a protected spreadsheet. The app
should auto-pull most of those from the **Square API**, let the manager enter/confirm
the rest, compute payouts deterministically, and export a payroll-ready summary.

**Users:** 1–3 managers + owner/Super Admin across two current venues. Low volume.
Prioritize correctness, auditability, and simplicity over scale.

---

## 2. Business rules (the tip pool algorithm — DO NOT deviate)

All amounts in USD. All calculations are **per day**, then summed per pay period.

### Pool membership rules (owner-confirmed)
- **All employees participate in the tip pool except managers.** Managers are
  hard-blocked from any pool (WA law + house policy). It does not matter who
  collected a payment — all credit and cash tips are pooled.
- **Tippable hours = hours worked during open-to-public business hours only**
  (typically 5:00 PM – 12:00 AM). Prep hours before open and closing work after
  midnight do NOT count toward the tip pool. The app clips each timecard to the
  tippable window automatically (see §2a).
- BOH (kitchen) staff do not share in the hourly FOH pool; they receive the
  5%/10% food-sales allocation, split evenly among BOH staff who worked that day.

### Daily inputs
| # | Field | Type | Source |
|---|-------|------|--------|
| 1 | `food_sales` | $ | Square (auto) — gross sales of items in FOOD categories, excluding alcohol |
| 2 | `event_food_sales` | $ | Manual (v1) — food sold as part of private events |
| 3 | `credit_tips` | $ | Square (auto) — sum of `tip_money` on card payments; collector identity is irrelevant (pooled) |
| 4 | `cash_tips` | $ | Square (auto) — sum of `declared_cash_tip_money` across ALL non-manager timecards for the day (employees declare cash tips at clock-out), with manual override |
| 5 | `event_tips` | $ | Manual — tips attributable to private events |
| 6 | `auto_gratuity` | $ | Square (auto) — gratuity-type service charges on orders |
| 7 | `boh_worked` | list of employee IDs | Square timecards (auto) filtered to BOH jobs, with manual override |
| 8 | `foh_hours` | map employee ID → **tippable** hours | Square timecards (auto), clipped to the tippable window, filtered to non-manager FOH jobs, with manual override/adjust |

### 2a. Tippable-hours clipping (critical logic)
```
tippable_window = [open_time, close_time]   # default 17:00–24:00, configurable
                                            # per day-of-week and per venue

for each timecard:
    worked_intervals = [clock_in, clock_out] minus unpaid breaks
    tippable_hours   = total overlap(worked_intervals, tippable_window)
```
- Example: clock-in 3:00 PM (prep), clock-out 12:40 AM → tippable = 5:00 PM–12:00 AM
  = 7.00 h. This matches the historical spreadsheet pattern (flat 7.0 entries).
- The window is a **setting with per-day-of-week values and effective dates**
  (e.g., extended weekend hours), never hardcoded.
- Show both raw hours and clipped tippable hours in the UI so managers can sanity-
  check; allow per-shift manual adjustment with audit logging.
- BOH "worked that day" = any BOH timecard that day (no window clipping needed
  for the even split; kitchen prep hours still count as having worked).

### Daily calculations
```
total_tips     = credit_tips + cash_tips + event_tips
boh_allocation = 0.05 * food_sales + 0.10 * event_food_sales
foh_pool       = total_tips - boh_allocation

boh_per_person = boh_allocation / count(boh_worked)        # even split
                 (0 if no BOH worked; then boh_allocation must be 0 or flagged)

foh_total_hours = sum(foh_hours.values())
tips_per_hour   = foh_pool / foh_total_hours               # 0 if no hours
foh_payout[e]   = tips_per_hour * foh_hours[e]

# Automatic gratuity (service charges) is a SEPARATE pool.
# OWNER DECISION (confirmed): distributed HOURS-PROPORTIONAL, same mechanics
# as the tip pool — NOT an even per-head split. FOH only; managers excluded.
# Reported separately on payroll export (service charges are wages, not tips —
# different tax treatment; never merge with the tips line).
grat_per_hour     = auto_gratuity / foh_total_hours         # 0 if no hours
grat_payout[e]    = grat_per_hour * foh_hours[e]
```

### Pay-period aggregation
- Periods: **1st–15th** and **16th–end of month**.
- Per employee: sum of daily `foh_payout` (reported as "Tips"), sum of daily
  `grat_payout` (reported separately as "Additional Payout / Auto Gratuity"),
  sum of daily `boh_per_person` for BOH staff, plus days-worked / total-hours counts.

### Rules the app must enforce (these fix known Excel bugs)
1. **BOH divisor = actual roster count.** The per-person BOH split divides by the
   number of BOH staff actually marked as worked — never a separately entered
   headcount. (The spreadsheet had both and they could disagree.)
2. **Conservation invariants (test these):**
   - `sum(foh_payout) == foh_pool` (± $0.01 rounding)
   - `sum(boh_per_person payouts) == boh_allocation` (± $0.01)
   - `sum(grat_payout) == auto_gratuity` (± $0.01)
3. **Rounding:** compute in cents (integer math or Decimal). Round individual
   payouts to cents; assign any residual cent(s) to the employee(s) with the most
   hours (deterministic largest-remainder method) so pools always balance exactly.
4. **Negative FOH pool** (BOH allocation > total tips — slow day edge case): do not
   silently pay negative tips. Flag the day for manager review and carry the shortfall
   as an explicit warning; owner decides policy.
5. FOH roles weigh equally per hour (servers, bartenders, support) **except a
   host/door shift, which earns half credit per hour** (owner ruling 2026-07-29;
   `tl_door_weight`, marked per person per day — see the decisions log). The
   engine's `foh_role_weights` map carries this and any future weighting.
6. Every computed day stores a **snapshot** of inputs + outputs (immutable audit
   record). Recomputing after an edit creates a new version; history is retained.

---

## 3. Square integration

- **APIs:** Square Web SDK / REST — Payments API, Orders API, Catalog API,
  Labor API (**SearchTimecards** — the Shift object/endpoints are deprecated;
  use Square API version 2025-05-21 or later), Team API. OAuth or a personal
  access token stored server-side; **never in client code**.
- **Location:** single location ID, configured in settings (env/config), not hardcoded.
- **Timezone:** America/Los_Angeles. A business "day" = calendar day in that TZ.
  Optionally support a configurable day-end cutoff (e.g., 3:00 AM) — build the day
  boundary as a setting, default midnight.

### Data pulls (per day)
1. **Food sales:** Search Orders for the day (state COMPLETED), expand line items,
   resolve each item's catalog category. Sum gross sales for categories mapped as
   FOOD. Admin UI must include a **category mapping screen** (each Square category →
   Food / Alcohol / N&A Bev / Retail / Other). Unmapped categories block the day's
   calc with a "map this category" prompt — never silently guess.
2. **Credit tips:** Payments API for the day, sum `tip_money` on CARD payments
   (status COMPLETED; subtract tips on refunded payments). Exclude cash-tender
   payments' tip fields.
3. **Auto gratuity:** From Orders' service charges where the charge is the venue's
   gratuity service charge (configurable by service charge catalog ID/name match).
4. **Timecards (one call, three inputs):** Labor API `SearchTimecards` for the day
   returns, per timecard: `team_member_id`, clock-in/out, breaks, `wage.tip_eligible`,
   and `declared_cash_tip_money`. From this single pull derive:
   - **FOH tippable hours** — worked intervals minus unpaid breaks, clipped to the
     tippable window (§2a), for non-manager FOH jobs. Clock times are never
     rounded; the clipped total is then rounded UP to the next `rounding_
     increment` (0.05 h — owner ruling 2026-07-29, superseding the 2026-07-05
     exact-minutes 0.01 rule).
   - **BOH worked roster** — any BOH-job timecard that day.
   - **Cash tips** — Σ `declared_cash_tip_money` across all non-manager timecards.
   Map each team member's job to FOH / BOH / Manager-excluded via an **employee &
   job mapping screen** (synced from Team API; seed defaults from Square's
   `tip_eligible` flag, with per-employee override).
   Flag for review: days where every declared cash tip is $0 (possible skipped
   declarations) and timecards missing clock-out.
5. **Manual fields:** event food sales, event tips — entered on the daily review
   screen, default 0. Cash tips is auto-filled from declarations but manually
   overridable (override logged).

### Sync behavior
- "Pull from Square" per day (idempotent re-pull allowed) + a nightly auto-sync for
  the prior day.
- Manager can **override any auto-pulled value**; overrides are visibly flagged
  (badge + original Square value shown) and logged (who/when/old/new).
- Store raw Square responses (or their relevant extracts) alongside the day for
  reconciliation/debugging.

---

## 4. App structure

**Stack (suggested — keep it boring):**
- Backend: Python + FastAPI (or Node/TypeScript + Express if preferred), SQLite
  database (single venue, low volume; use Postgres only if deployment demands it).
- Frontend: React + Tailwind, single-page app. Mobile-friendly — managers will use
  tablets/phones at close.
- Auth: email+password with 3 effective access levels: **Manager** (daily entry),
  **Admin** (venue setup/staff/audit/reopen for assigned venues), and
  **Super Admin** (user/password setup and all venues).
- Money handling: integer cents everywhere, or Python `Decimal`. Never floats.

**Screens:**
1. **Daily Review** (core screen): date picker → auto-pulled values with
   Square/manual/override badges → manual fields → live computed distribution
   (BOH allocation, FOH pool, per-person table) → "Finalize day" button.
2. **Pay Period Dashboard:** grid of days (status: not started / draft / finalized /
   flagged), running totals, per-employee period summary.
3. **Payroll Export:** per-employee totals for the period — Tips, Auto-Gratuity,
   Days/Hours — as CSV formatted for Square Payroll import, plus a printable PDF
   summary the owner can review/sign.
4. **Settings:** Square connection & location, category mapping, employee/job
   mapping (FOH / BOH / Manager-excluded), gratuity service charge selector,
   **tippable window per day-of-week** (default 17:00–24:00) with effective dates,
   day-boundary cutoff, rounding increment, BOH percentages (default 5% food /
   10% event food — configurable constants with effective dates so history isn't
   rewritten).
5. **Users:** Super Admin creates users, resets passwords, enables/disables
   accounts, assigns venue access, and grants Super Admin.
6. **Audit Log:** all overrides, recomputes, user changes, exports, roster edits,
   settings changes, and Square pulls.

---

## 5. Migration & validation

- Include a one-off importer script that reads the historical Excel workbook
  (`Payroll_Tip_Pool_-_2025.xlsx`, tabs named like `6.30.26`, layout: input rows 4–9,
  BOH allocation row 11, FOH pool row 12, kitchen Y-grid rows 15–20, FOH hours rows
  24–43, gratuity block rows 49–72, FOH payouts rows 75–95, BOH payouts rows 98–104)
  and loads past periods as finalized historical data.
- **Golden-file tests:** recompute at least 3 historical pay periods from the Excel
  inputs and assert the app's outputs match the spreadsheet's payouts within $0.02
  per employee per day (differences beyond that must be explained — e.g., the known
  spreadsheet bugs: B:P vs B:Q summation ranges, and headcount-vs-roster divisor).
- Unit tests for: conservation invariants, zero-hours day, zero-BOH day, negative
  FOH pool flag, rounding residual assignment, day-boundary/timezone handling,
  refunded-payment tip handling, and **tippable-window clipping** (clock-in before
  open, clock-out after midnight, shift entirely outside the window, unpaid break
  straddling the window boundary, DST transition days).

---

## 6. Compliance guardrails (Washington State)

Build these as assertions/warnings, not legal advice:
- BOH tip share via a mandatory tip pool is lawful in WA **only if no employer/
  manager participates**. The app must make it impossible to include salaried
  managers in any pool; the employee mapping screen needs an "excluded (manager/
  owner)" flag that hard-blocks inclusion.
- Auto-gratuity (service charges) in WA must be disclosed and paid per the stated
  disclosure; keep it tracked separately from tips end-to-end (the app already does).
- Retain daily records ≥ 3 years (never hard-delete finalized days).
- Show a footer note on exports: "Review with bookkeeper (CBS) before payroll submission."

---

## 7. Build order (milestones)

1. **M1 — Core engine + tests:** pure calculation module with the algorithm in §2,
   full unit test suite, golden-file test against Excel extracts. No UI, no Square.
2. **M2 — Manual-entry app:** DB schema, daily review screen with all-manual inputs,
   pay period dashboard, CSV export. Usable in production without Square.
3. **M3 — Square sync:** category & job mappings, per-day pull, override flow,
   nightly sync.
4. **M4 — Polish:** audit log, role-based auth, and the printable summary are
   implemented. Historical Excel import was DROPPED (owner, 2026-07-07) — the
   app went live with real data, so back-loading spreadsheet history is
   unnecessary. §5's importer spec is retained for reference only; do not
   build it.

Ship M2 to real use before building M3 — it already beats the spreadsheet.

---

## 8. Non-goals (v1)

- Additional venue models beyond Tavern Law/Needle & Thread and La Fontana.
- Scheduling, payroll tax, or wage calculations (Square Payroll owns those).
- Role-weighted tip points (model-friendly, not implemented).
- Direct write-back to Square Payroll (CSV export only in v1).
- Manual per-day tippable-window input (v2 backlog — v1 uses the configured
  per-day-of-week window with a hard midnight cutoff).

---

## 9. Owner decisions log (do not re-ask; do not deviate)

| Decision | Ruling |
|---|---|
| Tippable window cutoff | Hard midnight cutoff in v1, even on late-close nights. Per-day manual window input deferred to v2. |
| Auto-gratuity distribution | **Hours-proportional** (rate = gratuity ÷ total FOH tippable hours × individual hours). Same mechanics as tip pool. Not per-head. |
| Auto-gratuity reporting | Separate payroll line from tips (wages, not tips — distinct tax treatment). |
| Pool membership | All employees except managers. Managers hard-blocked from all pools. |
| Cash tips source | Σ `declared_cash_tip_money` from daily timecards; pooled regardless of who collected; manual override with audit log. |
| BOH allocation | 5% food sales + 10% event food sales; even split among BOH who worked that day (any timecard counts, no window clipping for roster). |
| Hours rounding (2026-07-05) | ~~Hours exact within the window: minutes/60 to 2 decimals, increment 0.01.~~ **SUPERSEDED 2026-07-29** — see the round-up-to-0.05 row below. |
| Venue model (M5) | TL+NT = one venue. La Fontana = separate venue, separate Square merchant, PERCENT_TIPOUT model. |
| LF percentages (M5) | Server keeps 65%; 20% bussers, 10% host, 5% BOH — of each server's OWN tips. Configurable with effective dates; must sum to 100%. |
| LF pool splits (M5) | **EVEN SPLIT** among role members who worked that day (busser, host, BOH pools). Hours-proportional toggle exists but ships OFF. |
| LF no host worked (updated 2026-07-06) | Host share goes **entirely to the busser pool** — an extra busser covers host duties on no-host nights. Effective 65 server / 30 busser / 5 BOH. Day flagged. (Supersedes the 75/20/5 re-split in docs/M5-la-fontana.md §3.) |
| LF no bussers / no BOH (M5) | Pool returns pro-rata to contributing servers; day flagged. No re-split defined — do not invent. |
| LF granularity (M5) | One shift per day at LF → per business day in v1. Per-shift pooling reserved for a future revision. |
| LF roles (M5) | **Fixed per person** (servers always servers, etc.), set on the employee mapping screen. Per-job/per-day roles out of v1 scope; mismatch = warning, assigned role wins. |
| LF cash tips (M5) | Declaration policy imminent; pipeline built now, zeros until staff start declaring. |
| RBAC (updated 2026-07-07) | Enforced. Super Admin sees all venues and manages users/passwords; non-super users only see/edit venues listed in `user_venue_access` (legacy home venue fallback retained). |
| LF report periods (2026-07-06) | Two schemes: **weekly Friday–Thursday** (tip payout report; tips paid in cash every Friday) and **monthly 1st–EOM** (populates payroll). Semi-monthly does not apply to LF. |
| LF hours (2026-07-06) | Not tracked in the UI — single shift, so day membership is a **worked checkbox** per person (like TL's BOH roster). Pools AND auto-gratuity split **evenly** among each role's workers (gratuity was hours-proportional; superseded). Square-pulled hours are stored but unreported; Hours column dropped from LF exports. |
| LF BOH pool (2026-07-06) | Kitchen is NOT tracked or paid daily. The 5% slice accumulates all month and is split **evenly** among a kitchen roster chosen on the **monthly export screen** (pre-populated from who worked that month, persisted per month, audit-logged). BOH pool never returns to servers. Daily payouts = tips − carried BOH slice. |
| LF cash round-up (updated 2026-07-06) | Cash payouts are decided **per employee, per period, on the export screen** — pre-filled to the next amount ending in zero (ceil to $10: 507.39 → 510), editable, persisted per period, total round-up tracked. Applies to the weekly FOH cash report and the monthly kitchen cash payout; payroll (FOH monthly) rows stay exact. Supersedes the per-employee Staff-screen increment. |
| LF no-host flag threshold (2026-07-06) | The no-host re-split itself is routine (low season runs with fewer bussers) and shows only as a reminder. A day is FLAGGED only when no host worked AND fewer than N bussers worked — N configurable in Setup (`lf_no_host_min_bussers`, default 3). |
| Export footer note (2026-07-06) | The "review with bookkeeper (CBS)" footer is removed from all exports/screens per owner request. (Supersedes §6.) |
| LF salaried BOH (2026-07-06) | Kitchen staff flagged **always in pool** (chef Elpidio Torralba — salaried, never clocks in) are pre-selected on the monthly kitchen roster regardless of timecards. Stored rosters are never silently changed by the flag. |
| Historical Excel import (2026-07-07) | **Dropped.** The app went live with real data; back-loading spreadsheet history is unnecessary. Do not build the §5 importer. Golden-file tests (already extracted) stay. |
| Legacy Daily Review (2026-07-07) | **Retired.** `#/day-classic` route, `renderDayLegacy`, and cross-links removed; the stepper is the only day screen. Do not reintroduce. |
| TL host/door shifts (2026-07-29) | A host/door shift earns **half an hour of tip credit per hour worked** (`tl_door_weight`, default 0.5, Setup-configurable 0–1). Marked **per person per day** with the Door toggle on the day screen (step 2) — NOT a fixed per-person role, because staff work dual roles (server/host, bartender/host). Applies to the tip pool **and** auto-gratuity (same weight map). Snapshots record the rate used, so changing the setting never rewrites finalized days. Split shifts (part floor, part door) are out of scope: adjust hours by hand on those rare nights. |
| Hours rounding (updated 2026-07-29) | Credited tippable hours step in **0.05 and always round UP** ("to the next 5 or 0": 0.78 → 0.80, 0.71 → 0.75; exact multiples untouched). Applies to Square-pulled AND hand-typed hours — enforced server-side on save, mirrored in the day screen so the field shows what is stored. Clock times themselves are still never rounded and window clipping is unchanged. `rounding_increment` setting = "0.05". **Supersedes the 2026-07-05 exact-to-the-minute 0.01 ruling.** |
| Poquitos venue (2026-08-03) | Third venue, third model **POINTS_HOURS** (`engine/points_hours.py`, spec `docs/M6-poquitos.md`). 100% of tips pooled, **80% FOH / 20% BOH**, each side split by **points × hours**: Bartender/Shift Lead 1.25, Server 1.0, Barback/Bar Prep/Host/Busser/Expeditor/Food Runner 0.5; all BOH roles 1.0 (so BOH is hours-proportional). **Role comes from the SHIFT, not the person** — Square's `wage.title`/`wage.job_id` records the job chosen at clock-in, so multi-role staff and split nights are credited automatically (verified on 260 live timecards). Unmapped job title blocks the day. Semi-monthly payroll, no cash payout run. |
| Poquitos gratuity (2026-08-03) | Service charges stay a **separate pool on their own payroll line** (wages, not tips — as at TL), distributed by the same 80/20 + points mechanics. Never merged into tips. |
| Poquitos Bar Manager (2026-08-03) | Policy allows Bar Managers in the pool **when working Bartender shifts**. Implemented as a narrow allowlist: an EXCLUDED person's shift is permitted only when its role is in `manager_pool_roles` (default `BARTENDER`); every other manager shift still raises ManagerInPoolError. The timecard's job decides — no manual toggle. |
| Poquitos events (2026-08-03) | Owner wants the private/special-event model BUILT, not deferred. Specifics still open — see `docs/M6-poquitos.md` §2; do not invent the missing rules. |
| Poquitos event membership (2026-08-03) | **The clock-in role decides.** Poquitos uses separate Square job titles for event work; anyone clocked in under an event service role is in that event's pool and OUT of the daily pool for those hours. Same `wage.title` mechanism as daily points — no manual marking. |
| Poquitos event support tip-out (2026-08-03) | Busser, expo/food-runner and host are tipped out **3% of the FOH portion each, per ROLE** (9% of the FOH portion total), each role's share then divided among the people in that role — NOT 3% per person. Support staff also remain in the daily pool. |
| Poquitos event math (2026-08-03) | Support 3% shares go to **everyone who worked that role that day** (not just event workers). The remaining service pool splits **points × hours** among event service staff; the event's 20% BOH portion splits **by kitchen hours that day**. The 3% admin fee is **charged in addition** and never touches the staff pool, so the engine does not model it. If a support role had nobody that day its 3% stays in the service pool and the event is flagged (assumption — confirm). |
| Poquitos job mapping (2026-08-13) | Audited the live account: 1 location `LB3CVZKR3BMND`, 17 jobs, **all with `is_tip_eligible=True` including `Owner` — that flag is meaningless here, do NOT seed from it**. Mapping confirmed in `docs/M6-poquitos.md` §2a. **EXCLUDED (earn nothing): Shift manager, Kitchen Manager, Owner, Janitorial, Staff Trainer, Training Shift.** Exclusion is a property of the JOB, not the person — a person who bartends one night and manages the next earns on the bartender hours only, and excluded hours never dilute anyone else's share. `Runner` = policy Food Runner (0.5) and fills the expo/food-runner event slot. No `Event Bartender` job exists yet — a bartender working an event would land in the DAILY pool. |
| Poquitos card processing fee (2026-08-13) | The card processor's fee is withheld from **CREDIT tips only**, before anything is pooled, so every share is of money the venue actually received. **Cash tips and auto-gratuity are never reduced.** Rate is the Setup input `poq_card_fee_pct`, **default "0" — a rate must be set deliberately, never assumed**. The rate used is stored on each snapshot, so changing it never moves a finalized day. `poq_foh_pct` (80) is editable in Setup too. |
| Poquitos fee base (updated 2026-08-14) | The processing fee applies to **everything the card processor handled — credit tips AND auto-gratuity**. **Only CASH is exempt.** Each pool bears its own share, rounded once to the cent, and the gratuity is distributed NET. Supersedes the 2026-08-13 card-tips-only rule. This closed the last gap with TipHaus: 2026-08-05 now reconciles at $1,052.42 exactly. |
| Refunded service charges (2026-08-14) | Auto-gratuity is now netted for REFUNDS, using the same split rule as tips: a refund eats the non-charge part of the check first, so a **fully refunded check returns its whole gratuity** and a small partial refund returns none. Applies to ALL venues. Found because one fully refunded check ($366.80) carried $55.30 of gratuity that we were still distributing; netting it reproduces Square's 'Net Service Charges' exactly. |
| Money-source breakdown on reports (2026-08-14) | Every Poquitos total now shows its components — card tips gross, cash tips, and the processing fee withheld — on the period tiles, the printable summary and the CSV, so a period can be checked line-by-line against Square's own card / cash / service-charge figures. |
| Excluded staff contribute but never earn (2026-08-15) | Owner: *"my hours don't count, but any tip I capture goes into the pool as I cannot retain them."* So a pool-EXCLUDED person may work an EXCLUDED-side job (Owner, Shift manager, Training) **without breaking the day** — that shift earns nothing by construction. Their declared cash tips and any card tips they ring **are still pooled**. The hard block still fires if an excluded person works an EARNING role. NOTE Poquitos differs from Tavern Law here, where manager timecards are ignored outright including their declared tips — do not harmonise them. |
| Average tip rate (2026-08-15) | Period reports show **discretionary tips (card gross + cash) ÷ net sales**, so the owner can track whether tipping improves as service does. Auto-gratuity is EXCLUDED from the headline (it is contractual, not a service signal) and shown as a second line including it. Net sales = order totals minus tax, tip and service charge — pulled from the orders we already fetch and reproduces Square's own 'Total Sales' exactly. Reporting only: `net_sales_cents` never enters the payout math. No sales pulled = no rate shown, never a misleading 0%. |
| Tip rate refuses partial data (2026-08-15) | If ANY day in the period has tips but no net sales (finalized before sales capture existed), the rate is **not shown at all** — the tile and report say which dates need a re-pull. A short denominator overstates the rate (a live period read 18.11% instead of 16.97%), and a plausible wrong number is worse than none for a metric being trended. |
| Poquitos hours rounding (2026-08-14) | Poquitos keeps hours **as Square reports them, 2 decimals, nearest** — NOT Tavern Law's round-up-to-0.05. Rounding up was inflating hours (~+0.26 h/day) and was the main reason Poquitos figures drifted from the venue's existing TipHaus numbers. `rounding_increment` stays a Tavern Law rule; `extract_timecards_poq` ignores it. |
| Poquitos daily view columns (2026-08-14) | The distribution table shows **Tips, Grat, (Event), Total** plus a totals row, because tips/gratuity/event are three pools but take-home is the sum — and that sum is what another system reports as a single figure. |
