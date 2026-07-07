# Tavern Law Tip Pool App — Project Brief for Claude Code

> Paste this file into a new repo as `CLAUDE.md` (or feed it to Codex as the project prompt).
> It fully specifies the business logic, Square integration, and acceptance criteria.

---

## 1. What we're building

A web app that replaces the Excel "Payroll Tip Pool" workbook used at **Tavern Law**
(Seattle bar/gastropub) to calculate daily tip distribution for FOH and BOH staff,
aggregated per semi-monthly pay period (1st–15th and 16th–EOM).

Today a manager enters ~7 data points per day into a protected spreadsheet. The app
should auto-pull most of those from the **Square API**, let the manager enter/confirm
the rest, compute payouts deterministically, and export a payroll-ready summary.

**Users:** 1–3 managers + owner. Single venue, single Square location. Low volume.
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
5. All FOH roles weigh equally per hour (servers, bartenders, support, door). No
   role weighting in v1, but model it so weights could be added later.
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
     tippable window (§2a), for non-manager FOH jobs. Hours are exact within the
     window (owner ruling 2026-07-05): never round clock times; minutes/60
     rounded to 2 decimals for display (increment configurable, default 0.01 —
     no quarter-hour rounding).
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
- Auth: simple email+password or magic link, 2 roles: **Manager** (daily entry) and
  **Owner/Admin** (settings, mappings, exports, edit history).
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
5. **Audit Log:** all overrides and recomputes.

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
4. **M4 — Polish:** audit log, PDF summary, historical Excel import, role-based auth.

Ship M2 to real use before building M3 — it already beats the spreadsheet.

---

## 8. Non-goals (v1)

- Multi-venue support (Needle & Thread runs a different model — design the schema
  with a `venue_id` so it can be added, but build nothing for it).
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
| Hours rounding (2026-07-05) | Tippable-window clipping stands, but hours are exact within the window like Square's display: clock times never rounded, minutes/60 rounded to 2 decimals (increment 0.01). No quarter-hour rounding. |
| Venue model (M5) | TL+NT = one venue. La Fontana = separate venue, separate Square merchant, PERCENT_TIPOUT model. |
| LF percentages (M5) | Server keeps 65%; 20% bussers, 10% host, 5% BOH — of each server's OWN tips. Configurable with effective dates; must sum to 100%. |
| LF pool splits (M5) | **EVEN SPLIT** among role members who worked that day (busser, host, BOH pools). Hours-proportional toggle exists but ships OFF. |
| LF no host worked (updated 2026-07-06) | Host share goes **entirely to the busser pool** — an extra busser covers host duties on no-host nights. Effective 65 server / 30 busser / 5 BOH. Day flagged. (Supersedes the 75/20/5 re-split in docs/M5-la-fontana.md §3.) |
| LF no bussers / no BOH (M5) | Pool returns pro-rata to contributing servers; day flagged. No re-split defined — do not invent. |
| LF granularity (M5) | One shift per day at LF → per business day in v1. Per-shift pooling reserved for a future revision. |
| LF roles (M5) | **Fixed per person** (servers always servers, etc.), set on the employee mapping screen. Per-job/per-day roles out of v1 scope; mismatch = warning, assigned role wins. |
| LF cash tips (M5) | Declaration policy imminent; pipeline built now, zeros until staff start declaring. |
| RBAC (M5) | Deferred; schema (`user_venue_access`) added now, unenforced. |
| LF report periods (2026-07-06) | Two schemes: **weekly Friday–Thursday** (tip payout report; tips paid in cash every Friday) and **monthly 1st–EOM** (populates payroll). Semi-monthly does not apply to LF. |
| LF hours (2026-07-06) | Not tracked in the UI — single shift, so day membership is a **worked checkbox** per person (like TL's BOH roster). Pools AND auto-gratuity split **evenly** among each role's workers (gratuity was hours-proportional; superseded). Square-pulled hours are stored but unreported; Hours column dropped from LF exports. |
| LF BOH pool (2026-07-06) | Kitchen is NOT tracked or paid daily. The 5% slice accumulates all month and is split **evenly** among a kitchen roster chosen on the **monthly export screen** (pre-populated from who worked that month, persisted per month, audit-logged). BOH pool never returns to servers. Daily payouts = tips − carried BOH slice. |
| LF cash round-up (updated 2026-07-06) | Cash payouts are decided **per employee, per period, on the export screen** — pre-filled to the next amount ending in zero (ceil to $10: 507.39 → 510), editable, persisted per period, total round-up tracked. Applies to the weekly FOH cash report and the monthly kitchen cash payout; payroll (FOH monthly) rows stay exact. Supersedes the per-employee Staff-screen increment. |
| LF no-host flag threshold (2026-07-06) | The no-host re-split itself is routine (low season runs with fewer bussers) and shows only as a reminder. A day is FLAGGED only when no host worked AND fewer than N bussers worked — N configurable in Setup (`lf_no_host_min_bussers`, default 3). |
| Export footer note (2026-07-06) | The "review with bookkeeper (CBS)" footer is removed from all exports/screens per owner request. (Supersedes §6.) |
| LF salaried BOH (2026-07-06) | Kitchen staff flagged **always in pool** (chef Elpidio Torralba — salaried, never clocks in) are pre-selected on the monthly kitchen roster regardless of timecards. Stored rosters are never silently changed by the flag. |
