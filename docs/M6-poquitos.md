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

Understood so far:

- An event is its own pool: event service charge + event tips, split **80/20**
  between (servers + bartenders who worked the event) and the kitchen.
- Support staff (busser, expo/food runner, host) take **3% each** out of the
  FOH portion, and **stay in the daily pool** as well.
- Service staff who worked the event are **only** in the event pool — their
  hours come out of that day's daily pool.
- An extra host gratuity at the end is split **equally** (not by points) among
  everyone who worked the event.
- Separate 3% administrative fee → the manager who organized the event.
- Take-out catering carries its own 20% service charge → Chef and Manager.

**Open questions blocking the event build** (each moves real money):

1. The 3% support tip-out — 3% *of the FOH portion*, or 3% of event sales /
   the event gross? And is it 3% per role (one busser share split among the
   bussers who worked) or 3% per person?
2. How is the kitchen's 20% of an event split — by hours worked that day,
   evenly per head, or among a named event crew?
3. "Service staff only in the event pool" — are they out of the daily pool for
   the *whole* day, or only for the hours worked at the event?
4. Take-out catering 20% → "Chef and Manager": what split between the two?
5. Is the 3% admin fee taken off the top before the 80/20, or additional to it?
6. Special events (Cinco de Mayo, Block Party) — same mechanics as a private
   event, or simply "everyone who worked shares the event tips"?

---

## 3. Build state

| Piece | State |
|---|---|
| `engine/points_hours.py` + 24 tests | **Done** — daily model, conservation asserted |
| `docs/M6-poquitos.md` (this file) | Done |
| Square job title → role mapping (Setup) | Pending real job titles from the Poquitos account |
| Venue row + per-venue Square credentials | Pending API key (`SQUARE_ACCESS_TOKEN__POQUITOS`) |
| `compute.py` dispatch + day inputs shape | Pending |
| Day screen (stepper) for POINTS_HOURS | Pending |
| Period summary + CSV export | Pending |
| Event sub-model | Blocked on the questions in §2 |

Per-venue Square credentials, venue scoping, semi-monthly periods, RBAC, the
audit log and snapshot immutability all already exist from M3/M5 and need no
new work for this venue.
