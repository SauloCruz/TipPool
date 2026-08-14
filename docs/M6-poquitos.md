# M6 — Poquitos (`POINTS_HOURS`)

Third venue, third tip model. Source: *Poquitos Tip Pool Policy* (Salt Holdings
LLC d/b/a Poquitos, 1000 E Pike St, Seattle), EN/ES, supplied by the owner
2026-08-03. Separate Square merchant account and API key.

Poquitos is mechanically closest to Tavern Law — hours drive everything — but
adds two things neither existing venue has: a fixed **80/20 FOH/BOH split** of
the pool, and **per-hour point values that differ by role**.

---

## 1. Daily model (SPECIFIED — engine built)

```
total_tips = credit_tips + cash_tips          # 100% pooled
foh_pool   = round(total_tips * 80%)
boh_pool   = total_tips - foh_pool            # exact remainder, never re-rounded

points[e]  = Σ over that person's shifts of role_points[role] * hours
payout[e]  = side_pool * points[e] / Σ(points on that side)
```

### Point values (policy tables)

| Points / hour | Roles |
|---|---|
| **1.25** | Bartender, Shift Lead |
| **1.0** | Server |
| **0.5** | Barback, Bar Prep, Host, Busser, Expeditor, Food Runner |
| **1.0 (BOH)** | Sous Chef, Line Cook, Prep Cook, Dishwasher |

Every BOH role is 1.0, so the kitchen pool reduces to hours-proportional —
which is exactly what the policy means by "divided evenly among hours worked".

### Role comes from the SHIFT, not the person

