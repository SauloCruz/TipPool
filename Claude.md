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
| Hours rounding (2026-07-05) | ~~Hours exact within the window: minutes/60 to 2 decimals, increment 0.01.~~ **SUPERSEDED 2026-07-29** — see the round-up-to-0.05 row below. |
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
| Poquitos fee base (updated 2026-08-14) | The processing fee applies to **everything the card processor handled — credit tips AND auto-gratuity**. **Only CASH is exempt.** Each pool bears its own share, rounded once to the cent, and the gratuity is distributed NET. Supersedes the 2026-08-13 card-tips-only rule. This closed the last gap with the venue's previous tip-pool service: 2026-08-05 now reconciles at $1,052.42 exactly. |
| Refunded service charges (2026-08-14) | Auto-gratuity is now netted for REFUNDS, using the same split rule as tips: a refund eats the non-charge part of the check first, so a **fully refunded check returns its whole gratuity** and a small partial refund returns none. Applies to ALL venues. Found because one fully refunded check ($366.80) carried $55.30 of gratuity that we were still distributing; netting it reproduces Square's 'Net Service Charges' exactly. |
| Money-source breakdown on reports (2026-08-14) | Every Poquitos total now shows its components — card tips gross, cash tips, and the processing fee withheld — on the period tiles, the printable summary and the CSV, so a period can be checked line-by-line against Square's own card / cash / service-charge figures. |
| Excluded staff contribute but never earn (2026-08-15) | Owner: *"my hours don't count, but any tip I capture goes into the pool as I cannot retain them."* So a pool-EXCLUDED person may work an EXCLUDED-side job (Owner, Shift manager, Training) **without breaking the day** — that shift earns nothing by construction. Their declared cash tips and any card tips they ring **are still pooled**. The hard block still fires if an excluded person works an EARNING role. NOTE Poquitos differs from Tavern Law here, where manager timecards are ignored outright including their declared tips — do not harmonise them. |
| Average tip rate (2026-08-15) | Period reports show **discretionary tips (card gross + cash) ÷ net sales**, so the owner can track whether tipping improves as service does. Auto-gratuity is EXCLUDED from the headline (it is contractual, not a service signal) and shown as a second line including it. Net sales = order totals minus tax, tip and service charge — pulled from the orders we already fetch and reproduces Square's own 'Total Sales' exactly. Reporting only: `net_sales_cents` never enters the payout math. No sales pulled = no rate shown, never a misleading 0%. |
| Tip rate refuses partial data (2026-08-15) | If ANY day in the period has tips but no net sales (finalized before sales capture existed), the rate is **not shown at all** — the tile and report say which dates need a re-pull. A short denominator overstates the rate (a live period read 18.11% instead of 16.97%), and a plausible wrong number is worse than none for a metric being trended. |
| Poquitos hours rounding (2026-08-14) | Poquitos keeps hours **as Square reports them, 2 decimals, nearest** — NOT Tavern Law's round-up-to-0.05. Rounding up was inflating hours (~+0.26 h/day) and was the main reason Poquitos figures drifted from the venue's previous tip-pool service. `rounding_increment` stays a Tavern Law rule; `extract_timecards_poq` ignores it. |
| Poquitos daily view columns (2026-08-14) | The distribution table shows **Tips, Grat, (Event), Total** plus a totals row, because tips/gratuity/event are three pools but take-home is the sum — and that sum is what another system reports as a single figure. |
| Take-home column on exports (2026-08-15) | Every Poquitos period surface — period view, Export screen, print summary and payroll CSV — carries a **Take Home / Total** column (tips + event + auto-gratuity) alongside the separate pools. Payroll still needs tips and gratuity on distinct lines (different tax treatment), so the columns stay; the total is for the manager checking one number per person. Computed by one shared `takeHome()` helper in the frontend so the screens cannot drift. |
| Take-home stubs (2026-08-15) | The export screen prints a **pay-envelope slip per employee** (`#/print-stubs`), 3 or 4 to a page with a dashed cut line, to hand out with the paystub. All three venues; rows differ by model (TL/POQ pooled tips, POQ event, LF weekly cash round-up) and the total is always the sum of the rows printed above it. Built off the same `/export` payload the report renders, so a slip can never disagree with the report beside it. A staff-facing app to check take-home is BACKLOG, not v1. |
| Declared-cash breakdown (2026-08-16) | The Poquitos day screen lists **who declared** the cash under the Cash tips field — name, Square job, amount, and a "pooled, earns nothing" tag on EXCLUDED-side jobs. Square's own labor dashboard counts a manager's HOURS but drops the manager's declared cash from its declared-cash tile (2026-08-15: their $40 vs our $152, the gap being a Shift manager's $112), so the totals disagree with no way to see why from the outside. Each shift now carries `declared_cents`; `square_payload` exposes only that slice of `raw`. Days pulled before this change show nothing until re-pulled. |
| Hours on the clock (2026-08-16) | Poquitos period reports show **worked hours (every timecard)** beside **credited hours (earned a pool share)** and the non-earning remainder, because an EXCLUDED job works real hours and earns nothing, so the per-person Hours column can never tie to Square's paid-hours figure. Summed from the day's stored `shifts`, not the payout rows — an excluded person never reaches a payout. Reporting only; hours never re-enter the payout math. **Overtime is NOT reported:** SearchTimecards carries no OT field and no workweek start day reproduces Square's figure (their 16.80 for Aug 1–15 vs our best 16.60 on a Sunday week), so a computed OT number would disagree with the dashboard it is meant to reconcile against. |
| Overtime + paid hours (2026-08-16) | Poquitos period reports carry **paid hours = regular + overtime** beside **tip-credited hours**, reconciling exactly against the point-of-sale (Aug 1–15: 1597.08 / 1580.28 / 16.80). Two non-obvious rules, both verified against live data and pinned in `engine/labor_hours.py`: a shift is **split at local midnight** (a 00:06 clock-out puts 0.10 h on the next calendar day, unlike the tip pool which credits the whole shift to the business day), and **overtime is weekly**, accruing on the shift that carries the running weekly total past the threshold — so the week straddling the period start must be loaded whole. `poq_workweek_start` (default SUN) and `poq_overtime_after` (default 40) are Setup-configurable and must mirror the venue's payroll settings; WA has no daily-overtime rule. REPORTING ONLY — never enters a payout. Needs clock times, so days pulled before this change read "needs re-pull" rather than showing a short total. |
| Payroll sheet (2026-08-16) | The printable summary is now the **payroll sheet**: Points and Days dropped (pool mechanics, useless at payroll), replaced by **Reg hrs / OT hrs / Wages / Tips / Event / Gratuity / Gross pay**. Hours are PAID hours (split at midnight), never tippable hours. Wages = hours rounded to 2dp **then** multiplied by the job's Square rate, overtime as a half-time premium, result rounded **UP** to the cent — reverse-engineered from the 2026-08-01..15 pay run, where half-up was $0.01 light (53.67 x 21.30 = 1143.171 paid as 1143.18). Gross = wages + tips + event + gratuity; reproduces Square's gross exactly for Abel 1153.22 / Alexander 2376.99 / Angel 1455.23. Square's "Additional pay" = our auto-gratuity, "Paycheck tips" = our tips. CROSS-CHECK ONLY — Square Payroll computes what is actually paid. Needs clock times, so pre-2026-08-16 days show "—" until re-pulled. |
| Payroll entry sheet (2026-08-16) | Separate lean print view `#/print-payroll`, built for typing into the payroll form: **Employee · Reg hrs · OT hrs · Gratuity · Tips · Gross pay** plus a totals row to check the entry against. Gratuity sits BEFORE tips (it is the form's additional-pay field); **event money rides in the Tips column** because the venue pays it as tips. Lists **everyone who worked, including jobs that earn no tip share** — a manager takes no tip but is still owed wages, and omitting them under-pays a real person (this was a bug in the first payroll sheet: the venue's Shift manager, 80.93 h, was missing). Including them makes the overtime total reconcile exactly. The fuller tip-distribution summary stays on its own route. |
| Labor backfill (2026-08-16) | `POST /api/periods/{anchor}/refresh-labor` (admin, POINTS_HOURS only) re-fetches a period's timecards and writes **only** the extracted shifts back onto each day's stored pull. `inputs_json` and day snapshots are untouched, so **no finalized payout can move** — which is why it is allowed to run on finalized days, unlike a normal re-pull (that needs a reopen and recomputes payouts from whatever Square says today, which can shift a locked figure if a timecard was edited since). Days never pulled are skipped, not invented; every day is audit-logged as `labor_refreshed`. Surfaced on the Export screen only when `hours_unknown_dates` is non-empty. |
| Blended overtime rate (2026-08-16) | Overtime is a half-time premium on the **regular rate of that workweek** (straight-time ÷ hours worked, per week) — NOT a period-wide average, which was the first implementation and was wrong for anyone holding two jobs at different rates. Affects only staff with BOTH multiple rates AND overtime (2 of 31 in 2026-08-01..15: a bartender/shift-lead at $25/$32 and one other). The fix roughly halves the residual (+$0.73 → +$0.48, +$2.70 → +$1.80) but does NOT close it: how the payroll engine blends the rate is undocumented and could not be reproduced. Those rows are marked **†** on the payroll sheet with a footnote saying to take their gross from payroll; every single-rate row is exact. |
| Payroll sheet lists everyone (2026-08-16) | The payroll entry sheet lists **every active employee**, including people with nothing that period and salaried staff who never clock in (Poquitos has one, Eduardo Solis), because the sheet is read line for line against the payroll form and a missing name is how pay gets typed onto the wrong person. Rows with no timecards are marked **‡**, greyed, and show **em dashes rather than zeros** for hours and gross — we have no wages for them and must not imply their gross is $0; a salaried figure comes from payroll. Totals row reads "N listed, M with hours". |
| Salaried staff (2026-08-16) | Salaried employees never clock in, so no timecard-driven report can see them. The team sync now also caches each member's Square **wage setting** (`square_wage_settings`; one `/v2/team-members/{id}/wage-setting` call per member — Square has no bulk endpoint — and a failure on one member never loses the sync). A SALARY pay type is converted the way Square converts it: **weekly_hours × 52 ÷ periods-per-year** standard hours at the equivalent hourly rate, rounded up to the cent. Verified against the live 2026-08-01..15 run: Kitchen Manager on $91,000/yr, 40 h/wk, $43.75/h → **86.67 h, $3,791.82**, exact. Marked **§** on the payroll sheet, because those hours are standard hours, not hours worked. Poquitos has exactly one such employee. |
| Staff not on payroll (2026-08-16) | Schema **v8** adds `employee.in_payroll` (default 1, so every existing person is unaffected). The point-of-sale allows a staff account that is not a payroll employee — an admin login, a contractor — and deactivating them is the wrong tool, since they have not left. Toggled per person on the Staff screen ("on payroll" / "not on payroll", distinct from Deactivate); the only effect is that they are dropped from the **payroll entry sheet**. Tip reports are unaffected. If someone marked off payroll nonetheless has hours or earnings, the sheet **names them in a footnote** (`totals.off_payroll_with_pay`) rather than dropping the row silently and losing real money. |
| Square employment status (2026-08-16) | The team sync now also fetches **INACTIVE** team members and **deactivates** any employee whose every linked Square account has gone inactive — Square is the record of who still works here, and otherwise a leaver lingers on the payroll sheet forever. Audit-logged with the reason. **Asymmetric on purpose:** an employee inactive here but still active in Square is only REPORTED, never silently reactivated, because a local deactivation may be deliberate. Someone with two linked accounts needs both gone. Deactivating never erases them from a period they worked — the payroll sheet keeps anyone with hours or earnings regardless of status, so a mid-period leaver is still paid. |
| Straight time uses the REPORTED hours (2026-08-16) | Regular and overtime hours are rounded **separately** before pricing, because those are the two figures the payroll form shows and multiplies. Rounding the combined total instead lands a hundredth of an hour away: 63.1333 reg + 3.9333 OT reports as 63.13 + 3.93 = 67.06 h, but 67.0667 rounds to 67.07 — $0.21 at $21.30/h. Found via Edilberto Pacheco, whose sheet printed 63.13/3.93 yet priced 67.07. Single-rate staff with overtime are the affected case; multi-rate rows are unchanged. |
| Square pre-fills "Tips already paid" (2026-08-16) | Square Payroll auto-fills that column from each person's **declared cash tips** (verified: Scott Yeager declared $27.78, column read $27.78). At Poquitos cash tips are POOLED and redistributed, so the declarer is not entitled to them — Vianeey Palalia declared $411 as Shift manager and earns nothing from the pool. **The column must be cleared every run**, or gross pay overstates by the declared amount and pays tips to people the rules exclude. Our figures were correct; the gap was data entry. Not modelled in the app — the payroll sheet reports only what the pool actually owes. |
| Poquitos event identification (2026-08-28) | A private event is whatever the shared **"Event Host" Square logon** rang — nothing else. Its orders' 20% service charge becomes the EVENT pool instead of the day's auto-gratuity, and its card tips become event tips. Setup field `poq_event_logon_tmid`; blank = no detection, events typed by hand. **`PDR` (private dining room) tickets are NOT events** — regular servers ring them under their own logon and their 20% charge is ordinary large-party gratuity (230 such orders in Jul–Aug). Found because 2026-08-17's $204.00 event charge was being paid to the daily pool while the event's own servers earned nothing. |
| Poquitos "Event Host" account (2026-08-28) | The pin is a **till, not a person**. Its timecards are ignored entirely (`ignore_tmids`), so its clocked hours never take a share — on 2026-08-17 it was clocked in as an Event Server for 6.75 h. Its orders still define the event. |
| Poquitos event window (2026-08-28) | Inferred from the event ticket: **top of the hour before the first order was opened, through the moment it was paid**. 2026-08-17 = 15:00–17:34, which brackets the real Event Server's 14:02–17:42 shift. Shown on the day screen. |
| Poquitos event bartender (2026-08-28) | There is no `Event Bartender` job in Square, so the bartender on duty covers the event on an ordinary Bartender clock-in. **They earn from BOTH pools**: the hours overlapping the event window move to the event's service pool, the rest of the shift stays in the daily pool. One bartender overlapping → drafted automatically; **more than one → the app asks which**, never guesses. Hours are editable on the day screen. |
| Poquitos event processing fee (2026-08-28) | The card fee applies to the event pool **only on the card-handled part**, before the 80/20 split — same rule as an ordinary day. Events here are often invoiced (2026-08-17 tendered EXTERNAL), where a blanket fee would be plain wrong, so the fee base is pulled per tender rather than assumed. |
| Poquitos special events (2026-08-28) | Cinco de Mayo, Block Party and the like follow the **ordinary daily policy — no special treatment**. Closes the open §2 question; nothing to build. |
| Poquitos take-out catering (2026-08-28) | 20% service charge splits **40% Chef / 40% Manager / 20% house**. NOT BUILT — it pays two pool-EXCLUDED people on a line no pool touches, and how a catering order is told apart in Square is still unknown. Do not invent either half. |
| Poquitos event distribution tables (2026-08-28) | The day screen shows the daily pool and the event pool as **separate tables** — they are separate distributions with different members, and an event server was appearing in the daily table as a row of zeroes. Someone who worked both (the drafted bartender) appears in both. A third **Take home tonight** block lists every person once with tips + gratuity + event, which is the figure handed out and the one the stubs and CSV carry. Only the daily table shows on a day with no event. |
| Flags are reviewable (2026-08-28) | A flag asks for a decision, it does not mean the day is wrong — so the day screen now names each flag in plain English (`POQ_FLAG_TEXT`) and offers **Mark reviewed**, which clears the ⚠ on the period screen. Stored per day in `day.acked_flags_json` as the **flag NAMES**, not a boolean: a flag appearing later is unreviewed and raises the mark again. Undoable, audited as `day.ack_flags`. Distinct from `muted_warnings`, which hides a Square-pull issue code venue-wide. |
| Period tiles fill their row (2026-08-28) | `.pools` is flex, not a 4-column grid, so a short last row stretches to fill the width instead of leaving a hole — the tile count varies by tip model. Poquitos gained an **Event tips** tile beside Auto-gratuity (both are money outside the tip pool); $0.00 simply means no event ran. |
| House service charges (2026-08-29) | A charge the house keeps is named in the `house_service_charges` setting (default `["administrative fee"]`) and that test **outranks every other**, including Square's `AUTO_GRATUITY` type. Square applies that type whenever a charge is flagged as gratuity in the dashboard — a box ticked by whoever created it — so Poquitos's new 3% **"Event Administrative Fee"** would otherwise have been pooled and paid to staff. Naming house charges explicitly is the only test that does not depend on how someone configured Square. Applies to the daily gratuity too: a catering admin fee can land on an ordinary ticket. Held-out charges are always REPORTED on the day — a recognised house charge as its own routine line, an unrecognised one as a warning to chase — because silently dropping money hides it just as badly as pooling it. |
| Re-pull is one action, and says what moved (2026-08-29) | Reopen -> pull -> finalize is now `POST /api/days/{date}/refresh`, and `POST /api/periods/{anchor}/refresh` runs it across a period. Every day comes back with one of four outcomes and one bad day never aborts the run: **refreshed** (relocked; `moved` names whose payout changed), **skipped** (not finalized, or hand-entered with no Square pull behind it — untouched, and the period button counts only what will actually run), **failed** (the fetch runs BEFORE anything is written, so the day is still finalized and unchanged), **left_open** (re-pulled but something blocks finalizing — the day is now a draft holding the new data, reported loudly because it is the only outcome that changes state without succeeding). The per-person diff is the point: re-pulling a locked day genuinely can move money, and the old manual path showed nothing. Confirm prompts became a per-user preference (schema v10, `user.prefs_json`) — neither finalize nor reopen destroys anything, so an admin re-running a stretch can turn them off without weakening a guardrail. |
| Orders belong to the day they were OPENED (2026-08-29) | Order search now filters and buckets on **`created_at`, not `closed_at`** — for every venue. Tavern Law closes its tabs in a batch the following afternoon (8/18's tickets closed 08-19 13:07), so `closed_at` moved **$525 of food sales from 8/18 onto 8/19**; keying on `created_at` reproduces the venue's own Sales-by-Category report exactly on all six audited days. Poquitos is barely affected (11 orders, $14.92 in August) but **La Fontana is: 338 of 1,400 August orders change day ($68.9k of tickets)**, so its gratuity attribution was wrong too. Finalized days keep their stored payouts — only new pulls and deliberate re-pulls move. Payments and timecards are unaffected (a payment is captured on the night; a timecard carries its own clock). |
| Tavern Law pool role comes from the JOB (2026-08-29) | TL now reads `wage.title` off each timecard the way Poquitos does, instead of the per-person `employee.pool_role`, because staff hold two Square jobs: **Bartender 1.0, Server 1.0, Host 0.5 (`tl_door_weight`), Kitchen Staff BOH, and Bar Manager / Manager / Bussines Accountant / Owner EXCLUDED.** Verified on live timecards: Jacob Ruley filed 6 `Bar Manager` and 5 `Bartender` shifts in 8/16–8/28, and the spreadsheet pays him for the Bartender nights only. Square marks Host, Kitchen Staff **and Owner** `is_tip_eligible=True` — that flag is meaningless here and is never seeded from, same ruling as Poquitos. The manual Door toggle survives as an override for a shift clocked in under the wrong job. An unmapped job title blocks the day. |
| TL manager cash is pooled (2026-08-29) | Cash tips = Σ `declared_cash_tip_money` across **ALL** timecards including EXCLUDED-side jobs: an excluded person's hours earn nothing, but tips they collected cannot be kept and go into the pool. **Supersedes the earlier TL rule** that dropped a manager's timecard whole, declared cash included; TL and Poquitos now agree. The day screen lists who declared, as at Poquitos. |
| TL event money is pulled, not typed (2026-08-29) | Three separate extractions, all from items in the `Events & Catering` reporting category: (1) **Food sales excludes every `Event *` line** — 7/11 pulled $3,370 gross and the venue's own entry was $910, the $2,460 difference being Event Beverage/Food/Room/Taxes. (2) **Event food sales = Σ every `Event Food Packages` line that day** — 8/22 = 440 + 75 = $515, matching the sheet; note 8/07 was entered as $440 and should have been $500, a second $60 line having been missed by hand. (3) **Event tips = the attached deposit + any tip or gratuity service charge on a ticket carrying an Event line** — 8/22 = $569.24 deposit + $55.80 charge = $625.04. Owner ruling: anything Event *Food* rolls into that day's Event Food, any other event tip money rolls into Event Tips. |
| Event deposits are attached, never guessed (2026-08-29) | An `Event Deposit` / `Event Deposit (Gratuity)` item is rung **days or weeks before** the event ($569.24 on 8/15 for the 8/22 event, receipt #3p1b) and only one of the last eight carries a note naming the date, so no parser can be trusted. The event day screen lists every **unassigned** deposit from the trailing window — date, amount, note, receipt # — and the manager attaches one by hand; a note that does parse to that date is merely pre-selected. A deposit can never be attached to two days. Deposits also do not pair 1:1 with events (August alone has an $800 deposit with no event yet), which is exactly why the list is manual. |
| Door staff who never clock in (2026-08-29) | The day screen can add an FOH row for someone with **no Square timecard** — "Ray" worked the 8/21 door and appears in the sheet at 2.125 h with no labor record at all. Enter the **raw** hours; the app applies `tl_door_weight` itself, so the manager types 4.25 and the pool credits 2.125. Hand-entered rows are badged and audit-logged, never overwritten by a re-pull. |