Confirmed against live Square data (260 timecards sampled): **every** timecard
carries `wage.title` and `wage.job_id` — the job the employee selected at
clock-in. So the engine takes a list of `Shift(employee, role, hours)` rather
than a per-person hours map. Someone who bartends Friday and hosts Saturday is
credited 1.25 and 0.5 respectively, and a split night lands in both buckets
automatically. This is the mechanism that answers the owner's multi-role
concern — no nightly manual toggle (unlike TL's door checkbox).

An unmapped job title raises `UnknownRoleError` and blocks the day: never
guess a point value.

### Owner rulings 2026-08-03

| Decision | Ruling |
|---|---|
| Auto-gratuity / service charges (ordinary days) | **Separate pool, own payroll line** (wages, not tips — same treatment as TL), but distributed by the same 80/20 + points mechanics so shares keep their shape. Never merged into the tips line. |
| Bar Manager working a Bartender shift | **The timecard's job decides.** A pool-excluded person earns normally on a shift whose role is in `manager_pool_roles` (default `BARTENDER`); any other manager shift still raises `ManagerInPoolError`. Narrow, configurable allowlist — the hard block stays on for everything else. |
| Cash tips | Σ `declared_cash_tip_money` across the day's timecards, pulled from Square, manual override allowed — identical to Tavern Law. |
| Private / special events | **Build them** (not deferred). See §2 — specifics still needed. |
| Card processing fee (2026-08-13) | Withheld from **credit tips only**, before pooling — so every share is of money the venue actually received. Cash tips and auto-gratuity untouched. Setup input `poq_card_fee_pct`, default `0` (never assumed); the rate used is recorded on each snapshot. |
| Pay periods | Semi-monthly (1st–15th, 16th–EOM), same as TL. Tips are paid through payroll only — no cash payout run (unlike La Fontana). |

### Flags (non-blocking, surfaced for review)

- `no_boh_worked` — kitchen slice computed but nobody to pay. The 20% is NOT
  reassigned to FOH; the day is flagged for a decision.
- `no_foh_worked` — mirror case.
- `negative_tips`, `negative_gratuity`.

---

## 2. Events (OWNER SAID BUILD — specifics still OPEN)

Policy text, for reference:

> In the case of a private, contracted event, a 20% service charge is applied
> to all food, beverage, and other services. Private events are rung up under a
> separate number and the service charge and any tips collected are split 80-20
> between the servers and bartenders that worked the event and the kitchen.
>
> The support staff: busser, expo/food runner and host are tipped out at 3%
> each from the FOH portion. They would remain in the daily pool as well, while
> the service staff working the event would only be in the event pool.
>
> A 3% administrative fee is also added and distributed to the Manager in
> charge of organizing the event. A 20% service charge is added to take-out
> catering orders and distributed to the Chef and Manager. Should the host of
> the private event choose to leave an additional tip at the end of the event,
> this tip will be divided equally amongst the staff working the event.

### Settled (owner, 2026-08-03)

| Rule | Ruling |
|---|---|
| Support tip-out base | **3% of the FOH portion, per ROLE** — one busser share, one expo/food-runner share, one host share = 9% of the FOH portion in total. Each role's 3% is then divided among the people in that role (NOT 3% per person). |
| Who is in the event pool vs the daily pool | **The clock-in role decides.** Poquitos has separate event roles in Square; anyone clocked in under an event service role is in the event pool and **out of the daily pool** for those hours. No manual marking — the same `wage.title` mechanism that drives daily points. |

Structure now settled:

```
event_pool   = event_service_charge (20%) + event_tips
foh_portion  = 80% of event_pool
boh_portion  = event_pool - foh_portion          # exact remainder

busser_share = 3% of foh_portion   -> divided among bussers
expo_share   = 3% of foh_portion   -> divided among expo / food runners
host_share   = 3% of foh_portion   -> divided among hosts
service_pool = foh_portion - 9%    -> event servers & bartenders
```

### Settled 2026-08-03 (second round) — engine built

| Rule | Ruling |
|---|---|
| Who shares each 3% | **Everyone who worked that role that day**, whether or not they were on the event. No event support roles needed in Square. |
| `service_pool` split | **Points x hours**, same rates as daily (event bartender 1.25, event server 1.0). |
| `boh_portion` split | **By kitchen hours worked that day** — same rule as the daily 20%. |
| 3% administrative fee | **Charged in addition**, on top of the 20% service charge. It never touches the staff pool, so the engine does not model it; the full pool still splits 80/20. |

Because every role inside a support group carries the same point value
(busser/expo/host are all 0.5) and every BOH role is 1.0, "by hours" and
"points x hours" give identical results there — no ambiguity survives.

**Assumption flagged for confirmation:** if nobody worked a support role that
day, that group's 3% has nowhere to go. The engine keeps it in `service_pool`
(so it reaches the event's own staff) and raises a `no_<group>_worked` flag,
rather than letting the money vanish. This mirrors the La Fontana precedent
where an empty pool returns to the contributing staff. Say the word if it
should behave differently.

### Still OPEN

1. Take-out catering 20% service charge -> "Chef and Manager": what split?
2. Special events (Cinco de Mayo, Block Party) — same mechanics as a private
   event, or simply "everyone who worked shares the event tips equally"?
3. The extra end-of-event host gratuity is "divided equally amongst the staff
   working the event" — does that mean the event service staff only, or
   everyone including support and kitchen?

---

## 2a. Square job title -> role mapping (CONFIRMED 2026-08-13)

Poquitos Square account: **1 location, `LB3CVZKR3BMND`** ("Poquitos", 1000 E
Pike St, America/Los_Angeles, production). Credentials live in `.env` as
`SQUARE_ACCESS_TOKEN__POQUITOS` / `SQUARE_LOCATION_ID__POQUITOS`.

17 jobs are configured on the account. Audited against a month of real
timecards (172 cards, **every one carrying `wage.title`**; 5 of 30 people
worked more than one job, one of them three).

> **`is_tip_eligible` is `True` on all 17 jobs — including `Owner`.** It is
> left at Square's default and carries no meaning here. Do NOT seed pool
> membership from it (unlike the Tavern Law setup, where it was a useful hint).

| Square job title | Role | Points | Side |
|---|---|---|---|
| `Bartender` | BARTENDER | 1.25 | FOH |
| `Shift Lead` | SHIFT_LEAD | 1.25 | FOH |
| `Server` | SERVER | 1.0 | FOH |
| `Bar Prep` | BAR_PREP | 0.5 | FOH |
| `Busser` | BUSSER | 0.5 | FOH |
| `Host` | HOST | 0.5 | FOH |
| `Runner` | FOOD_RUNNER | 0.5 | FOH |
| `Line Cook` | LINE_COOK | 1.0 | BOH |
| `Prep Cook` | PREP_COOK | 1.0 | BOH |
| `Dishwasher` | DISHWASHER | 1.0 | BOH |
| `Event Server` | EVENT_SERVER | 1.0 | **EVENT** (out of the daily pool) |
| `Shift manager` | — | — | **EXCLUDED** |
| `Kitchen Manager` | — | — | **EXCLUDED** |
| `Owner` | — | — | **EXCLUDED** |
| `Janitorial` | — | — | **EXCLUDED** |
| `Staff Trainer` | — | — | **EXCLUDED** |
| `Training Shift` | — | — | **EXCLUDED** |

**Exclusion is a property of the JOB, not the person** (owner ruling
2026-08-13). Someone who bartends Tuesday and works a Shift manager shift
Wednesday earns on Tuesday's hours only — no per-person flag needed, and the
excluded hours neither earn nor dilute anyone else's share. This also covers
the policy's Bar Manager exception for free: a Bar Manager clocked in as
`Bartender` simply earns like any bartender.

### Notes and gaps

- `Runner` is taken as the policy's **Food Runner** (0.5) and fills the
  "expo/food runner" slot for the event 3% tip-out; there is no separate
  `Expeditor` job on the account.
- Policy roles with **no Square job**: Barback, Expeditor, Sous Chef. They are
  kept in the defaults so they work if the jobs are ever added.
- **There is no `Event Bartender` job.** If a bartender works a private event
  today, whatever they clock in under decides their pool — clocked in as
  `Bartender` puts their hours in the DAILY pool, not the event. Worth adding
  an `Event Bartender` job in Square if that is not the intent.
- A job title seen on a timecard but missing from this mapping blocks the day
  (`UnknownRoleError`) rather than being guessed.

### Reporting

Semi-monthly periods (1st–15th, 16th–EOM), the same as Tavern Law — the policy
pays tips "through semi-monthly payroll", and there is no cash payout run.

The CSV and the printable summary both carry **Points** alongside hours,
because points are the audit trail: `tips ÷ points` is what one point was
worth that period, so any row can be re-derived by hand. Event money is its
own column — it comes from a different pool than the daily 80/20.

| Column | Meaning |
|---|---|
| Tips (daily pool) | share of the 80/20 daily pool |
| Event Payout | share of any private/special event that period |
| Tips Total | the two added |
| Auto Gratuity (wages) | separate payroll line, never merged with tips |
| Days / Hours / Points | what the split was computed from |


---

## 3. Build state

| Piece | State |
|---|---|
| `engine/points_hours.py` daily model + 24 tests | **Done** — conservation asserted |
| `docs/M6-poquitos.md` (this file) | Done |
| Square job title → role mapping | **Done** — venue settings `poq_job_roles` / `poq_roles`; Setup editing UI still to come |
| Per-venue Square credentials | **Done** — token + location `LB3CVZKR3BMND` verified against the live account |
| Venue row (slug `poquitos`, POINTS_HOURS) | **Done** — seeded declaratively in `VENUE_SEEDS` |
| `compute.py` dispatch + day inputs + Square pull path | **Done** |
| Day screen (`renderDayPoq`) | **Done** — shifts with role chips, event section, live distribution |
| Period summary + CSV export + print summary | **Done** — semi-monthly, points carried through as the audit trail |
| Event sub-model (`compute_event_points_hours`) + 17 tests | **Done** — daily/event pool membership, 3% per group, conservation asserted |

Per-venue Square credentials, venue scoping, semi-monthly periods, RBAC, the
audit log and snapshot immutability all already exist from M3/M5 and need no
new work for this venue.
